"""View-time access policy (RBAC redaction).

Decides, per viewer ROLE, which retrieved citations are restricted — driven by
``configs/policy/sensitivity.yaml`` (classes -> fact labels; roles -> denied
classes). Deterministic and graph-free: a fact is classified by its label (or,
for raw orphan fragments, its harvest raw_label), so new documents are covered
with no code change.

Two enforcement helpers the chat route uses:
  * ``tag_citations`` — stamp ``sensitivity`` + ``restricted`` on each citation
    for the client (so the UI can blur restricted evidence).
  * ``partition`` — split into (visible, hidden) so the answer is synthesised
    from visible evidence only; ``redaction_note`` summarises what was withheld.

The role is currently supplied by the request (faked via the UI). Replacing
that with a real credential check is the only step to production access control.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .schemas import Citation

_POLICY_DIR = Path(__file__).resolve().parents[2] / "configs" / "policy"


def _policy_path() -> Path:
    """The active domain's policy file, falling back to the generic one.

    Which labels carry money is domain vocabulary: one domain calls them Charge
    and Rate, another calls the same thing Payment. A single global file was
    seeded with the first domain's labels, so on any other domain the class
    matched nothing and every restricted fact was served to every viewer with
    no error. Per-domain files mirror how views, packs, ontologies and the
    extraction prompt already resolve.
    """
    try:
        from pipeline.ontology import resolve_active_doctype
        per_domain = _POLICY_DIR / f"sensitivity.{resolve_active_doctype()}.yaml"
        if per_domain.exists():
            return per_domain
    except Exception:
        # An unresolvable domain must not take the app down on an import-time
        # policy read. The generic file below denies nothing extra, and
        # `unmatched_labels()` reports the resulting gap at boot.
        pass
    return _POLICY_DIR / "sensitivity.yaml"

# In-memory ACTIVE policy. Seeded lazily from the YAML; the admin panel mutates
# it at runtime via set_hidden_labels(). Resets to the YAML seed on restart
# (in-memory by design — real persistence/auth comes later).
_ACTIVE: dict | None = None
_INDEX: tuple[dict, dict] | None = None


def _load_yaml() -> dict:
    path = _policy_path()
    if not path.exists():
        return {"classes": {}, "roles": {}, "default_role": "general"}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def unmatched_labels() -> dict[str, list[str]]:
    """Class -> the labels it names that the ACTIVE pack does not declare.

    Empty is the healthy answer. A non-empty result means the redaction for
    that class is partly or wholly inert, which is silent by nature: nothing
    errors, restricted values simply reach viewers who should not see them.
    Surfaced at boot and by the setup command rather than left to be noticed.
    """
    try:
        from pipeline.extraction import pack as _pack
        declared = set(_pack.load().fact_labels)
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for cls, spec in (_policy().get("classes") or {}).items():
        missing = [str(lab) for lab in ((spec or {}).get("labels") or [])
                   if str(lab) not in declared]
        if missing:
            out[cls] = missing
    return out


def _policy() -> dict:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = _load_yaml()
    return _ACTIVE


def _label_index() -> tuple[dict[str, str], dict[str, str]]:
    """(fact_label -> class, raw_label -> class) lookup tables. Cached; the
    cache is invalidated whenever the active policy changes."""
    global _INDEX
    if _INDEX is None:
        by_label: dict[str, str] = {}
        by_raw: dict[str, str] = {}
        for cls, spec in (_policy().get("classes") or {}).items():
            for lab in (spec or {}).get("labels") or []:
                by_label[str(lab)] = cls
            for rl in (spec or {}).get("raw_labels") or []:
                by_raw[str(rl).lower()] = cls
        _INDEX = (by_label, by_raw)
    return _INDEX


def default_role() -> str:
    return str(_policy().get("default_role") or "general")


def denied_classes(role: str | None) -> set[str]:
    roles = _policy().get("roles") or {}
    r = roles.get(role or default_role())
    if r is None:                      # unknown role -> treat as default
        r = roles.get(default_role()) or {}
    return set((r or {}).get("deny") or [])


def sensitivity_of(c: Citation) -> str | None:
    """The access class a citation belongs to, or None if unclassified."""
    by_label, by_raw = _label_index()
    if c.fact_label and c.fact_label in by_label:
        return by_label[c.fact_label]
    if c.raw_label and c.raw_label.lower() in by_raw:
        return by_raw[c.raw_label.lower()]
    return None


def tag_citations(citations: list[Citation], role: str | None,
                  doc_tokens: dict[str, list[str]] | None = None) -> None:
    """Stamp ``sensitivity`` + ``restricted`` (for this role) on each citation,
    in place. ``restricted`` drives the client-side blur.

    ``doc_tokens`` is the VALUE NET at answer time: {doc_id: confidential value
    tokens} stamped by build_km. Labels alone are a loophole — a mislabelled
    fact (a Formula carrying a rate) or a visible citation's ``row_context``
    (the full table row, adjacent cells included) can smuggle a restricted
    value past the class check. Any citation whose text carries one of its
    document's confidential tokens is restricted for an uncleared role, no
    matter which tool produced it or what label it wears."""
    denied = denied_classes(role)
    for c in citations:
        cls = sensitivity_of(c)
        restricted = bool(cls and cls in denied)
        if not restricted and denied and doc_tokens:
            toks = doc_tokens.get(c.doc_id) or ()
            if toks:
                text = " ".join(filter(None, (c.snippet, c.row_context,
                                              c.linked_context, c.fact_summary)))
                if any(t in text for t in toks):
                    cls = cls or "financial"
                    restricted = True
        c.sensitivity = cls
        c.restricted = restricted


def partition(citations: list[Citation], role: str | None) -> tuple[list[Citation], list[Citation]]:
    """(visible, hidden) for this role. The answer is built from ``visible``
    only; ``hidden`` never reaches synth. Trusts the ``restricted`` flag when
    ``tag_citations`` ran first (it carries the value net); the label-class
    check stays as a belt-and-braces fallback."""
    denied = denied_classes(role)
    visible, hidden = [], []
    for c in citations:
        if c.restricted or sensitivity_of(c) in denied:
            hidden.append(c)
        else:
            visible.append(c)
    return visible, hidden


def redaction_note(hidden: list[Citation], role: str | None) -> str:
    """One-line notice for the synth prompt; '' when nothing was withheld."""
    if not hidden:
        return ""
    by_class: dict[str, int] = {}
    for c in hidden:
        cls = sensitivity_of(c) or "restricted"
        by_class[cls] = by_class.get(cls, 0) + 1
    parts = ", ".join(f"{n} {cls}" for cls, n in sorted(by_class.items()))
    return (f"{len(hidden)} item(s) ({parts}) were withheld for the current "
            f"access role '{role or default_role()}'.")


def restricted_labels(role: str | None) -> set[str]:
    """Fact labels denied to this role — used to drop matching aggregations so a
    sum/avg can't leak a restricted total, and to scan a document for the
    regions that must be blurred in the PDF viewer."""
    by_label, _ = _label_index()
    denied = denied_classes(role)
    return {lab for lab, cls in by_label.items() if cls in denied}


def restricted_raw_labels(role: str | None) -> set[str]:
    """Harvest raw_labels denied to this role — covers orphan money fragments
    (FactMentions backing no typed fact) when scanning a document to blur."""
    _, by_raw = _label_index()
    denied = denied_classes(role)
    return {rl for rl, cls in by_raw.items() if cls in denied}


# ---------------------------------------------------------------------------
# Runtime admin — what the "Access policy" UI panel reads + writes. In-memory
# (seeded from YAML, resets on restart). Edits the LOW-PRIVILEGE role's
# sensitivity as a flat list of fact labels — the simple model the panel shows.
# ---------------------------------------------------------------------------

def _label_raw() -> dict[str, list[str]]:
    """Fact label -> the raw harvest labels that feed it.

    Hiding a fact label has to hide the raw fragments carrying the same text,
    or an orphan mention leaks what the typed fact conceals. The pack already
    declares which raw labels route into each fact type, so read it there. This
    was a literal two-entry map of one domain's labels, which meant every other
    domain hid the fact and left the fragments visible.
    """
    from pipeline.extraction.pack import load as load_pack

    out: dict[str, list[str]] = {}
    for ft in load_pack().fact_types.values():
        if ft.label and ft.raw_labels:
            out.setdefault(ft.label, []).extend(ft.raw_labels)
    return {k: sorted(set(v)) for k, v in out.items()}


def available_labels() -> list[str]:
    """The fact labels an admin can mark sensitive (the pack's typed facts)."""
    from pipeline.extraction.pack import load as load_pack
    from pipeline.ontology import DEFAULT_DOCTYPE
    return sorted(load_pack(DEFAULT_DOCTYPE).fact_labels)


def restricted_role() -> str:
    """The role the panel edits — the first role that denies something, else
    the default (low-privilege) role."""
    for name, spec in (_policy().get("roles") or {}).items():
        if (spec or {}).get("deny"):
            return name
    return default_role()


def policy_state() -> dict:
    """Snapshot for GET /policy: the editable label set + current selection."""
    role = restricted_role()
    return {
        "available_labels": available_labels(),
        "hidden_labels": sorted(restricted_labels(role)),
        "restricted_role": role,
        "roles": sorted((_policy().get("roles") or {}).keys()),
    }


def set_hidden_labels(labels: list[str]) -> dict:
    """Replace the restricted role's sensitivity with exactly ``labels`` (+ the
    matching raw_labels). In-memory; resets to the YAML seed on restart."""
    global _ACTIVE, _INDEX
    valid = set(available_labels())
    labs = sorted(l for l in labels if l in valid)
    label_raw = _label_raw()
    raws: list[str] = []
    for l in labs:
        raws += label_raw.get(l, [])
    role = restricted_role()
    pol: dict = {
        "classes": {
            "restricted": {
                "description": "Admin-selected sensitive facts",
                "labels": labs,
                "raw_labels": sorted(set(raws)),
            }
        },
        "roles": {role: {"deny": ["restricted"]}},
        "default_role": default_role(),
    }
    # Preserve other roles (e.g. finance) as deny-nothing so they still exist.
    for name in (_policy().get("roles") or {}):
        pol["roles"].setdefault(name, {"deny": []})
    _ACTIVE = pol
    _INDEX = None
    return policy_state()
