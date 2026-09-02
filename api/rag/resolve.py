"""Entity -> document resolution (retrieval scoping).

A corpus of one document type is full of near-identical documents. A question
names a SITE or a PARTY, but the agent has no way to bind that name to the right
document, so unscoped retrieval pulls clauses from whichever near-twin scores
highest and the answer cites the WRONG document. This module closes that gap.

It is deterministic and LLM-free: it builds a vocabulary of the entity names
that exist in the graph (document titles, party names, identity hubs), matches
the question against them on DISTINCTIVE tokens with fuzzy equality (so a
misspelled name still lands), then expands any hit to its whole document FAMILY
via ``Document.group``. The result is the set of doc_ids the turn should be
scoped to. No match -> empty set -> caller falls back to the whole corpus, so
recall is preserved and narrowing only happens when we are confident.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from functools import lru_cache

from pipeline import ontology
from pipeline.store.aio import AsyncCosmosStore

# Words that must NOT count as distinctive identity tokens. Dropping them is
# what stops a question phrased entirely in domain vocabulary from matching
# every document at once.
#
# This half is generic: English question scaffolding, corporate suffixes, and
# the words any document corpus shares. It ships as the default.
_GENERIC_ENGLISH = {
    # question scaffolding and function words
    "and", "the", "for", "with", "between", "under", "what", "who", "when",
    "how", "where", "which", "list", "show", "give", "please", "describe",
    "compare", "comparison", "can", "you", "me", "of", "to", "is", "are", "be",
    "at", "in", "on", "as", "an", "a", "i", "if", "its", "it", "this", "that",
    "most", "recent", "current", "related", "available", "other", "others",
    "total", "involved",
    # corporate suffixes
    "pte", "ltd", "limited", "private", "llp", "llc", "inc", "corp",
    "corporation", "company", "co",
    # words shared by any document corpus
    "agreement", "contract", "contracts", "document", "documents", "file",
    "files", "link", "links", "section", "clause", "schedule", "part", "page",
    "table", "letter", "notice", "first", "second", "third",
    "condition", "conditions", "requirement", "requirements", "scope",
    "terminate", "termination", "provide", "providing", "responsible",
}

# The other half is domain vocabulary, and it belongs to the domain. It is
# declared in configs/ontology/<domain>.yaml under `identity.generic_tokens`.
# A domain that declares none simply gets the generic set, which still works,
# just with less aggressive narrowing.


@lru_cache(maxsize=None)
def generic_tokens(doctype: str = ontology.DEFAULT_DOCTYPE) -> frozenset[str]:
    """The stopword set for identity matching: generic English plus whatever
    the active domain declares. Cached, since it is read on every question."""
    try:
        ident = (ontology.load_ontology(doctype) or {}).get("identity") or {}
    except (FileNotFoundError, OSError):
        ident = {}
    declared = ident.get("generic_tokens") or []
    # legal_suffixes is already declared for party-name normalisation and is
    # never distinctive, so fold it in rather than making domains list it twice.
    suffixes = ident.get("legal_suffixes") or []
    extra = {tok
             for entry in (*declared, *suffixes)
             for tok in str(entry).lower().split()}
    return frozenset(_GENERIC_ENGLISH | extra)


@lru_cache(maxsize=None)
def canonical_roles(doctype: str = ontology.DEFAULT_DOCTYPE) -> tuple[str, ...]:
    """The PRINCIPAL party roles for this domain, from
    `identity.canonical_roles` in its ontology.

    These are the parties a document is between, as opposed to the signatories,
    witnesses and notice contacts it also names. Question scoping and the
    corpus catalog want the principals only, and both used to name one
    domain's two roles as a literal, so on any other domain they matched no
    party at all and quietly fell back to matching on titles alone.

    A domain that declares none gets every role the schema knows, which is
    noisier but never wrong.
    """
    try:
        ident = (ontology.load_ontology(doctype) or {}).get("identity") or {}
    except (FileNotFoundError, OSError):
        ident = {}
    declared = [str(r).strip().lower() for r in (ident.get("canonical_roles") or [])
                if str(r).strip()]
    if declared:
        return tuple(declared)
    from pipeline.kb.opsview_spec import party_roles
    return tuple(sorted(party_roles(doctype)))


@dataclass
class ScopeMatch:
    entity: str          # the matched vocabulary name (display)
    kind: str            # 'doc' | 'party' | 'site'
    score: float         # fraction of the entity's distinctive tokens matched
    doc_ids: list[str]


@dataclass
class ScopeResult:
    doc_ids: list[str] = field(default_factory=list)   # [] = no confident match
    matches: list[ScopeMatch] = field(default_factory=list)
    reason: str = ""
    # Zero-match only: the closest known entity names (fuzzy candidates below
    # the acceptance threshold). Fuel for a "did you mean" hint, never scoping.
    near_misses: list[str] = field(default_factory=list)

    @property
    def scoped(self) -> bool:
        return bool(self.doc_ids)


_WORD = re.compile(r"[A-Za-z0-9]+")
_LEGAL_SUFFIX = {"pte", "ltd", "limited", "private", "llp", "llc", "inc",
                 "incorporated", "corp", "corporation", "co"}


def _tokens(text: str | None) -> list[str]:
    """Distinctive lowercase tokens: alnum, len>=3, not generic, not all-digits."""
    generic = generic_tokens()
    out: list[str] = []
    for w in _WORD.findall((text or "").lower()):
        if len(w) < 3 or w.isdigit() or w in generic:
            continue
        out.append(w)
    return out


def _full_name(text: str | None) -> str:
    """Normalised full name with legal suffixes dropped — the fallback key for
    entities whose every token is generic ('Cooling Services Pte Ltd' ->
    'cooling services'). Lets a fully-generic client name still resolve by an
    exact phrase match in the question when distinctive-token matching can't."""
    toks = [w for w in _WORD.findall((text or "").lower())
            if w not in _LEGAL_SUFFIX]
    return " ".join(toks)


