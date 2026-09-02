"""kb/km.py — the KM (knowledge-management) layer: aligned knowledge from verified fields.

This is the "aligned knowledge falls out of clean data" stage of the spine. It runs
AFTER extraction (storage/fields/<doc>.json) + human verification
(storage/review/<doc>.verified.json) and materialises a CLEAN, cross-document graph on
top of the existing :Document skeleton. Deterministic, free (no LLM) — every input is
already on disk.

It builds four things, all schema-driven (no per-document heuristics):

  1. :OpsField nodes — one per (document × filled ops field), carrying the value(s),
     parsed numbers (for aggregation), the evidence snippet+bbox (Prime Directive), and a
     TRUST TIER: `human_validated` where a verification overlay stamps it, else
     `machine_extracted`. `(:Document)-[:STATES]->(:OpsField)`.

  2. :CanonicalParty hubs — resolve-and-link (NOT mint-new): the Supplier / Customer named
     in each document collapse onto ONE hub per legal entity via a suffix-invariant key, so
     "NORTHWIND LOGISTICS PTE. LTD." and "Northwind Logistics" are the same node across the family.
     `(:OpsField)-[:RESOLVES_TO]->(:CanonicalParty)` + `(:Document)-[:HAS_PARTY]->`.

  3. The document-family DAG — AMENDS / SUPERSEDES / NOVATES between :Document, DECLARED in
     the recital fields (document_relationships.*), resolved within the family. A missed
     edge silently detaches a doc from its chain, so unresolved targets are logged.

  4. Per-field supersedence — within a family, the LATEST document that SETS a single-valued
     field wins (`is_current=true`); older statements are `is_current=false` +
     `superseded_by`. A field nobody re-states stays current (inherit from the base).
     Multi-valued fields (equipment lists) accumulate — every statement stays current.

  PYTHONUTF8=1 python -m scripts.build_km            # build from cache (needs the store up)
  PYTHONUTF8=1 python -m scripts.build_km --dry-run  # parse + report, no writes
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from ..config import Config
from ..store import model
from ..storage import fields_dir
from . import intake as intake_mod
from ..ontology import DEFAULT_DOCTYPE
from .opsview_spec import REL_CATEGORY, OpsView, family_policy, load as load_view
from .writers import _canonical_key, _props, _store, strip_system

# Date parsing for recital text and ISO dates. Inlined when the fact-side
# timeline module was retired: km is the only consumer left.
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
# "3 April 2020", "01 October 2022", "2nd August 1999"
_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _to_iso(text: str | None) -> str | None:
    """First parseable date in ``text`` as YYYY-MM-DD, else None."""
    if not text:
        return None
    m = _ISO.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _DMY.search(text)
    if m:
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(1)):02d}"
    return None

# Legal-entity suffixes peeled to canonicalise a party name (resolve-and-link).
# Common international forms, longest-first so "X Co Pte Ltd" peels cleanly.
# The active domain's `identity.legal_suffixes` is folded in on top, because a
# jurisdiction this list has never heard of is a domain fact, not an engine one.
_BASE_PARTY_SUFFIXES = [
    "private limited", "pte ltd", "pte limited", "pte", "ltd", "limited", "llp", "llc",
    "plc", "inc", "incorporated", "corporation", "corp", "company", "co", "sdn bhd",
    "sdn", "bhd", "gmbh", "ag", "bv", "nv", "sa",
]

# Template party tokens. A pro-forma document names its parties generically
# ("VENDOR", "COMPANY"). Those are honest extractions for THAT document but not
# real legal entities, so resolve-and-link ABSTAINS on them rather than minting
# a hub named after a role word.
#
# The words below are generic English. Each domain's OWN role words are folded
# in from `identity.canonical_roles` and `identity.generic_tokens`, because
# "the Receiving Party" is a placeholder in one domain and this engine list
# would never have contained it. Missing them is silent and corrupting: the
# identity layer mints a canonical party called Licensor and then resolves
# every document's real counterparty onto it.
_BASE_PLACEHOLDER_PARTIES = {
    "company", "the company", "vendor", "the vendor", "customer", "the customer",
    "supplier", "the supplier", "client", "the client", "contractor", "the contractor",
    "owner", "the owner", "operator", "parties", "party", "buyer", "seller",
}


@lru_cache(maxsize=None)
def _party_suffixes(doctype: str = DEFAULT_DOCTYPE) -> list[str]:
    """Suffix list for this domain, longest first so peeling is greedy."""
    extra = [str(s).strip().lower()
             for s in (_identity(doctype).get("legal_suffixes") or [])
             if str(s).strip()]
    return sorted(set(_BASE_PARTY_SUFFIXES) | set(extra), key=len, reverse=True)


@lru_cache(maxsize=None)
def _placeholder_parties(doctype: str = DEFAULT_DOCTYPE) -> frozenset[str]:
    """Words that name a ROLE rather than an entity, for this domain."""
    ident = _identity(doctype)
    words = set(_BASE_PLACEHOLDER_PARTIES)
    for role in (ident.get("canonical_roles") or []):
        word = str(role).replace("_", " ").strip().lower()
        if word:
            words.add(word)
            words.add(f"the {word}")
    for tok in (ident.get("generic_tokens") or []):
        word = str(tok).strip().lower()
        if word:
            words.add(word)
    return frozenset(words)


def _identity(doctype: str) -> dict:
    """The ontology's `identity` block, or an empty one. Never raises: a
    missing ontology must not take the whole alignment layer down."""
    try:
        from ..ontology import identity_policy, load_ontology
        return identity_policy(load_ontology(doctype) or {})
    except Exception:  # noqa: BLE001 — any config read failure means "no extras"
        return {}

# Categories that describe the DOCUMENT ITSELF (its type/date/ordinal) or the STABLE
# identity of the parties — not an evolving contract TERM. Supersedence is per-term
# ("the latest doc that sets a field wins"), so these must be EXCLUDED from the walk:
# a tech spec's "Customer = COMPANY" must never supersede the base's real customer, and
# each document keeps its own type/date as a fact about that document.
# ...all three of those rules are DECLARED, not known. They live in the view's
# `document_family:` block and its per-category `supersedes:` flags, read through
# opsview_spec.family_policy(). They used to be hardcoded sets here, which meant
# the supersedence walk — the machinery the "current value" answer rests on —
# only understood one domain's vocabulary for amendments and ancillary documents.

_UNDATED = "9999-99-99"    # sentinel for a document with no resolvable date


def _non_superseding(doctype: str = DEFAULT_DOCTYPE) -> frozenset[str]:
    return family_policy(doctype).non_superseding_categories
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
}


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


# --------------------------------------------------------------------------- #
# Pure helpers — number/date parsing, canonical keys, ordering, supersedence.
# Kept free of the store so tests/kb/test_km.py can exercise them without one.
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"\d[\d.,]*\d|\d")


def _to_float(tok: str) -> float | None:
    """Parse one numeric token, resolving the thousands-vs-decimal comma ambiguity:
    "1,576,035.00" → 1576035.0 (comma=thousands, dot=decimal); "1,000" → 1000.0
    (3-digit group = thousands); "4,5" → 4.5 (non-3-digit = decimal comma, e.g. barg)."""
    t = tok
    if "," in t and "." in t:
        t = t.replace(",", "")                       # comma=thousands, dot=decimal
    elif "," in t:
        parts = t.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            t = t.replace(",", ".")                  # decimal comma: 4,5 -> 4.5
        else:
            t = t.replace(",", "")                   # thousands: 1,000 -> 1000
    try:
        return float(t)
    except ValueError:
        return None


def parse_numbers(s) -> list[float]:
    """Every numeric magnitude in a value string (order preserved). Feeds aggregation —
    'sum the deposits' UNWINDs OpsField.numbers, so this is where a currency string like
    'S$159,455.60' becomes 159455.6."""
    out: list[float] = []
    for tok in _NUM_RE.findall(str(s or "")):
        n = _to_float(tok)
        if n is not None:
            out.append(n)
    return out


_UNIT_RE = re.compile(
    r"(?:s\$|sgd|usd|rm)|°?c\b|barg|bar\b|rth|rt\b|kg/cm[²2]?|kwh|kw\b|m3|m³|%|/h|/rth|/rt",
    re.IGNORECASE)


def parse_unit(s) -> str | None:
    """Best-effort first unit/currency token in a value (display only; aggregation uses
    numbers). Not exhaustive by design — the verbatim value is always kept."""
    m = _UNIT_RE.search(str(s or ""))
    return m.group(0) if m else None


def canonical_party_key(name: str | None,
                        doctype: str = DEFAULT_DOCTYPE) -> str:
    """Suffix-invariant canonical key for a legal entity — the resolve-and-link primitive.
    Empty (abstain, don't mint a hub) for a template token ("VENDOR"/"COMPANY"), for one
    of THIS domain's own role words ("the Receiving Party"), or for a name too short to
    trust."""
    if _norm(name) in _placeholder_parties(doctype):
        return ""
    key = _canonical_key(name, _party_suffixes(doctype))
    return key if len(key) >= 3 else ""


def ordinal_int(s) -> int:
    """Supplemental ordinal → int for chain ordering. Base / Standalone / None → 0."""
    t = _norm(s)
    if not t or t in ("none", "not stated", "standalone", "na", "n/a", "base"):
        return 0
    for word, n in _ORDINALS.items():
        if word in t:
            return n
    return 0


def rel_edge(relationship_type: str | None,
             doctype: str = DEFAULT_DOCTYPE) -> str | None:
    """The EXTRACTED relationship word → its DAG edge label, or None for a base
    document. Domain vocabulary: one domain writes "amends" in a field of its
    own, another says it by calling the document an "Amended and Restated"."""
    return family_policy(doctype).relation_edges.get(_norm(relationship_type))


# The uploader's DECLARED relationship, which is the engine's own vocabulary
# and not the domain's. `intake.RELATIONS` offers these four words on every
# deployment, so they must not be looked up in a domain's `relations:` map:
# that map is keyed on whatever word the DOCUMENTS use, and running human input
# through it silently dropped the edge on any domain whose two vocabularies
# were not accidentally identical.
_DECLARED_EDGES = {"amends": "AMENDS", "supersedes": "SUPERSEDES",
                   "novates": "NOVATES"}


def declared_edge(relation: str | None) -> str | None:
    """The uploader's declared relation → its DAG edge. None for 'standalone'."""
    return _DECLARED_EDGES.get(_norm(relation))


def _has_rects(rects) -> bool:
    """True only for a non-empty highlight-rects payload. Guards against an empty list
    ('[]') or null being mistaken for real page evidence (the Prime Directive: a value
    only counts as backed if it resolves to an actual clause rectangle)."""
    return bool(rects) and str(rects).strip() not in ("[]", "null", "{}")


def ev_key(page, snippet) -> str:
    """A stable identity for one evidence span — how a reviewer's 'wrong clause'
    rejection is matched back to the machine evidence (survives re-extraction and
    record rebuilds). Shared with the review API so both sides agree."""
    snip = " ".join(str(snippet or "").split())[:80].lower()
    return f"{page}:{snip}"


def _rect_digest(rects) -> str:
    """A short physical fingerprint of an evidence's highlight rectangles —
    what tells apart two near-identical rows on the same page when their text
    prefixes collide. Empty when the evidence has no rects."""
    try:
        arr = json.loads(rects) if isinstance(rects, str) else (rects or [])
    except (TypeError, ValueError):
        arr = []
    parts = []
    for r in arr or []:
        if not isinstance(r, dict):
            continue
        b = r.get("bbox") or [0, 0, 0, 0]
        try:
            parts.append(f"{r.get('page_no', 0)}:"
                         + ",".join(f"{float(x):.3f}" for x in b))
        except (TypeError, ValueError):
            continue
    if not parts:
        return ""
    return hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:10]


