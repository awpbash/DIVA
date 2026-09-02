"""kb/km_query.py — read the aligned KM layer (the two-mode retrieval surface).

Read-only queries over the opsfield / CanonicalParty / document-DAG items that
kb.km builds. Two modes, right tool per question (the spine's retrieval
principle):

  * ALIGNED LOOKUP — ``aligned_view`` returns a family's knowledge apples-to-apples: the
    canonical parties, the document chain, and every field's CURRENT value (with the
    superseded prior values kept for provenance). This is what the Knowledge Base view
    renders.
  * DETERMINISTIC AGGREGATION — ``aggregate`` sums / lists a field's numbers across
    documents in code, never by LLM map-reduce (exact arithmetic is a code job).

RBAC is enforced HERE, not in the UI: a ``default`` viewer never receives a
``confidential`` field's value; ``confidential`` + ``admin`` see everything. Mirrors the
review + ontology sensitivity model so one classification drives every surface.
"""
from __future__ import annotations

from collections import defaultdict

from ..config import Config
from ..store import model
from . import opsview_spec
from .writers import _store

_ROLES = {"admin", "confidential", "default"}

_DAG_RELS = ["AMENDS", "SUPERSEDES", "NOVATES"]


def _role(role: str | None) -> str:
    r = (role or "default").lower()
    return r if r in _ROLES else "default"


def _can_see(sensitivity: str | None, role: str) -> bool:
    if role in ("admin", "confidential"):
        return True
    return str(sensitivity or "general") != "confidential"


def _docs_with_groups(store) -> list[dict]:
    return store.query(
        "SELECT c.doc_id, c.title, c['group'] AS grp, c.effective_date FROM c "
        "WHERE c.kind = 'document'")


def _family_doc_ids(store, group: str) -> list[dict]:
    return [d for d in _docs_with_groups(store)
            if (d.get("grp") or d["doc_id"]) == group]


def families(cfg: Config, role: str | None = None) -> list[dict]:
    """Every contract family with its documents + a value count (richest first)."""
    store = _store(cfg)
    docs = _docs_with_groups(store)
    field_counts: dict[str, int] = defaultdict(int)
    for r in store.query(
            "SELECT c.doc_id FROM c WHERE c.kind = 'opsfield'"):
        field_counts[r["doc_id"]] += 1
    fams: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        fams[d.get("grp") or d["doc_id"]].append({
            "doc_id": d["doc_id"],
            "title": d.get("title") or d["doc_id"],
            "date": d.get("effective_date"),
            "n_fields": field_counts.get(d["doc_id"], 0),
        })
    out = []
    for grp, fam_docs in fams.items():
        fam_docs.sort(key=lambda d: (d.get("date") or "9999", d.get("title") or ""))
        out.append({"group": grp, "title": _family_title(fam_docs),
                    "n_docs": len(fam_docs),
                    "n_fields": sum(d["n_fields"] for d in fam_docs),
                    "docs": fam_docs})
    out.sort(key=lambda f: -f["n_fields"])
    return out


def _family_title(docs: list[dict]) -> str:
    """A human family label: the base/earliest document's title, trimmed of doc suffixes."""
    if not docs:
        return "—"
    base = docs[0].get("title") or docs[0].get("doc_id")
    return str(base)