def _normalized_question(text: str | None) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def _token_match(needle: str, hay: set[str]) -> bool:
    """Does an entity token ``needle`` match any question token in ``hay``?
    Exact, or substring (len>=4), or a close fuzzy match (typo) sharing the
    first character. That last clause is what survives a transposed typo, so a
    misspelled entity name still lands on the right one."""
    if needle in hay:
        return True
    # ID-like tokens (anything carrying a digit: BR017, CA1C006) match
    # EXACTLY or not at all — 'br013' and 'br017' are different blocks,
    # not typo variants of one name.
    if any(ch.isdigit() for ch in needle):
        return False
    for h in hay:
        # substring only when the SHORTER token is >=5 chars — otherwise short
        # generic-ish stems collide ("tech" in "technologies" would wrongly
        # bind "Northwind Tech Park" to "Harbour Point Technologies").
        if min(len(needle), len(h)) >= 5 and (needle in h or h in needle):
            return True
        if (len(needle) >= 5 and len(h) >= 5 and needle[0] == h[0]
                and difflib.SequenceMatcher(None, needle, h).ratio() >= 0.80):
            return True
    return False


# Near-miss band floor: a question token this close to an entity token (but
# below _token_match's 0.80 acceptance) reads as an attempted NAME, not a
# stray topic word. Corpus name ratios against ordinary question words sit
# well under this in practice: two unrelated entity names score about 0.48.
_NEAR_MISS_FLOOR = 0.60