def _value_key(value) -> str:
    return " ".join(str(value or "").split())[:60].lower()


def ev_key2(page, snippet, rects, value) -> str:
    """The precise identity a NEW rejection stores: the legacy page+text core
    plus the rect fingerprint plus the value the reviewer clicked it on. A
    re-anchor that moves the rects (or a re-read that changes the text) stops
    the match, so drifted evidence resurfaces for re-review instead of staying
    invisibly rejected."""
    return f"{ev_key(page, snippet)}|r={_rect_digest(rects)}|v={_value_key(value)}"


def ev_rejected(rejected, *, page, snippet, rects, value) -> bool:
    """Whether a stored rejection matches this evidence-of-a-value. Legacy
    entries (no '|') match on page+text alone, field-wide — grandfathered so
    existing review work keeps its effect. v2 entries also require the rect
    fingerprint and the value to agree, so twin table rows never collide and
    a rejection made on one value never strips its siblings."""
    if ev_key(page, snippet) in rejected:
        return True
    return ev_key2(page, snippet, rects, value) in rejected


def field_values(extraction: dict, full_key: str,
                 rejected: frozenset | set = frozenset()) -> list[dict]:
    """A field's extracted values with their first NON-REJECTED evidence, as
    [{value, snippet, page, rects, has_evidence}]. A value whose every citation the
    reviewer rejected is dropped (the human said the clauses don't support it).
    Empty when the field is Not Stated."""
    entry = (extraction.get("fields") or {}).get(full_key)
    if not entry:
        return []
    out: list[dict] = []
    for v in entry.get("values") or []:
        val = (v.get("value") or "").strip()
        if not val:
            continue
        evs = [e for e in (v.get("evidence") or [])
               if not ev_rejected(rejected, page=e.get("page"),
                                  snippet=e.get("snippet"),
                                  rects=e.get("rects"), value=val)]
        if not evs and (v.get("evidence") or []):
            continue                                # all citations rejected → withdrawn
        e0 = evs[0] if evs else {}
        out.append({
            "value": val, "snippet": e0.get("snippet"), "page": e0.get("page"),
            "rects": e0.get("rects"), "has_evidence": any(_has_rects(e.get("rects")) for e in evs),
        })
    return out


def added_values(overlay_entry: dict | None) -> list[dict]:
    """Reviewer-attached values ('the AI missed this clause') in field_values shape."""
    out: list[dict] = []
    for a in (overlay_entry or {}).get("added") or []:
        val = str(a.get("value") or "").strip()
        if not val:
            continue
        out.append({"value": val, "snippet": a.get("snippet"), "page": a.get("page"),
                    "rects": a.get("rects"), "has_evidence": _has_rects(a.get("rects"))})
    return out


def doc_relationship(extraction: dict, doctype: str = DEFAULT_DOCTYPE) -> dict:
    """The document_relationships.* fields as a normalised record (dates → ISO).

    Which field carries which role is domain configuration. The six role names
    below are the engine's vocabulary and never change. The field each one
    reads comes from `document_family.field_roles`, because one domain's
    `agreement_date` is another's `document_date` and reading the wrong name
    returns None from every lookup: the chain then ties on the undated
    sentinel and "the latest document wins" quietly becomes "the last document
    id alphabetically wins".
    """
    roles = family_policy(doctype).field_roles

    def role(name: str) -> str | None:
        fk = roles.get(name)
        if not fk:
            return None
        vs = field_values(extraction, f"{REL_CATEGORY}.{fk}")
        return vs[0]["value"] if vs else None

    rt = role("relationship_type")
    return {
        "document_type": role("document_type"),
        "ordinal": ordinal_int(role("ordinal")),
        "relationship_type": rt,
        "edge": rel_edge(rt, doctype),
        "document_date": _to_iso(role("document_date")),
        "amends_dated": _to_iso(role("amends_dated")),
        "effective_date": _to_iso(role("effective_date")),
    }