def aligned_view(cfg: Config, group: str, role: str | None = None) -> dict:
    """A family's aligned knowledge: canonical parties, the document DAG, and every field's
    CURRENT value with its superseded history — RBAC-filtered for the role."""
    r = _role(role)
    store = _store(cfg)
    fam_docs = _family_doc_ids(store, group)
    doc_ids = [d["doc_id"] for d in fam_docs]
    titles = {d["doc_id"]: d.get("title") or d["doc_id"] for d in fam_docs}
    if not doc_ids:
        return {"group": group, "role": r, "parties": [], "dag": [],
                "conflicts": [], "categories": [], "hidden_fields": 0,
                "n_fields": 0, "n_current_statements": 0, "n_verified": 0}

    # Parties: HAS_PARTY edge items (pk = doc) + hub items.
    hp_edges = store.query(
        "SELECT * FROM c WHERE c.kind = 'edge' AND c.rel = 'HAS_PARTY' AND "
        "ARRAY_CONTAINS(@ids, c.pk)", [{"name": "@ids", "value": doc_ids}])
    hubs = {h["id"]: h for h in store.query(
        "SELECT * FROM c WHERE c.kind = 'hub' AND c.hub = 'CanonicalParty'",
        pk=model.GLOBAL_PK)}
    by_hub: dict[str, dict] = {}
    for e in hp_edges:
        hub = hubs.get(e["tgt"]) or {}
        slot = by_hub.setdefault(e["tgt"], {
            "name": hub.get("name") or e["tgt"].split(":", 1)[1],
            "key": hub.get("key") or hub.get("normalized_name"),
            "roles": set(), "cur_true": False, "cur_false": False,
        })
        if e.get("role"):
            slot["roles"].add(e["role"])
        if e.get("current") is True:
            slot["cur_true"] = True
        if e.get("current") is False:
            slot["cur_false"] = True
    parties = []
    for _hub_id, s in by_hub.items():
        # ``current``: tri-state. True/False only when a party-currency pass
        # flagged the family; None means "no currency knowledge", so the UI
        # shows nothing rather than lying.
        parties.append({
            "name": s["name"], "key": s["key"], "roles": sorted(s["roles"]),
            "current": (True if s["cur_true"]
                        else False if s["cur_false"] else None),
        })
    parties.sort(key=lambda p: p["name"] or "")

    # Unresolved intake disagreements (uploader-declared identity vs what the
    # document's own text says) — shown as a banner so a reviewer settles them.
    conflicts = [
        {"reason": p.get("reason"), "doc_id": p["doc_id"],
         "doc_title": titles.get(p["doc_id"], p["doc_id"])}
        for p in store.query(
            "SELECT c.reason, c.doc_id FROM c WHERE c.kind = 'proposal' AND "
            "c.proposal_kind = 'grounding' AND c.status = 'pending' AND "
            "STARTSWITH(c.id, 'intake:') AND ARRAY_CONTAINS(@ids, c.doc_id)",
            [{"name": "@ids", "value": doc_ids}])
    ]
    conflicts.sort(key=lambda c: c["doc_title"])

    # Document DAG: doc-level edge items (agreement-level timeline edges
    # carry ':agreement' ids and are excluded).
    dag = [
        {"src": e["src"], "rel": e["rel"], "tgt": e["tgt"]}
        for e in store.query(
            "SELECT c.src, c.rel, c.tgt FROM c WHERE c.kind = 'edge' AND "
            "ARRAY_CONTAINS(@rels, c.rel) AND ARRAY_CONTAINS(@ids, c.src)",
            [{"name": "@rels", "value": _DAG_RELS},
             {"name": "@ids", "value": doc_ids}])
    ]

    rows = store.query(
        "SELECT * FROM c WHERE c.kind = 'opsfield' AND "
        "ARRAY_CONTAINS(@ids, c.doc_id)",
        [{"name": "@ids", "value": doc_ids}])
    rows.sort(key=lambda o: (o.get("category") or "", o.get("title") or ""))

    # Group field statements by full_key; split current vs superseded; RBAC-filter.
    by_key: dict[str, dict] = {}
    hidden = 0
    for row in rows:
        if not _can_see(row.get("sensitivity"), r):
            hidden += 1
            continue
        k = row["full_key"]
        slot = by_key.setdefault(k, {
            "full_key": k, "field_key": row.get("field_key"), "category": row.get("category"),
            "title": row.get("title"), "type": row.get("type"),
            "sensitivity": row.get("sensitivity"),
            "multiplicity": row.get("multiplicity"), "current": [], "superseded": [],
        })
        stmt = {
            "value": row.get("value"), "values": row.get("values"), "unit": row.get("unit"),
            "trust": row.get("trust"), "verified": row.get("verified"),
            "confidence": row.get("confidence"), "verifiers": row.get("verifiers"),
            "n_votes": row.get("n_votes"), "disputed": bool(row.get("disputed")),
            "doc_id": row["doc_id"],
            "doc_title": titles.get(row["doc_id"], row["doc_id"]),
            "page": row.get("page"), "snippet": row.get("snippet"),
            "rects": row.get("rects"),
        }
        current = row.get("is_current", True) is not False
        (slot["current"] if current else slot["superseded"]).append(stmt)

    # Order fields into categories for display.
    cats: dict[str, list[dict]] = {}
    for f in by_key.values():
        cats.setdefault(f["category"], []).append(f)
    ordered = [{"category": c, "fields": sorted(cats[c], key=lambda x: x["title"])}
               for c in opsview_spec.order_categories(cats)]

    n_current = sum(len(f["current"]) for f in by_key.values())
    n_verified = sum(1 for f in by_key.values() for s in f["current"] if s["verified"])
    return {
        "group": group, "role": r, "parties": parties, "dag": dag,
        "conflicts": conflicts,
        "categories": ordered, "hidden_fields": hidden,
        "n_fields": len(by_key), "n_current_statements": n_current, "n_verified": n_verified,
    }