def _near_miss_names(entries: list[dict], q_tokens: set[str],
                     k: int = 3) -> list[str]:
    """Closest entity names when nothing matched: best fuzzy ratio between
    any distinctive question token and any entity token. Names only, deduped,
    top ``k``, surfaced as a "did you mean" hint by the chat layer. ID-like
    tokens are skipped (they match exactly or not at all, same rule as
    _token_match)."""
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for e in entries:
        best = 0.0
        for t in e.get("tokens") or []:
            if any(ch.isdigit() for ch in t):
                continue
            for h in q_tokens:
                if len(t) < 4 or len(h) < 4 or any(ch.isdigit() for ch in h):
                    continue
                r = difflib.SequenceMatcher(None, t, h).ratio()
                if r > best:
                    best = r
        name = str(e.get("name") or "")
        if best >= _NEAR_MISS_FLOOR and name and name.lower() not in seen:
            seen.add(name.lower())
            scored.append((best, name))
    scored.sort(key=lambda p: (-p[0], p[1]))
    return [n for _, n in scored[:k]]


def unresolved_scope_note(result: ScopeResult) -> str:
    """The explicit instruction chat.py injects into the agent + synth
    context when a question NAMED something and resolution matched zero
    documents. Empty when resolution succeeded or when no known name comes
    close (a generic unscoped question falls back to the whole corpus with
    no warning, recall preserved)."""
    if result.scoped or not result.near_misses:
        return ""
    names = ", ".join(result.near_misses)
    return (
        "SCOPE WARNING: the entity named in this question matched no "
        "document in the corpus. Do not borrow clauses from other contracts "
        "to answer it. State plainly that it is not in the documents. The "
        f"closest names on file are: {names}. Offer these to the user as a "
        '"did you mean" suggestion.'
    )


def _evaluate(entity_tokens: list[str], q_tokens: set[str]) -> tuple[bool, float]:
    """(hit, score). A hit needs a strong distinctive token (len>=5) matched, or
    >=2 distinctive tokens matched — enough to bind an identity, not a stray word."""
    if not entity_tokens:
        return False, 0.0
    matched = [t for t in entity_tokens if _token_match(t, q_tokens)]
    n = len(matched)
    strong = any(len(t) >= 5 for t in matched)
    hit = bool(strong or n >= 2)
    return hit, n / len(entity_tokens)


async def _load_vocabulary(store: AsyncCosmosStore) -> tuple[list[dict], dict[str, list[str]]]:
    """Return (entries, group_map). entries: {kind,name,doc_ids,tokens};
    group_map: group_name -> all doc_ids in that family. Three sources:
    document titles, party facts, and the identity hubs a document's facts
    resolve to (via the RESOLVES_TO edge items, whose pk IS the citing doc).

    There used to be a fourth, an external register of customers and assets
    that an admin maintained alongside the corpus. It was one deployment's
    master data rather than part of the framework and has been retired.
    """
    rows: list[dict] = []
    group_map: dict[str, list[str]] = {}
    for d in await store.query(
            "SELECT c.doc_id, c.title, c['group'] AS grp FROM c "
            "WHERE c.kind = 'document'"):
        rows.append({"kind": "doc", "doc_id": d["doc_id"],
                     "name": d.get("title"), "grp": d.get("grp")})
    for p in await store.query(
            "SELECT c.doc_id, c['name'] AS name FROM c WHERE c.kind = 'fact' "
            "AND c.label = 'Party' AND ARRAY_CONTAINS(@roles, c.role) "
            "AND IS_DEFINED(c['name']) AND c['name'] != ''",
            [{"name": "@roles", "value": list(canonical_roles())}]):
        rows.append({"kind": "party", "doc_id": p["doc_id"],
                     "name": p.get("name"), "grp": None})
    # Identity hubs, whatever this domain calls them. Naming the hub label here
    # is how a question about a site used to bind to its documents, and it bound
    # to nothing on a domain whose hubs are named differently. Every hub item
    # carries `key` (its normalised value) plus a display name when the writer
    # had one, so both are usable identity tokens.
    hub_edges = await store.query(
        "SELECT c.pk, c.tgt FROM c WHERE c.kind = 'edge' AND c.rel = 'RESOLVES_TO'")
    if hub_edges:
        hubs = {h["id"]: h for h in await store.query(
            "SELECT c.id, c.hub, c['key'] AS hub_key, c['name'] AS hub_name, "
            "c.address, c.term FROM c WHERE c.kind = 'hub'", pk="global")}
        for e in hub_edges:
            hub = hubs.get(e["tgt"]) or {}
            names = {hub.get("hub_name"), hub.get("address"), hub.get("term"),
                     hub.get("hub_key") or e["tgt"].split(":", 1)[1]}
            for name in names:
                if name:
                    rows.append({"kind": "entity", "doc_id": e["pk"],
                                 "name": str(name), "grp": None})
    # group_map from the doc rows
    for r in rows:
        if r["kind"] == "doc" and r.get("grp"):
            group_map.setdefault(r["grp"], [])
            if r["doc_id"] not in group_map[r["grp"]]:
                group_map[r["grp"]].append(r["doc_id"])
    # collapse to entries keyed by (kind, normalized name) so a party named in
    # several docs becomes one entry spanning those docs. Keep entries with NO
    # distinctive tokens too (all-generic names) — they resolve via the
    # full-name fallback instead of being dropped.
    by_key: dict[tuple, dict] = {}
    for r in rows:
        name = r.get("name") or ""
        toks = _tokens(name)
        full = _full_name(name)
        if not toks and len(full.split()) < 2:
            continue  # nothing distinctive and no multi-word phrase to match on
        key = (r["kind"], " ".join(toks) or full)
        e = by_key.setdefault(key, {"kind": r["kind"], "name": name,
                                    "tokens": toks, "full": full, "doc_ids": []})
        if r["doc_id"] not in e["doc_ids"]:
            e["doc_ids"].append(r["doc_id"])
    return list(by_key.values()), group_map