def best_date(rel: dict, graph_effective: str | None,
              declared_date: str | None = None) -> str:
    """The date to order a document in its family: the amendment's effective date if
    stated, else the uploader's DECLARED document date (human input beats the
    extracted document date), else the extracted date, else the effective date the
    timeline pass set, else the undated sentinel (an undateable doc has no timeline
    position — it never supersedes). The extracted effective date still ranks first
    because it is a different, more specific axis: when the change takes effect,
    not when the paper is dated."""
    return (rel.get("effective_date") or declared_date
            or rel.get("document_date") or graph_effective or _UNDATED)


def order_family(docs: list[dict]) -> list[dict]:
    """Order a family's documents into their chain: by best_date, ordinal as
    tiebreaker, doc_id last so a dead tie is deterministic across rebuilds
    (store enumeration order must never decide which value is current)."""
    return sorted(docs, key=lambda d: (d.get("best_date") or "9999-99-99",
                                       d.get("ordinal", 0), d.get("doc_id", "")))


_NOT_STATED = "not stated"


def states_a_value(field: dict) -> bool:
    """Whether a document's field record actually says something.

    The schema's own "Not Stated" is the ABSENCE of a statement, not a statement
    of absence. It reaches a record only one way: a verifier correcting a value
    the model read into a document that does not contain one. Counting that as
    stated would let the correction supersede the real value in an earlier
    document, which is the opposite of what the verifier meant. A blank in an
    amendment inherits, and this is how a human writes that blank.
    """
    return any(v and _norm(v) != _NOT_STATED for v in (field.get("values") or ()))


def supersedence(ordered_docs: list[dict], multiplicity: dict[str, int]) -> dict[tuple, dict]:
    """Per-field currency over a family's ordered documents.

    ``ordered_docs``: oldest→newest, each ``{doc_id, stated_fields: set(full_key)}``.
    ``multiplicity``: full_key → its cardinality (only single-valued fields supersede;
    multi-valued fields accumulate — every statement stays current).

    Returns ``{(doc_id, full_key): {is_current, superseded_by}}``.
    """
    by_field: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for i, d in enumerate(ordered_docs):
        for fk in d.get("stated_fields") or ():
            by_field[fk].append((i, d["doc_id"]))

    status: dict[tuple, dict] = {}
    for fk, occ in by_field.items():
        supersedable = multiplicity.get(fk, 1) == 1 and len(occ) > 1
        if not supersedable:
            for _, doc_id in occ:
                status[(doc_id, fk)] = {"is_current": True, "superseded_by": None}
            continue
        occ.sort()
        newest = occ[-1][1]
        for _, doc_id in occ:
            status[(doc_id, fk)] = ({"is_current": True, "superseded_by": None}
                                    if doc_id == newest
                                    else {"is_current": False, "superseded_by": newest})
    return status


def _is_base_candidate(doc: dict, doctype: str = DEFAULT_DOCTYPE) -> bool:
    """A document an amendment can point at: no outgoing edge (it's a base/standalone) and
    NOT an ancillary type (a technical spec / FDD states values but isn't in the chain)."""
    return (doc.get("edge") is None
            and _norm(doc.get("document_type"))
            not in family_policy(doctype).ancillary_doc_types)


def is_chain_member(doc: dict, doctype: str = DEFAULT_DOCTYPE) -> bool:
    """A document that is a link in the amendment chain (base or amendment) —
    ancillary types (tech spec / FDD / NTP) state values but never carry the
    contract's evolving identity."""
    return _norm(doc.get("document_type")) not in family_policy(doctype).ancillary_doc_types


def party_supersedable(field: dict, doc: dict) -> bool:
    """Whether a party statement joins the per-field supersedence walk.

    Party identity sits in a NON-superseding category by default (an ancillary
    document's placeholder "COMPANY" must never clobber a real party), but a
    novation or assignment exists precisely to change the party, so a party
    field DOES supersede when the document is a real chain member and the value
    is a real legal entity (non-placeholder)."""
    if field.get("mechanism") != "party" or not field.get("values"):
        return False
    if not is_chain_member(doc):
        return False
    return bool(canonical_party_key(field["values"][0]))


def resolve_dag_target(doc: dict, family: list[dict]) -> str | None:
    """Which document in the family this amendment/novation points at.

    An uploader-DECLARED parent (the intake form) wins outright — it is human
    input. Otherwise: prefer an explicit date match (``amends_dated`` ==
    another doc's ``document_date``); else the single base document (no edge,
    not ancillary). When several bases compete, temporal consistency
    disambiguates: a document cannot amend one dated AFTER it, so dated
    candidates in the amendment's future drop out (undated candidates survive —
    they cannot be excluded on evidence). Still ambiguous → None (the caller
    logs it rather than guessing, so a wrong chain never forms silently)."""
    if doc.get("edge") is None:
        return None                                  # a base doc has no outgoing edge
    declared = doc.get("intake_parent")
    # A self-reference can never win (a document cannot amend itself); it
    # falls through to the recital and resolve_family surfaces the conflict.
    if declared and declared != doc["doc_id"] \
            and any(o["doc_id"] == declared for o in family):
        return declared
    return recital_target(doc, family)


def dated_target(doc: dict, family: list[dict]) -> str | None:
    """The recital's EVIDENCE-backed resolution only: an explicit "agreement
    dated X" matching another document's date. No single-base inference —
    this is the resolution strong enough to contradict a human declaration."""
    amends = doc.get("amends_dated")
    if not amends:
        return None
    for other in family:
        if other["doc_id"] != doc["doc_id"] and other.get("document_date") == amends:
            return other["doc_id"]
    return None


def recital_target(doc: dict, family: list[dict]) -> str | None:
    """The evidence-only resolution (what the document's own recital supports),
    kept callable on its own so a declared parent can be cross-checked against
    it instead of silently replacing it."""
    dated = dated_target(doc, family)
    if dated:
        return dated
    bases = [o for o in family if o["doc_id"] != doc["doc_id"] and _is_base_candidate(o)]
    src_date = doc.get("best_date")
    if len(bases) > 1 and src_date and src_date != _UNDATED:
        bases = [b for b in bases
                 if (b.get("best_date") or _UNDATED) == _UNDATED
                 or b["best_date"] <= src_date]
    if len(bases) == 1:
        return bases[0]["doc_id"]
    return None


def group_families(records: list[dict]) -> dict[str, list[dict]]:
    """Group documents into families by their declared group. A document WITHOUT
    a group forms its own singleton family — unrelated standalone documents must
    never chain or supersede each other just because neither was assigned."""
    fams: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        fams[r.get("group") or f"solo:{r['doc_id']}"].append(r)
    return fams