_AGG_OPS = ("sum", "list")


def aggregate(cfg: Config, field_key: str, group: str | None = None,
              role: str | None = None, op: str = "sum") -> dict:
    """Deterministic aggregation of a field's numeric values across CURRENT statements —
    exact arithmetic in code, the right tool for 'sum the demand charges'.

    Two ops. ``sum`` totals the numbers, per unit: values carrying DIFFERENT units never
    fold into one number, so a mixed-unit field returns ``by_unit`` subtotals and a None
    ``sum``. ``list`` returns the per-document values with no arithmetic. Any other op
    raises ValueError.

    RBAC: a confidential field is excluded for a ``default`` viewer (both the number and
    the fact of it), so an aggregate can never leak a value the role can't see."""
    if op not in _AGG_OPS:
        raise ValueError(f"unknown op {op!r}; use one of {_AGG_OPS}")
    r = _role(role)
    store = _store(cfg)
    doc_ids: list[str] | None = None
    if group is not None:
        doc_ids = [d["doc_id"] for d in _family_doc_ids(store, group)]
    params: list[dict] = [{"name": "@fk", "value": field_key}]
    scope = "true"
    if doc_ids is not None:
        params.append({"name": "@ids", "value": doc_ids})
        scope = "ARRAY_CONTAINS(@ids, c.doc_id)"
    # NB: the alias must not be `value` — VALUE is a Cosmos SQL keyword and
    # the emulator's parser rejects it as an alias (SC1001).
    rows = store.query(
        f"SELECT c.doc_id, c.title, c['value'] AS field_value, c.numbers, c.unit, "
        f"c.sensitivity, c.is_current FROM c WHERE c.kind = 'opsfield' "
        f"AND c.field_key = @fk AND {scope}", params)
    rows = [row for row in rows if row.get("is_current", True) is not False]

    visible = [row for row in rows if _can_see(row.get("sensitivity"), r)]
    items = []
    by_unit: dict[str, list[float]] = defaultdict(list)
    for row in visible:
        nums = list(row.get("numbers") or [])
        items.append({"doc_id": row["doc_id"], "title": row.get("title"),
                      "value": row.get("field_value"), "numbers": nums, "unit": row.get("unit")})
        unit_key = str(row.get("unit") or "").strip() or "(none)"
        for n in nums:
            by_unit[unit_key].append(float(n))
    n_nums = sum(len(ns) for ns in by_unit.values())
    result = {"field_key": field_key, "group": group, "op": op, "n_docs": len(visible),
              "n_hidden": len(rows) - len(visible), "items": items,
              "n_numbers": n_nums}
    if op == "list":
        return result
    if len(by_unit) > 1:
        # Mixed units (e.g. S$/kWh and S$/RT): one total would be
        # meaningless, so return per-unit subtotals instead.
        result["sum"] = None
        result["by_unit"] = [{"unit": u, "sum": round(sum(ns), 4), "n_numbers": len(ns)}
                             for u, ns in sorted(by_unit.items())]
        result["note"] = "values span multiple units. See by_unit for per-unit sums."
    else:
        result["sum"] = (round(sum(next(iter(by_unit.values()))), 4)
                         if by_unit else 0.0)
    return result