async def resolve_scope(
    store: AsyncCosmosStore, question: str, key_terms: list[str] | None = None,
) -> ScopeResult:
    """Resolve the entities named in ``question`` to the contract documents
    they belong to (family-expanded). Empty doc_ids => no confident match."""
    haystack = question + " " + " ".join(key_terms or [])
    q_tokens = set(_tokens(haystack))
    q_norm = f" {_normalized_question(haystack)} "
    if not q_tokens and not q_norm.strip():
        return ScopeResult(reason="no distinctive tokens in question")
    entries, group_map = await _load_vocabulary(store)

    matches: list[ScopeMatch] = []
    for e in entries:
        hit, score = _evaluate(e["tokens"], q_tokens)
        # Fallback for all-generic names: exact multi-word phrase in the question.
        if not hit:
            full = e.get("full") or ""
            if len(full.split()) >= 2 and f" {full} " in q_norm:
                hit, score = True, 1.0
        if hit:
            matches.append(ScopeMatch(entity=e["name"], kind=e["kind"],
                                      score=round(score, 2), doc_ids=list(e["doc_ids"])))
    if not matches:
        return ScopeResult(reason="no entity matched",
                           near_misses=_near_miss_names(entries, q_tokens))

    # union doc_ids, then expand each to its full family group
    seed: list[str] = []
    for m in matches:
        for d in m.doc_ids:
            if d not in seed:
                seed.append(d)
    # map doc_id -> group for expansion
    doc_group = {d: g for g, ds in group_map.items() for d in ds}
    expanded: list[str] = []
    for d in seed:
        for dd in group_map.get(doc_group.get(d, ""), [d]):
            if dd not in expanded:
                expanded.append(dd)

    matches.sort(key=lambda m: (-m.score, m.kind))
    top = ", ".join(f"{m.entity}({m.kind})" for m in matches[:4])
    reason = f"matched {len(matches)} entity name(s): {top}; scoped to {len(expanded)} doc(s)"
    return ScopeResult(doc_ids=expanded, matches=matches, reason=reason)