def resolve_family(fam: list[dict]) -> tuple[list[tuple[str, str, str]], int,
                                             dict[str, str], list[dict]]:
    """Resolve one family's declared DAG: ``([(src, edge, tgt)], n_unresolved,
    {tgt_doc_id: declared_date}, conflicts)``.

    ``conflicts`` lists documents whose UPLOADER-declared parent disagrees with
    what the recital evidence resolves to — the declared edge still wins (human
    input), but the disagreement is surfaced instead of swallowed.

    Second pass mutates ``fam`` in place: an UNDATED edge target inherits the
    earliest date its amendments DECLARE for it ("the Agreement dated 2 August
    1999" — recital evidence, not inference), so the supersedence chain can
    order a base whose own date the extractor missed."""
    edges: list[tuple[str, str, str]] = []
    conflicts: list[dict] = []
    unresolved = 0
    for doc in fam:
        if doc.get("edge") is None:
            continue
        declared = doc.get("intake_parent")
        # A declared parent that CANNOT take effect is a human statement
        # evaporating — that must surface as a conflict, never a log counter.
        if declared == doc["doc_id"]:
            conflicts.append({"doc_id": doc["doc_id"], "declared": declared,
                              "recital": None, "reason": "self"})
        elif declared and not any(o["doc_id"] == declared for o in fam):
            conflicts.append({"doc_id": doc["doc_id"], "declared": declared,
                              "recital": None, "reason": "missing"})
        tgt = resolve_dag_target(doc, fam)
        if tgt is None:
            unresolved += 1
        else:
            edges.append((doc["doc_id"], doc["edge"], tgt))
            if declared and tgt == declared:
                # Cross-check only against recital EVIDENCE (an explicit
                # dated reference). The single-base fallback is an inference,
                # so declaring an intermediate amendment as the parent must
                # not raise a false disagreement.
                recital = dated_target(doc, fam)
                if recital and recital != declared:
                    conflicts.append({"doc_id": doc["doc_id"],
                                      "declared": declared, "recital": recital})

    # The chain must stay a DAG. A declared edge that loops back to its own
    # source would detach every downstream doc from the base, so the closing
    # edge(s) drop and the loop is surfaced instead. Deterministic: a mutual
    # A<->B pair drops both edges (a human must pick the real parent).
    tgt_of = {s: t for s, _e, t in edges}
    kept: list[tuple[str, str, str]] = []
    for s, e, t in edges:
        cur, seen = t, set()
        cycle = False
        while cur is not None and cur not in seen:
            if cur == s:
                cycle = True
                break
            seen.add(cur)
            cur = tgt_of.get(cur)
        if cycle:
            unresolved += 1
            conflicts.append({"doc_id": s, "declared": t,
                              "recital": None, "reason": "cycle"})
        else:
            kept.append((s, e, t))
    edges = kept

    by_id = {d["doc_id"]: d for d in fam}
    declared: dict[str, str] = {}
    for src_id, _edge, tgt_id in edges:
        d = by_id[src_id].get("amends_dated")
        if d and by_id[tgt_id].get("best_date", _UNDATED) == _UNDATED:
            declared[tgt_id] = min(declared.get(tgt_id, d), d)
    for tgt_id, d in declared.items():
        by_id[tgt_id]["best_date"] = d
    return edges, unresolved, declared, conflicts


def current_party_keys(fam: list[dict]) -> dict[str, str]:
    """The family's CURRENT party per principal ROLE, as canonical keys.

    Walk the dated chain newest to oldest and, for each role, take the first
    document that names a real (non-placeholder) party in it. A novation is
    simply the newest chain document naming a different party, so it wins.

    Per role rather than for one hardcoded role: which role changes hands is
    domain vocabulary. Reading only one named role meant that domains without
    that word found nothing and every party stayed current forever.

    Pure. Each fam record carries ``party_keys`` (role -> key, may be empty).
    """
    chain = [d for d in fam
             if is_chain_member(d) and d.get("best_date", _UNDATED) != _UNDATED]
    out: dict[str, str] = {}
    for doc in sorted(chain,
                      key=lambda d: (d["best_date"], d.get("ordinal", 0),
                                     d.get("doc_id", "")),
                      reverse=True):
        for role, key in (doc.get("party_keys") or {}).items():
            if key and role not in out:
                out[role] = key
    return out


# --------------------------------------------------------------------------- #
# Cache readers
# --------------------------------------------------------------------------- #
def _cache(cfg: Config) -> Path:
    return fields_dir(cfg)


def _load_extraction(cfg: Config, doc_id: str) -> dict | None:
    p = _cache(cfg) / f"{doc_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Silence here is the worst outcome available. The document drops out
        # of the rebuild entirely, so its old field rows survive from the last
        # build and keep `is_current` forever, and it leaves its family's
        # supersedence walk: a newer document can no longer supersede it. That
        # reads as a correct knowledge base right up until someone asks.
        print(f"  WARNING: corrupt field extraction for {doc_id}, skipping it. "
              f"Delete {p} and re-extract to restore this document.")
        return None


def _load_verified(cfg: Config, doc_id: str) -> dict:
    """The human-verification overlay: full_key → the compiled vote consensus
    {verified, value, verifier, at, confidence, verifiers, n_votes, disputed,
    needs_correction, evidence} (api/review_votes.py compiles it after every
    vote; legacy entries carry just the first four keys). ``evidence`` is the
    clause a winning correction carried: it re-anchors the field's highlight."""
    p = cfg.storage_root / "review" / f"{doc_id}.verified.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Never fail the whole rebuild on one torn overlay, but never hide it
        # either: the doc silently losing its verified tier is the worst case.
        print(f"[km] WARNING: verification overlay unreadable, doc {doc_id} "
              f"builds as machine-extracted only: {p}")
        return {}


def _ops_id(doc_id: str, full_key: str) -> str:
    return f"{doc_id}:{full_key}"


def _build_doc_record(cfg: Config, view: OpsView, doc_id: str, meta: dict) -> dict | None:
    """Parse one document's extraction + verification into the shape the writers consume.
    Returns None when the document has no schema-first extraction yet."""
    extraction = _load_extraction(cfg, doc_id)
    if not extraction:
        return None
    verified = _load_verified(cfg, doc_id)
    rel = doc_relationship(extraction)
    intake = intake_mod.load_intake(cfg, doc_id)

    fields: list[dict] = []
    for f in view.fields:
        ov = verified.get(f.full_key) or {}
        # Apply the reviewer's evidence corrections: rejected clauses detach (a
        # value with no surviving citation is withdrawn), attached clauses append.
        vals = field_values(extraction, f.full_key,
                            rejected=frozenset(ov.get("rejected_evidence") or []))
        vals += added_values(ov)
        is_verified = bool(ov.get("verified"))
        corrected = bool(is_verified and ov.get("value"))
        # A verified correction stands even when every machine clause was
        # rejected: the human value IS the field then. Skipping it here would
        # let one rejected bad clause silently erase the correction from the
        # knowledge base while the review screen still shows it verified.
        if not vals and not corrected:
            continue
        # A human edit overrides the machine value; a plain confirm keeps it. Either way
        # a verified field carries the higher trust tier + lineage.
        value_strings = [v["value"] for v in vals]
        cev = None
        displaced: list[str] = []
        if corrected:
            # What the machine said before the human overrode it. Retrieval's
            # legacy fact tier still carries these values, so they ride on the
            # OpsField for the fallback tools to tag or suppress.
            displaced = [s for s in value_strings if str(s) != str(ov["value"])]
            value_strings = [str(ov["value"])]
            # The clause the winning correction carried (if any) re-anchors
            # the field: the highlight must back the value people see.
            c = ov.get("evidence")
            if isinstance(c, dict) and (c.get("rects") or c.get("snippet")):
                cev = c
        numbers = [n for vs in value_strings for n in parse_numbers(vs)]
        e0 = vals[0] if vals else {}
        fields.append({
            "ops_id": _ops_id(doc_id, f.full_key),
            "doc_id": doc_id, "full_key": f.full_key, "category": f.category,
            "field_key": f.key, "title": f.title, "type": f.type,
            "sensitivity": f.sensitivity, "multiplicity": f.multiplicity,
            "mechanism": f.mechanism,
            "value": "; ".join(value_strings), "values": value_strings,
            "n_values": len(value_strings),
            "numbers": numbers, "unit": parse_unit(value_strings[0]),
            "trust": "human_validated" if is_verified else "machine_extracted",
            "verified": is_verified, "verifier": ov.get("verifier"),
            "verified_at": ov.get("at"),
            "confidence": ov.get("confidence"),
            "verifiers": ov.get("verifiers")
                or ([ov["verifier"]] if is_verified and ov.get("verifier") else []),
            "n_votes": ov.get("n_votes"),
            "disputed": bool(ov.get("disputed")),
            "needs_correction": bool(ov.get("needs_correction")),
            "snippet": (cev or e0).get("snippet"), "page": (cev or e0).get("page"),
            "rects": (cev or e0).get("rects"),
            "has_evidence": bool(cev.get("rects")) if cev
                            else any(v["has_evidence"] for v in vals),
            "displaced_values": displaced,
            "party_role": (f.source.get("role") if f.mechanism == "party" else None),
        })

    # The uploader's declared relationship outranks the extracted recital (it
    # is human input); the recital stays behind it as the cross-check. An
    # explicit "standalone" is a declaration too: it must beat an extracted
    # relation exactly like a declared link does, and it retires any stale
    # parent left over from an earlier declaration.
    edge = rel["edge"]
    declared_rel = (intake.get("relation") or "").lower() or None
    if intake_mod.link_relation(declared_rel):
        edge = declared_edge(intake["relation"])
    elif declared_rel == "standalone":
        edge = None

    # The party signals used for the family's party-currency walk, taken from
    # the parties the document itself names. Every principal role this domain
    # declares, not one role named in the engine: the first document to name a
    # given role wins it, and roles nobody names simply stay absent.
    party_names: dict[str, str] = {}
    party_keys: dict[str, str] = {}
    principal = {str(r).strip().lower()
                 for r in (_identity(DEFAULT_DOCTYPE).get("canonical_roles") or [])}
    for f in fields:
        role = (f.get("party_role") or "").strip().lower()
        if not role or role in party_keys or not f["values"]:
            continue
        if principal and role not in principal:
            continue
        key = canonical_party_key(f["values"][0])
        if key:
            party_names[role], party_keys[role] = f["values"][0], key

    # Sidecar family first: an upload can family a document after it was
    # loaded, and the graph copy only catches up when build_km stamps it.
    group = intake_mod.load_group(cfg, doc_id) or meta.get("group")

    return {
        "doc_id": doc_id, "title": meta.get("title") or doc_id,
        "group": group or doc_id,                      # ungrouped doc = its own family
        "graph_effective": meta.get("effective_date"),
        "document_type": rel["document_type"], "relationship_type": rel["relationship_type"],
        "edge": edge, "ordinal": rel["ordinal"],
        "document_date": rel["document_date"], "amends_dated": rel["amends_dated"],
        "best_date": best_date(rel, meta.get("effective_date"),
                               _to_iso(intake.get("document_date"))),
        "intake": intake,
        "intake_parent": (None if declared_rel == "standalone"
                          else intake.get("parent_doc_id")),
        "party_names": party_names, "party_keys": party_keys,
        "fields": fields,
    }


# --------------------------------------------------------------------------- #
# Store writers (sync store, mirrors kb.timeline).
# --------------------------------------------------------------------------- #
# No DDL in Cosmos: ops_id / hub key ARE the item ids; the old ops_fulltext
# Lucene index is replaced by Python keyword scoring at read time
# (pipeline/store/query.py).


def _clear_doc_fields(store, doc_id: str) -> None:
    for r in store.query("SELECT c.id FROM c WHERE c.kind = 'opsfield'", pk=doc_id):
        store.delete(r["id"], doc_id)


def _clear_parties(store) -> None:
    """Canonical hubs are FULLY re-derived every run, so clear them first —
    plus every edge touching them (the old DETACH DELETE) — otherwise a hub
    minted before a fix (e.g. a placeholder "COMPANY") lingers with its
    HAS_PARTY edges."""
    for r in store.query(
            "SELECT c.id FROM c WHERE c.kind = 'hub' AND c.hub = 'CanonicalParty'",
            pk=model.GLOBAL_PK):
        store.delete(r["id"], model.GLOBAL_PK)
    store.delete_where(
        "c.kind = 'edge' AND (STARTSWITH(c.src, 'CanonicalParty:') "
        "OR STARTSWITH(c.tgt, 'CanonicalParty:'))")


def _clear_dag(store, ids: list[str]) -> None:
    """Remove the Document-level DAG edges for these families (the
    Agreement-level timeline edges carry ':agreement' ids and stay)."""
    params = [{"name": "@ids", "value": ids}]
    store.delete_where(
        "c.kind = 'edge' AND ARRAY_CONTAINS(@ids, c.src) AND "
        "ARRAY_CONTAINS(['AMENDS','SUPERSEDES','NOVATES'], c.rel)", params)


def _stamp_eff(store, doc_id: str, eff: str) -> None:
    """Only-when-null: the legacy timeline pass owns this property where it
    ran; schema-first docs (no Date facts, no timeline) get their KM
    best_date so `effective_date DESC` recency ranking works for them too."""
    doc = store.point(doc_id, doc_id)
    if doc is not None and not doc.get("effective_date"):
        store.patch(doc_id, doc_id,
                    [{"op": "set", "path": "/effective_date", "value": eff}])


def _stamp_group(store, doc_id: str, group: str) -> None:
    """The sidecar's family declaration is newer than the loaded copy (an
    upload can family a document after load) — push it back onto the item so
    every group-keyed query (families, timeline, scoping) sees the same
    family."""
    store.patch(doc_id, doc_id, [{"op": "set", "path": "/group", "value": group}])


def _stamp_chain(store, doc_id: str, best_date: str, ordinal: int) -> None:
    """The document's position in its family chain, exactly as this build
    computed it.

    Written unconditionally, unlike effective_date, which the legacy timeline
    pass owns and which therefore cannot be trusted to equal best_date. A
    targeted currency refresh reads these two so it orders the family the same
    way the full build did instead of re-deriving the order from a different
    property and quietly disagreeing with it."""
    store.patch(doc_id, doc_id, [
        {"op": "set", "path": "/chain_date", "value": best_date},
        {"op": "set", "path": "/chain_ordinal", "value": int(ordinal or 0)}])


def refresh_field_currency(store, doc_id: str, full_key: str) -> int:
    """Re-run the supersedence walk for ONE field across its family.

    A verifier's correction can change WHETHER a document states a field at
    all, because "Not Stated" is the absence of a statement rather than a value
    (see states_a_value). That moves which document holds the current value.
    The vote path only stamps the node it edited, so without this the corrected
    document goes on shadowing the value it was wrongly superseding until
    somebody runs a full build, and the correction appears to do nothing. That
    is the one promise the review workflow makes.

    Identity categories are left to the full build: their currency depends on
    the novation carve-out in party_supersedable, which needs document records
    this path does not have. Returns the number of nodes changed.
    """
    docs = store.query("SELECT c.doc_id, c['group'] AS grp, c.chain_date, "
                       "c.chain_ordinal FROM c WHERE c.kind = 'document'")
    me = next((d for d in docs if d["doc_id"] == doc_id), None)
    if me is None:
        return 0
    group = me.get("grp") or doc_id
    fam = {d["doc_id"]: d for d in docs if (d.get("grp") or d["doc_id"]) == group}
    if len(fam) < 2:
        return 0                          # a family of one supersedes nothing
    if not any(d.get("chain_date") for d in fam.values()):
        # A store written before chain_date existed. Treating every document as
        # undated would mark every statement current, which is worse than
        # waiting: leave it, the next build stamps the family and settles it.
        return 0

    rows = store.query(
        "SELECT c.doc_id, c.category, c['values'] AS vals, c.multiplicity, "
        "c.is_current, c.superseded_by FROM c WHERE c.kind = 'opsfield' "
        "AND c.full_key = @fk AND ARRAY_CONTAINS(@ids, c.doc_id)",
        [{"name": "@fk", "value": full_key},
         {"name": "@ids", "value": sorted(fam)}])
    if not rows or rows[0].get("category") in _non_superseding():
        return 0

    ordered = order_family([
        {"doc_id": r["doc_id"],
         "best_date": fam[r["doc_id"]].get("chain_date") or _UNDATED,
         "ordinal": fam[r["doc_id"]].get("chain_ordinal") or 0,
         "stated_fields": ({full_key} if states_a_value({"values": r.get("vals")})
                           else set())}
        for r in rows])
    dated = [d for d in ordered if d["best_date"] != _UNDATED]
    status = supersedence(
        dated, {full_key: int(rows[0].get("multiplicity") or 1)})

    changed = 0
    for r in rows:
        st = status.get((r["doc_id"], full_key))
        if st is None:
            # No verdict means the document states nothing here, or it has no
            # date and so no position in the chain. Either way it is not the
            # current value — unless nothing in the family is dated, in which
            # case there is no chain to be measured against and blanking every
            # statement would hide the field entirely.
            st = {"is_current": not dated, "superseded_by": None}
        if (bool(r.get("is_current")) == st["is_current"]
                and r.get("superseded_by") == st["superseded_by"]):
            continue
        store.patch(_ops_id(r["doc_id"], full_key), r["doc_id"], [
            {"op": "set", "path": "/is_current", "value": st["is_current"]},
            {"op": "set", "path": "/superseded_by", "value": st["superseded_by"]}])
        changed += 1
    return changed

# --- Declared identity (the upload intake form) ------------------------------ #


def _upsert_party_hub(store, key: str, name: str) -> str:
    """CanonicalParty hub item, name kept from the first writer (the old
    ON CREATE SET). Carries BOTH `key` (km's axis) and `normalized_name`
    (mint_hubs' axis) — they are the same canonical value."""
    hid = f"CanonicalParty:{key}"
    prior = strip_system(store.point(hid, model.GLOBAL_PK))
    store.upsert(model.hub_item("CanonicalParty", key, {
        **prior, "key": key, "normalized_name": key,
        "name": prior.get("name") or name,
    }))
    return hid


def _write_party(store, key: str, name: str, ops_id: str,
                 doc_id: str, role: str) -> None:
    hid = _upsert_party_hub(store, key, name)
    store.upsert(model.edge_item("RESOLVES_TO", ops_id, hid, doc_id))
    eid = f"e:HAS_PARTY:{doc_id}:{hid}"
    prior = strip_system(store.point(eid, doc_id))
    store.upsert(model.edge_item("HAS_PARTY", doc_id, hid, doc_id,
                                 {**{k: v for k, v in prior.items()
                                     if k in ("declared", "current")},
                                  "role": role}))


def _intake_mismatch(store, pid: str, doc_id: str, target: str,
                     reason: str) -> None:
    """Cross-check quarantine: the uploader declared customer X but the
    document's own text names a different (real, non-placeholder) party. The
    declared bind stays (human input wins) — the disagreement is surfaced for
    review instead of silently picked."""
    store.upsert(model.item(model.PROPOSAL, pid, doc_id, {
        "proposal_id": pid, "proposal_kind": "grounding", "status": "pending",
        "reason": reason, "source_node_id": doc_id, "target_node_id": target,
        "confidence": 0.0, "doc_id": doc_id,
    }))


def _clear_intake_mismatch(store, pid: str, doc_id: str) -> None:
    store.delete(pid, doc_id)


def _set_party_current(store, ids: list[str], keys_by_role: dict[str, str]) -> None:
    """Party currency per family: after a novation, exactly one hub per ROLE is
    CURRENT, the one the newest chain document (declared or extracted) names.

    Roles the family never resolved are left alone rather than being marked
    stale, because "nobody named a party in this role" is not the same as
    "the party in this role was replaced"."""
    if not keys_by_role:
        return
    roles = sorted(keys_by_role)
    for doc_id in ids:
        for e in store.query(
                "SELECT * FROM c WHERE c.kind = 'edge' AND c.rel = 'HAS_PARTY' "
                "AND ARRAY_CONTAINS(@roles, c.role)",
                [{"name": "@roles", "value": roles}], pk=doc_id):
            key = keys_by_role.get(e.get("role"))
            cur = e.get("tgt") == f"CanonicalParty:{key}"
            store.patch(e["id"], doc_id,
                        [{"op": "set", "path": "/current", "value": cur}])

# --- Block sensitivity (closes the raw-text leak) --------------------------- #
# Text search (vector/keyword over blocks) bypasses the fact-label policy — a
# raw paragraph carries no label, so a default user's chat could quote a rate
# table verbatim. Fix: stamp `sensitivity` on block items at build time from
# every source that KNOWS where sensitive values live, and let the retrieval
# tools filter on it. The port computes the UNION of all four sources per doc
# in Python, then applies one reset-then-set patch pass over the doc's blocks.


def _restricted_fact_block_ids_and_texts(
        store, doc_id: str, labels: list[str]) -> tuple[set[str], set[str]]:
    """(cited block ids, evidence texts) of the restricted-class legacy facts
    (the old _FACT_BLOCK_SENS + _RESTRICTED_FACT_SPAN_TEXTS in one pass)."""
    fact_ids = {r["id"] for r in store.query(
        "SELECT c.id FROM c WHERE c.kind = 'fact' AND "
        "ARRAY_CONTAINS(@labs, c.label)",
        [{"name": "@labs", "value": labels}], pk=doc_id)}
    block_ids: set[str] = set()
    texts: set[str] = set()
    if not fact_ids:
        return block_ids, texts
    for e in store.query(
            "SELECT c.fact_id, c.block_ids, c.text_span FROM c "
            "WHERE c.kind = 'evidence' AND IS_DEFINED(c.fact_id)", pk=doc_id):
        if e["fact_id"] not in fact_ids:
            continue
        for bid in e.get("block_ids") or []:
            block_ids.add(f"{doc_id}:{bid}")
        if e.get("text_span"):
            texts.add(e["text_span"])
    return block_ids, texts


def _mention_block_ids(store, doc_id: str, raw_labels: list[str]) -> set[str]:
    out: set[str] = set()
    for m in store.query(
            "SELECT c.block_ids FROM c WHERE c.kind = 'mention' AND "
            "ARRAY_CONTAINS(@raws, c.raw_label) AND IS_DEFINED(c.block_ids)",
            [{"name": "@raws", "value": raw_labels}], pk=doc_id):
        for bid in m.get("block_ids") or []:
            out.add(f"{doc_id}:{bid}")
    return out


def _apply_block_sensitivity(store, doc_id: str, conf_ids: set[str],
                             tokens: set[str]) -> int:
    """Reset-then-set over the doc's blocks: sensitivity = confidential when
    the block is in ``conf_ids`` OR its text carries a distinctive value
    token (the cross-layer value net). Returns how many blocks the VALUE net
    tagged beyond the id sources (the old _VALUE_BLOCK_SENS count).

    Also stamps ``cites_confidential`` onto the doc's evidence + mention
    items (their block_ids touch a confidential block) — that turns the old
    per-query CITES_BLOCK sensitivity join into a plain field check in the
    retrieval tools."""
    value_hits = 0
    final_conf: set[str] = set()
    for b in store.query(
            "SELECT c.id, c.text, c.sensitivity FROM c WHERE c.kind = 'block'",
            pk=doc_id):
        text = b.get("text") or ""
        by_value = any(t in text for t in tokens) if tokens else False
        conf = b["id"] in conf_ids or by_value
        if by_value:
            value_hits += 1
        if conf:
            final_conf.add(b["id"])
        if conf and b.get("sensitivity") != "confidential":
            store.patch(b["id"], doc_id, [
                {"op": "set", "path": "/sensitivity", "value": "confidential"}])
        elif not conf and b.get("sensitivity") is not None:
            store.patch(b["id"], doc_id,
                        [{"op": "remove", "path": "/sensitivity"}])

    conf_local = {bid.split(":", 1)[1] for bid in final_conf}   # 'b0042' form
    for kind in ("evidence", "mention"):
        for it in store.query(
                f"SELECT c.id, c.block_ids, c.cites_confidential FROM c "
                f"WHERE c.kind = '{kind}'", pk=doc_id):
            cites = bool(set(it.get("block_ids") or []) & conf_local)
            if cites != bool(it.get("cites_confidential")):
                store.patch(it["id"], doc_id, [
                    {"op": "set", "path": "/cites_confidential", "value": cites}])
    return value_hits


def distinctive_value_tokens(value: str) -> list[str]:
    """Tokens precise enough to tag raw text without false positives: decimal
    numbers ('0.2485'), thousands-grouped amounts ('550,000'), long digit runs
    (phone numbers), and emails. Bare small integers ('12', '2020') are NOT
    distinctive — tagging them would blanket half the document."""
    out: set[str] = set()
    for m in re.finditer(r"\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+|\d{5,}", value or ""):
        tok = m.group(0)
        digits = re.sub(r"\D", "", tok)
        if len(digits) >= 4 or ("." in tok and len(digits) >= 3):
            out.add(tok)
    for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.]+", value or ""):
        out.add(m.group(0))
    return sorted(out)


def _policy_financials() -> tuple[list[str], list[str]]:
    """(fact labels, harvest raw_labels) of the classes the DEFAULT role is denied,
    read straight from configs/policy/sensitivity.yaml — one classification drives
    answers, blur AND block tagging. Empty lists if the policy file is absent."""
    import yaml
    path = Path(__file__).resolve().parents[2] / "configs" / "policy" / "sensitivity.yaml"
    if not path.exists():
        return [], []
    pol = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    denied = set(((pol.get("roles") or {}).get("default") or {}).get("deny") or [])
    labels: list[str] = []
    raws: list[str] = []
    for cls, spec in (pol.get("classes") or {}).items():
        if cls in denied:
            labels += [str(x) for x in (spec or {}).get("labels") or []]
            raws += [str(x).lower() for x in (spec or {}).get("raw_labels") or []]
    return labels, raws


def block_marker_map(cfg: Config, doc_id: str) -> dict[str, str]:
    """Extractor marker `pNbM` -> graph Block id `<doc_id>:bKKKK`.

    Walks pages_md exactly like the schema-first renderer (pages sorted by
    page_no; the global counter k advances for EVERY block), so a cited marker
    resolves to the same canonical block the reading stage numbered."""
    d = cfg.storage_root / "pages_md" / doc_id
    out: dict[str, str] = {}
    if not d.exists():
        return out
    pages: list[dict] = []
    for p in sorted(d.glob("p_*.json")):
        try:
            pages.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    pages.sort(key=lambda pg: pg.get("page_no", 0))
    k = 0
    for pg in pages:
        n = int(pg.get("page_no") or 0)
        for idx, _b in enumerate(pg.get("blocks") or []):
            k += 1
            out[f"p{n}b{idx}"] = f"{doc_id}:b{k:04d}"
    return out


def confidential_block_ids(cfg: Config, view: OpsView, doc_id: str) -> list[str]:
    """Graph Block ids cited as evidence for CONFIDENTIAL ontology fields in this
    document — from the extractor's marker citations (exact mapping), plus a
    bbox-equality fallback for reviewer-attached evidence (whose rects came from
    the same pages_md blocks, so equality is exact, not fuzzy)."""
    p = fields_dir(cfg) / f"{doc_id}.json"
    if not p.exists():
        return []
    try:
        ext = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    conf_keys = {f.full_key for f in view.fields if f.sensitivity == "confidential"}
    markers: set[str] = set()
    rect_keys: set[tuple] = set()
    for fk, entry in (ext.get("fields") or {}).items():
        if fk not in conf_keys:
            continue
        for v in entry.get("values") or []:
            for e in v.get("evidence") or []:
                for m in e.get("blocks") or []:
                    markers.add(str(m))
                for r in _parse_rects(e.get("rects")):
                    rect_keys.add(_rect_key(r))
    # Reviewer-attached evidence on confidential fields (rects only, no markers).
    ov_path = cfg.storage_root / "review" / f"{doc_id}.verified.json"
    if ov_path.exists():
        try:
            overlay = json.loads(ov_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            overlay = {}
        for fk, entry in overlay.items():
            if fk not in conf_keys:
                continue
            for a in entry.get("added") or []:
                for r in _parse_rects(a.get("rects")):
                    rect_keys.add(_rect_key(r))

    mmap = block_marker_map(cfg, doc_id)
    ids = {mmap[m] for m in markers if m in mmap}
    if rect_keys:
        # bbox-equality fallback: same walk, match block bboxes to attached rects.
        d = cfg.storage_root / "pages_md" / doc_id
        pages = []
        for pp in sorted(d.glob("p_*.json")) if d.exists() else []:
            try:
                pages.append(json.loads(pp.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        pages.sort(key=lambda pg: pg.get("page_no", 0))
        k = 0
        for pg in pages:
            n = int(pg.get("page_no") or 0)
            for b in pg.get("blocks") or []:
                k += 1
                bb = b.get("bbox")
                if bb and _rect_key({"page_no": n, "bbox": bb}) in rect_keys:
                    ids.add(f"{doc_id}:b{k:04d}")
    return sorted(ids)


def _parse_rects(rects) -> list[dict]:
    if not rects:
        return []
    if isinstance(rects, str):
        try:
            rects = json.loads(rects)
        except json.JSONDecodeError:
            return []
    return [r for r in rects if isinstance(r, dict) and r.get("bbox")] if isinstance(rects, list) else []


def _rect_key(r: dict) -> tuple:
    bb = r.get("bbox") or []
    return (int(r.get("page_no") or 0), *(round(float(x), 4) for x in bb))


def _write_field(store, f: dict) -> None:
    props = {
        "ops_id": f["ops_id"], "doc_id": f["doc_id"], "full_key": f["full_key"],
        "category": f["category"], "field_key": f["field_key"], "title": f["title"],
        "type": f["type"], "sensitivity": f["sensitivity"], "multiplicity": f["multiplicity"],
        "mechanism": f["mechanism"], "value": f["value"], "values": f["values"],
        "n_values": f["n_values"], "numbers": f["numbers"], "unit": f["unit"],
        "trust": f["trust"], "verified": f["verified"], "verifier": f["verifier"],
        "verified_at": f["verified_at"], "confidence": f["confidence"],
        "verifiers": f["verifiers"], "n_votes": f["n_votes"], "disputed": f["disputed"],
        "snippet": f["snippet"], "page": f["page"],
        "rects": f["rects"], "has_evidence": f["has_evidence"],
        "displaced_values": f.get("displaced_values") or None,
        "is_current": True, "superseded_by": None,     # default; supersedence pass refines
    }
    store.upsert(model.item(model.OPSFIELD, f["ops_id"], f["doc_id"], _props(props)))


def build_km(cfg: Config, *, dry_run: bool = False) -> dict:
    """Materialise the aligned KM layer from the extraction + verification cache. Idempotent
    (per-doc OpsField cleared + re-MERGEd; DAG edges cleared per family). Returns counts."""
    view = load_view()
    multiplicity = {f.full_key: f.multiplicity for f in view.fields}
    counts = {"docs": 0, "ops_fields": 0, "human_validated": 0, "parties": 0,
              "dag_edges": 0, "dag_unresolved": 0, "superseded": 0, "conf_blocks": 0,
              "declared_binds": 0, "intake_conflicts": 0, "assets": 0}

    store = _store(cfg)
    store.ensure()
    metas = {r["doc_id"]: {"id": r["doc_id"],
                           "title": r.get("title") or r["doc_id"],
                           "group": r.get("grp"),
                           "effective_date": r.get("effective_date")}
             for r in store.query(
                 "SELECT c.doc_id, c.title, c['group'] AS grp, "
                 "c.effective_date FROM c WHERE c.kind = 'document'")}

    # Parse every document that has a schema-first extraction.
    records: list[dict] = []
    for doc_id, meta in metas.items():
        rec = _build_doc_record(cfg, view, doc_id, dict(meta))
        if rec is not None:
            records.append(rec)
    counts["docs"] = len(records)
    counts["ops_fields"] = sum(len(r["fields"]) for r in records)
    counts["human_validated"] = sum(
        1 for r in records for f in r["fields"] if f["trust"] == "human_validated")

    if dry_run:
        fams = defaultdict(list)
        for r in records:
            fams[r["group"]].append(r["doc_id"])
        counts["families"] = len(fams)
        return counts

    _clear_parties(store)                  # re-derive canonical hubs from scratch

    # 1. OpsField items + 2. canonical parties. Also stamp the document's
    # effective date (only when the timeline pass didn't already) and push the
    # sidecar family onto the item.
    canon_names: dict[str, str] = {}
    for rec in records:
        _clear_doc_fields(store, rec["doc_id"])
        if rec["best_date"] != _UNDATED:
            _stamp_eff(store, rec["doc_id"], rec["best_date"])
        _stamp_chain(store, rec["doc_id"], rec["best_date"], rec["ordinal"])
        if rec["group"] != rec["doc_id"]:
            _stamp_group(store, rec["doc_id"], rec["group"])
        for f in rec["fields"]:
            _write_field(store, f)
            if f["party_role"] and f["values"]:
                key = canonical_party_key(f["values"][0])
                if key:
                    canon_names.setdefault(key, f["values"][0])
                    _write_party(store, key, f["values"][0],
                                 f["ops_id"], rec["doc_id"], f["party_role"])

        _clear_intake_mismatch(store, f"intake:customer:{rec['doc_id']}",
                               rec["doc_id"])
    counts["parties"] = len(canon_names)

    # 3. Document-family DAG (declared edges) + 4. per-field supersedence.
    families = group_families(records)

    for fam in families.values():
        _clear_dag(store, [d["doc_id"] for d in fam])
        edges, unresolved, declared, dag_conflicts = resolve_family(fam)
        for src, edge, tgt in edges:
            store.upsert(model.edge_item(edge, src, tgt, src, {
                "method": "declared", "via": "recital_field"}))
        counts["dag_edges"] += len(edges)
        counts["dag_unresolved"] += unresolved
        # A declared parent that disagrees with the recital, points nowhere,
        # or would loop the chain: keep the human input where it can hold,
        # quarantine the problem (never silently pick a side or drop it).
        for c in dag_conflicts:
            reason = c.get("reason")
            if reason == "self":
                msg = "uploader linked this document to itself; the link is ignored"
            elif reason == "missing":
                msg = (f"uploader linked this document to {c['declared']}, which is "
                       f"not in this folder (deleted, or filed elsewhere)")
            elif reason == "cycle":
                msg = (f"uploader link to {c['declared']} would loop the amendment "
                       f"chain; the edge was dropped")
            else:
                msg = (f"uploader linked this document to {c['declared']} "
                       f"but its recital resolves to {c['recital']}")
            _intake_mismatch(store, f"intake:parent:{c['doc_id']}",
                             c["doc_id"], c["declared"], msg)
            counts["intake_conflicts"] += 1
        # Symmetric clear (mirrors the customer cross-check): once the admin
        # fixes the declaration, the pending proposal must not outlive it.
        conflict_ids = {c["doc_id"] for c in dag_conflicts}
        for d in fam:
            if d["doc_id"] not in conflict_ids:
                _clear_intake_mismatch(store, f"intake:parent:{d['doc_id']}",
                                       d["doc_id"])
        # A backfilled chain date is real ordering knowledge — stamp it so
        # `effective_date DESC` recency ranking sees the doc too.
        for tgt_id, d in declared.items():
            _stamp_eff(store, tgt_id, d)

        # Supersedence walks only the CHAIN — dated documents — over evolving
        # contract TERMS. Identity/document-metadata categories stay out, with
        # ONE carve-out: a real party stated by a real chain document DOES
        # supersede (that's what a novation is) — see party_supersedable.
        ordered = order_family([
            {"doc_id": d["doc_id"], "best_date": d["best_date"], "ordinal": d["ordinal"],
             "stated_fields": {f["full_key"] for f in d["fields"]
                               if states_a_value(f)
                               and (f["category"] not in _non_superseding()
                                    or party_supersedable(f, d))}}
            for d in fam if d["best_date"] != _UNDATED])
        status = supersedence(ordered, multiplicity)
        # A "Not Stated" record is kept for audit (it is a verifier's decision)
        # but it is not a current value of the field, so say so explicitly. The
        # walk never returns a verdict for it, and the default is current.
        for d in fam:
            for f in d["fields"]:
                if not states_a_value(f):
                    status.setdefault((d["doc_id"], f["full_key"]),
                                      {"is_current": False, "superseded_by": None})

        # An UNDATED document is excluded from the walk above, because a
        # document with no resolvable date has no position in the chain and
        # cannot supersede anything. Its fields kept the `is_current: True`
        # default, though, so a later document could not supersede them either
        # and both values counted in every aggregate. Two answers to "what is
        # the current rate", one of them from a document nobody could place.
        #
        # Only when the family HAS a dated chain to be measured against. A
        # family where nothing is dated is an undated corpus, not a
        # supersedence problem, and blanking it would hide every value.
        if len(ordered) and len(ordered) < len(fam):
            for d in fam:
                if d["best_date"] != _UNDATED:
                    continue
                for f in d["fields"]:
                    status[(d["doc_id"], f["full_key"])] = {
                        "is_current": False, "superseded_by": None}
                counts["undated_excluded"] = counts.get("undated_excluded", 0) + 1
                print(f"  WARNING: {d['doc_id']} has no resolvable date, so it "
                      f"cannot take a position in its family's chain. Its "
                      f"values are marked not-current. Declare a document date "
                      f"on upload to place it.")
        for (doc_id, fk), st in status.items():
            store.patch(_ops_id(doc_id, fk), doc_id, [
                {"op": "set", "path": "/is_current", "value": st["is_current"]},
                {"op": "set", "path": "/superseded_by", "value": st["superseded_by"]},
            ])
        counts["superseded"] += sum(1 for st in status.values() if not st["is_current"])

        # 4b. Party currency: per principal role, flag which hub is CURRENT for
        # the family (newest chain doc naming that role wins).
        cur_keys = current_party_keys(fam)
        if cur_keys:
            _set_party_current(store, [d["doc_id"] for d in fam], cur_keys)

    # 5. Block sensitivity — stamp the raw-text layer so retrieval can filter
    # it per role. Reset-then-set per doc, so an ontology sensitivity DOWNGRADE
    # takes effect on rebuild. Three id sources of "this block holds sensitive
    # text" (confidential ontology fields' evidence, financial typed facts,
    # financial orphan fragments) + the value net, unioned in one pass.
    fin_labels, fin_raws = _policy_financials()
    for rec in records:
        did = rec["doc_id"]
        conf_ids = set(confidential_block_ids(cfg, view, did))
        counts["conf_blocks"] += len(conf_ids)
        fact_texts: set[str] = set()
        if fin_labels:
            fact_ids_blocks, fact_texts = _restricted_fact_block_ids_and_texts(
                store, did, fin_labels)
            conf_ids |= fact_ids_blocks
        if fin_raws:
            conf_ids |= _mention_block_ids(store, did, fin_raws)
        # 4th source: the confidential VALUES themselves, wherever they
        # recur in raw text (formulas, recitals the extractor never cited).
        # Tokens come from BOTH layers — the ontology fields AND the
        # restricted-class legacy facts' evidence — so a miss in one layer
        # can't leave the value unprotected in raw text.
        tokens = {t
                  for f in rec["fields"] if f["sensitivity"] == "confidential"
                  for v in (f["values"] or [])
                  for t in distinctive_value_tokens(v)}
        for t_text in fact_texts:
            tokens.update(distinctive_value_tokens(t_text))
        counts["conf_blocks"] += _apply_block_sensitivity(store, did, conf_ids, tokens)
        # Persist the tokens on the document so ANSWER-TIME tagging can run
        # the same value net over the citation bundle — a mislabelled fact
        # (e.g. a Formula carrying a rate) can't ride a label loophole out.
        # Always set: an empty list clears stale tokens after a downgrade.
        store.patch(did, did, [{"op": "set", "path": "/conf_tokens",
                                "value": sorted(tokens)}])

    # 6. Page assets — deterministic scan of the cached OCR layer for
    # figure-dominant pages (schematics/drawings), so chat can point a user at
    # "the plant schematic on page 18". Free; lives here so EVERY build path
    # (rebuild_kb, the admin extract job, scripts.build_km) picks it up.
    from . import assets as assets_mod
    counts["assets"] = assets_mod.build_assets(cfg).get("assets", 0)

    return {k: v for k, v in counts.items() if v or k in ("docs", "ops_fields")}


def main() -> None:
    import argparse

    from dotenv import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser(description="Build the aligned KM layer from the cache.")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, no graph writes")
    args = ap.parse_args()
    print("[KM]", build_km(Config.load(), dry_run=args.dry_run))


if __name__ == "__main__":
    main()
