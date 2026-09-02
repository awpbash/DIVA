"""pipeline/kb/field_llm.py — SCHEMA-FIRST extraction of the ops-view fields.

Fills the ops-view fields DIRECTLY with one LLM pass over the document,
instead of DERIVING them from the generic open-vocabulary facts (the
projection/re-map approach that leaks — a field only fills if a hand-tuned matcher
catches the right generic fact, and a blank is ambiguous between "not stated" and
"matcher missed it"). Here the field IS the target: the model reads the whole
contract and assigns a value (or nothing) to each named field, with evidence.

Grounding guard (the Prime Directive — the model NEVER emits coordinates): the
document is rendered with per-block markers ``[pNbM]`` and the model must CITE the
marker(s) it took each value from. We resolve a cited marker back to that block's
real bbox (from ``pages_md``); a snippet re-find is the fallback when the citation
is missing/stale. A value whose evidence resolves to nothing is kept but flagged,
never silently trusted.

Scope: only DOCUMENT-BODY fields (mechanism != external). A field declared
`external` holds a value that lives in another system by design, so the model,
which reads only the document, is never asked for it.

Output ``storage/fields/<doc>.json`` is read back through ``field_extract`` in the
same ``{values, evidence_ok, raw}`` shape the scorer consumes,
so the scorer and the review-record builder consume LLM and derived fills
identically and can be compared on the same gold.

  PYTHONUTF8=1 python -m pipeline.kb.field_llm --doc <id>     # one document
  PYTHONUTF8=1 python -m pipeline.kb.field_llm --all          # every ingested document
  PYTHONUTF8=1 python -m pipeline.kb.field_llm --doc <id> --force   # ignore cache
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import Config, make_async_openai
from ..extraction import render_prompt, token_meter
from ..extraction.loader import CONFIGS_DIR, ConfigError
from ..extraction.prompt_helpers import render_grid
from ..storage import atomic_write_json, fields_dir
from ..ontology import DEFAULT_DOCTYPE
from .opsview_spec import OpsField, OpsView, load as load_view

_log = logging.getLogger(__name__)

# Fields whose value is NOT in the document body. It lives in another system by
# design, so the model, which reads only the document, must not guess at it.
_SKIP_MECHANISMS = {"external"}


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cache_dir(cfg: Config) -> Path:
    # One source of truth for this prefix, including the legacy-name fallback
    # that stops a rename orphaning an existing install's extractions.
    return fields_dir(cfg)


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


# --------------------------------------------------------------------------- #
# Document rendering — clean page markdown with per-block citation markers.
# --------------------------------------------------------------------------- #
def _load_pages(cfg: Config, doc_id: str) -> list[dict]:
    d = cfg.storage_root / "pages_md" / doc_id
    if not d.exists():
        return []
    pages = []
    for p in sorted(d.glob("p_*.json")):
        try:
            pages.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return sorted(pages, key=lambda pg: pg.get("page_no", 0))


def _load_grids(cfg: Config, doc_id: str) -> dict[str, dict]:
    """Recovered table grids, keyed by canonical block id (b0697 …). The pipeline's
    table-repair stage rebuilds the row/column structure the flat OCR loses; feeding
    it as a real markdown table lets the model read a schedule row ("Declared Load |
    550 RT") instead of value soup."""
    d = cfg.storage_root / "table_repair" / doc_id
    out: dict[str, dict] = {}
    if d.exists():
        for p in d.glob("b*.json"):
            try:
                g = (json.loads(p.read_text(encoding="utf-8")) or {}).get("grid")
            except json.JSONDecodeError:
                continue
            if g:
                out[p.stem] = g
    return out


def _render(pages: list[dict], grids: dict[str, dict]
            ) -> tuple[str, dict[str, dict], dict[int, list[dict]]]:
    """Render the document as markered text + a block map for evidence resolution.

    Returns (text, block_map, pages_by_no) where block_map[`pNbM`] = {page_no, bbox,
    text} and pages_by_no[n] = that page's blocks (for the snippet re-find fallback).
    A table block is rendered from its recovered grid (verified 1:1: the k-th block
    overall is canonical id ``b{k+1:04d}``) so schedules read as tables, not soup.
    """
    lines: list[str] = []
    block_map: dict[str, dict] = {}
    pages_by_no: dict[int, list[dict]] = {}
    k = 0                                   # global block index -> canonical id b{k:04d}
    for pg in pages:
        n = int(pg.get("page_no") or 0)
        blocks = pg.get("blocks") or []
        pages_by_no[n] = blocks
        lines.append(f"=== PAGE {n} ===")
        for idx, b in enumerate(blocks):
            k += 1                          # increment for EVERY block to stay aligned
            text = (b.get("text") or "").strip()
            grid = grids.get(f"b{k:04d}")
            if not text and not grid:
                continue
            bid = f"p{n}b{idx}"
            grid_md = "(table)\n" + render_grid(grid) if grid else None
            # Guard against a STALE overlay: table_repair grids are keyed by
            # the reader's global block numbering, and a re-read (RapidOCR ->
            # CU) renumbers every block. A grid that shares no content with
            # the block it would replace belongs to another numbering —
            # overlaying it pastes a table over unrelated text, so the model
            # "sees" (and correctly cites) the table at markers whose rects
            # point at that unrelated text. The raw text stands instead.
            if grid_md and text and _recall(grid_md, text) < 0.3:
                grid_md = None
            if grid_md:
                body = grid_md
            else:
                # Collapse intra-block newlines so one marker == one line the model reads.
                body = text.replace(chr(10), " ")
            bbox = b.get("bbox")
            if bbox:
                # body = what the model actually read for this marker (the grid
                # render for repaired tables) — the citation filter scores
                # against it, not the raw OCR text.
                block_map[bid] = {"page_no": n, "bbox": bbox, "text": text, "body": body}
            lines.append(f"[{bid}] {body}")
    return "\n".join(lines), block_map, pages_by_no


# --------------------------------------------------------------------------- #
# Field catalogue — what the model is asked to fill, with typed hints.
# --------------------------------------------------------------------------- #
def _field_line(f: OpsField) -> str:
    src = f.source or {}
    match = src.get("match") or {}
    if f.type in ("enum", "presence_enum"):
        typ = f"enum[{' | '.join(f.values)}]"
    elif f.type == "value":
        u = src.get("expect_unit")
        typ = "value+unit" + (f" (e.g. {u[0]})" if u else "")
    elif f.type == "number":
        typ = "number"
    else:
        typ = "text"
    # A field's capacity is part of its contract: without it the model treats
    # every field as single-valued and stops at the first (often umbrella) hit.
    if f.multiplicity > 1:
        typ += f" | LIST(up to {f.multiplicity})"

    hints: list[str] = []
    if f.hint:                          # explicit per-field guidance wins
        hints.append(f.hint)
    if src.get("mechanism") == "recital":
        hints.append("from the RECITALS / opening 'WHEREAS' + dating clauses — the "
                     "document's own type, date, and what prior agreement it amends")
    if src.get("mechanism") == "party":
        hints.append(f"the {src.get('role')} — the company/legal entity name")
    for k in ("concept_any", "parameter_any"):
        if match.get(k):
            hints.append("about " + ", ".join(x.replace("_", " ") for x in match[k][:4]))
            break
    if src.get("keywords"):
        hints.append("mentions " + ", ".join(src["keywords"][:6]))
    if src.get("role_map"):
        # The allowed answers come from the field's OWN role map. Naming one
        # domain's three roles here told the model to answer with values that
        # are not in the closed enum printed on the same catalogue line, which
        # costs accuracy on a paid call and is invisible without reading it.
        roles = [str(v) for v in (src["role_map"].values() if
                                  isinstance(src["role_map"], dict)
                                  else src["role_map"]) if str(v).strip()]
        if roles:
            hints.append("who is responsible (" + " / ".join(dict.fromkeys(roles)) + ")")
        else:
            hints.append("who is responsible")
    hint = ("  — " + "; ".join(hints)) if hints else ""
    return f"- {f.full_key} | {f.title} | {typ}{hint}"


# The extraction persona. Per-domain by design, NOT a template with the domain's
# nouns filled in: the guidance that makes extraction accurate is specific
# ("supply vs return, chilled vs cooling water"), and genericising it costs
# accuracy in every domain. So a domain ships its own file and the generic one is
# only a starting point:
#
#   configs/prompts/field_extract.<doctype>.md   the domain's own, if present
#   configs/prompts/field_extract.md             the generic fallback
#
# The first domain's file is byte-identical to the literal that used to live here,
# asserted by tests/extraction/test_second_domain.py, because this module's cache
# is PRESENCE-based and would not re-extract to reveal a regression.


@lru_cache(maxsize=None)
def system_prompt(doctype: str = DEFAULT_DOCTYPE) -> str:
    """The extraction system prompt for one doctype."""
    for name in (f"field_extract.{doctype}", "field_extract"):
        if (CONFIGS_DIR / "prompts" / f"{name}.md").exists():
            return render_prompt(name).rstrip("\n")
    raise ConfigError("no field_extract prompt found in configs/prompts/")


def _schema() -> dict:
    return {
        "name": "field_extraction",
        "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "field": {"type": "string"},
                            "values": {
                                "type": "array",
                                "items": {
                                    "type": "object", "additionalProperties": False,
                                    "properties": {
                                        "value": {"type": "string"},
                                        "blocks": {"type": "array", "items": {"type": "string"}},
                                        "snippet": {"type": "string"},
                                        "page": {"type": "integer"},
                                    },
                                    "required": ["value", "blocks", "snippet", "page"],
                                },
                            },
                        },
                        "required": ["field", "values"],
                    },
                },
            },
            "required": ["fields"],
        },
    }


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20),
       retry=retry_if_exception_type(Exception), reraise=True)
async def _call(client, model: str, focus: str, catalogue: str, doc_text: str) -> dict:
    user = (
        f"## FIELDS TO EXTRACT — {focus} (field_key | title | type | hint)\n"
        "Fill EVERY field below that the document states; omit only the truly absent ones.\n"
        "For LIST fields, return every distinct item as its own value entry — a partial "
        "list is a wrong answer.\n"
        f"{catalogue}\n\n"
        "## DOCUMENT\n"
        "Block markers [pNbM] are per-page block ids you MUST cite as evidence.\n\n"
        f"{doc_text}\n\n"
        "Return JSON {\"fields\":[{\"field\":\"<field_key>\",\"values\":[{\"value\":..,"
        "\"blocks\":[\"pNbM\"],\"snippet\":\"verbatim\",\"page\":N}]}]}. "
        "Only include fields you found a value for; omit not-stated fields."
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_prompt()},
                  {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": _schema()},
        max_completion_tokens=8000,
    )
    token_meter.record(resp.usage, stage="field_extract", model=model)
    if resp.choices[0].finish_reason == "length":
        raise RuntimeError("field extraction hit max_completion_tokens on one category — raise the cap")
    return json.loads(resp.choices[0].message.content or "{}")


# --------------------------------------------------------------------------- #
# Evidence resolution — cited markers -> real bbox rects (model never emits coords).
# --------------------------------------------------------------------------- #
_EV_TOKEN = re.compile(r"[a-z0-9]{2,}")


def _recall(needle: str, haystack: str) -> float:
    """Fraction of the needle's content tokens that appear in the haystack.
    Deliberately loose (token set, not substring): a legit citation survives
    formatting drift ("as Supplier" vs "(as Supplier)"), while an unrelated
    block scores near zero."""
    toks = set(_EV_TOKEN.findall(_norm(needle)))
    if not toks:
        return 1.0
    hay = set(_EV_TOKEN.findall(_norm(haystack)))
    return len(toks & hay) / len(toks)


def _cite_score(v: dict, b: dict) -> float:
    body = b.get("body") or b.get("text") or ""
    return max(_recall(v.get("snippet") or "", body),
               _recall(v.get("value") or "", body))


def _resolve_evidence(v: dict, block_map: dict, pages_by_no: dict) -> tuple[dict, bool]:
    # The model sometimes pads a correct citation with unrelated block ids (a
    # schedule row cited alongside random boilerplate), and every cited id
    # becomes a highlight rect — a junk rect poisons the review screen, and
    # bbox fidelity outranks everything. General rule: when at least one cited
    # block strongly matches the quoted evidence, drop siblings that match
    # neither the quote nor the value. Entries with no strong block keep all
    # their citations — judging those is the human reviewer's job.
    cited = [(block_map[bid], _cite_score(v, block_map[bid]))
             for bid in (v.get("blocks") or []) if bid in block_map]
    if any(s >= 0.6 for _, s in cited):
        cited = [(b, s) for b, s in cited if s >= 0.2]
    rects: list[dict] = []
    seen: set = set()
    for b, _s in cited:
        key = (b["page_no"], tuple(b["bbox"]))
        if key not in seen:
            seen.add(key)
            rects.append({"page_no": b["page_no"], "bbox": b["bbox"]})
    if not rects:                                   # re-find the snippet on its page
        pg = v.get("page")
        snip = _norm(v.get("snippet"))[:60]
        for b in pages_by_no.get(pg, []):
            if snip and snip in _norm(b.get("text")):
                bbox = b.get("bbox")
                if bbox:
                    rects.append({"page_no": pg, "bbox": bbox})
                break
    page = rects[0]["page_no"] if rects else v.get("page")
    ev = {"snippet": (v.get("snippet") or v.get("value") or "")[:240], "page": page,
          "rects": json.dumps(rects) if rects else None,
          "blocks": v.get("blocks") or []}
    return ev, bool(rects)


# Units that appear in any corpus, plus whatever THIS domain's schema declares
# under `source.expect_unit`. The list used to be one domain's units only, and
# because the guard fails open, a domain whose units it had never heard of got
# no evidence check at all: the Prime Directive was silently switched off on
# exactly the fields most likely to be misread.
_BASE_UNIT_TOKENS = (r"°c", r"°f", r"barg", r"\bbar\b", r"kg/cm", r"\bkg\b",
                     r"kwh", r"kw", r"m3", r"m³", r"/h", r"%", r"\bkm\b",
                     r"\bm\b", r"\bmm\b", r"\bhz\b", r"\bva\b")


@lru_cache(maxsize=None)
def _unit_tokens(doctype: str = DEFAULT_DOCTYPE) -> re.Pattern:
    """The unit vocabulary for the evidence-support check, domain included."""
    declared: set[str] = set()
    try:
        for f in load_view(doctype=doctype).fields:
            unit = str((f.source or {}).get("expect_unit") or "").strip().lower()
            if unit:
                declared.add(re.escape(unit))
    except Exception:  # noqa: BLE001 — a config problem must not disable the guard
        pass
    # Longest first so "kwh" is not eaten by "kw".
    parts = sorted(declared | set(_BASE_UNIT_TOKENS), key=len, reverse=True)
    return re.compile("|".join(parts))


def _supports(value: str, evidence: list[dict]) -> bool:
    """A unit-bearing value must have its unit appear in at least one of its cited
    evidence snippets. Otherwise it is a value pinned to an UNRELATED block — a
    likely hallucination (e.g. "7 °C" cited on a rate line "S$0.0241/RTh"). Values
    with no strong unit signal are accepted (the human verifies). This enforces the
    Prime Directive — no confident value beyond what the evidence actually states —
    at extraction time, turning a hallucination into an honest abstention."""
    units = _unit_tokens().findall(_norm(value))
    if not units:
        return True
    return any(units[0] in _norm(e.get("snippet")) for e in evidence)


def _assemble(resp: dict, valid_keys: set, block_map: dict, pages_by_no: dict) -> dict:
    """LLM response -> {field_key: {values:[{value, evidence:[...]}]}}, evidence-resolved.
    Identical value strings under one field are merged (their evidence pooled)."""
    out: dict[str, dict] = {}
    for entry in (resp.get("fields") or []):
        fkey = entry.get("field")
        if fkey not in valid_keys:
            continue                                # ignore hallucinated field keys
        by_value: dict[str, dict] = {}
        for v in (entry.get("values") or []):
            val = (v.get("value") or "").strip()
            if not val or _norm(val) == "not stated":
                continue
            ev, _ = _resolve_evidence(v, block_map, pages_by_no)
            slot = by_value.setdefault(_norm(val), {"value": val, "evidence": []})
            slot["evidence"].append(ev)
        # Keep only values whose own evidence supports them (drops hallucinated
        # unit-values pinned to an unrelated clause — the Prime Directive).
        supported = [s for s in by_value.values() if _supports(s["value"], s["evidence"])]
        if supported:
            out[fkey] = {"values": supported}
    return out


async def _extract_category(client, model, title, fields, doc_text,
                            block_map, pages_by_no, sem) -> dict:
    """One focused call for a single category's fields over the whole document.
    Small, coherent asks complete reliably (a 40-field single shot on a small model
    sometimes gives up partway) and keep the model concentrated on related fields."""
    catalogue = "\n".join(_field_line(f) for f in fields)
    valid = {f.full_key for f in fields}
    async with sem:
        resp = await _call(client, model, title, catalogue, doc_text)
    return _assemble(resp, valid, block_map, pages_by_no)


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


async def extract(cfg: Config, doc_id: str, view: OpsView, gap: bool = True) -> dict:
    pages = _load_pages(cfg, doc_id)
    if not pages:
        raise FileNotFoundError(f"no pages_md for {doc_id} — ingest it first")
    doc_text, block_map, pages_by_no = _render(pages, _load_grids(cfg, doc_id))
    client = make_async_openai(cfg)
    sem = asyncio.Semaphore(4)          # bound concurrency (rate limits)
    model = cfg.reasoning_model

    def call(title, fields):
        return _extract_category(client, model, title, fields, doc_text,
                                 block_map, pages_by_no, sem)

    # Pass 1 — one focused call per category.
    by_cat = view.by_category()
    tasks = []
    for cat, fields in by_cat.items():
        body = [f for f in fields if f.mechanism not in _SKIP_MECHANISMS]
        if body:                        # skip all-external categories
            tasks.append(call(view.category_titles.get(cat, cat), body))
    fields_out: dict[str, dict] = {}
    for r in await asyncio.gather(*tasks):
        fields_out.update(r)

    # Pass 2 — recall sweep: re-ask ONLY the fields still empty, in small focused
    # groups. A second look with far less to juggle recovers values buried in an
    # unexpected section, WITHOUT loosening the "Not Stated is correct, never
    # invent" guard (most empties are genuinely absent, and any new value still
    # has to resolve to real evidence).
    if gap:
        body_all = [f for fields in by_cat.values() for f in fields
                    if f.mechanism not in _SKIP_MECHANISMS]
        empty = [f for f in body_all if f.full_key not in fields_out]
        if empty:
            title = ("SECOND PASS — a first read did NOT find these fields. Look again "
                     "carefully; some are genuinely Not Stated (omit those), but fill any "
                     "the document truly states, with evidence. Do not invent")
            gap_tasks = [call(title, chunk) for chunk in _chunks(empty, 12)]
            for r in await asyncio.gather(*gap_tasks):
                for k, v in r.items():
                    fields_out.setdefault(k, v)     # never overwrite a pass-1 value

        # Pass 3 — list-completeness sweep: a LIST field holding exactly ONE
        # value is the satisficing signature (the umbrella-term trap). Re-ask
        # just those, merging NEW values in (never dropping the pass-1 value).
        sparse = [f for f in body_all
                  if f.multiplicity > 1
                  and len((fields_out.get(f.full_key) or {}).get("values") or []) == 1]
        if sparse:
            title = ("LIST-COMPLETENESS SWEEP — these LIST fields captured only ONE "
                     "value on the first read; contracts usually state several. Re-scan "
                     "the WHOLE document (definitions, schedules, tables, figures) and "
                     "return EVERY distinct item for each field, including the one "
                     "already found. If the document truly states only one, omit the field")
            sweep_tasks = [call(title, chunk) for chunk in _chunks(sparse, 8)]
            for r in await asyncio.gather(*sweep_tasks):
                for k, v in r.items():
                    if k not in fields_out:
                        fields_out[k] = v
                        continue
                    have = {_norm(x["value"]) for x in fields_out[k]["values"]}
                    for slot in v["values"]:
                        if _norm(slot["value"]) not in have:
                            fields_out[k]["values"].append(slot)

    return {"doc_id": doc_id, "model": model, "n_pages": len(pages), "fields": fields_out}


def run(doc_id: str, *, force: bool = False, gap: bool = True) -> dict:
    cfg = Config.load()
    out_dir = _cache_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / f"{doc_id}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A torn cache file would otherwise crash every retry forever.
            _log.warning("corrupt field-extraction cache %s: deleting and re-extracting", cache)
            cache.unlink(missing_ok=True)
    view = load_view()
    rec = asyncio.run(extract(cfg, doc_id, view, gap=gap))
    atomic_write_json(cache, rec)
    return rec


# --------------------------------------------------------------------------- #
# Reader — consumed by the scorer / review-record builder (mirrors _extract).
# --------------------------------------------------------------------------- #
def load(doc_id: str, cfg: Config | None = None) -> dict:
    cfg = cfg or Config.load()
    p = _cache_dir(cfg) / f"{doc_id}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _log.warning("corrupt field-extraction cache %s: returning empty extraction "
                     "(the scorer will grade this doc as unextracted)", p)
        return {}


def field_extract(cache: dict, field: OpsField) -> dict:
    """One field's LLM fill in the ``{values, evidence_ok, raw}`` shape the scorer
    and record builder expect (same contract as score._extract)."""
    entry = (cache.get("fields") or {}).get(field.full_key)
    if not entry:
        return {"values": [], "evidence_ok": False, "raw": []}
    vals: list[str] = []
    raw: list[dict] = []
    ev_ok = False
    for v in entry.get("values") or []:
        vals.append(v["value"])
        evs = v.get("evidence") or []
        raw.append({"value": v["value"], "evidence": evs})
        ev_ok = ev_ok or any(e.get("rects") for e in evs)
    return {"values": vals, "evidence_ok": ev_ok, "raw": raw[:24]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default=None, help="one doc_id")
    ap.add_argument("--all", action="store_true", help="every ingested document")
    ap.add_argument("--force", action="store_true", help="ignore cache, re-extract")
    ap.add_argument("--no-gap", action="store_true", help="skip the second recall pass")
    args = ap.parse_args()

    cfg = Config.load()
    if args.all:
        docs = sorted(p.name for p in (cfg.storage_root / "pages_md").glob("*") if p.is_dir())
    elif args.doc:
        docs = [args.doc]
    else:
        ap.error("pass --doc <id> or --all")
        return

    for d in docs:
        rec = run(d, force=args.force, gap=not args.no_gap)
        n = len(rec.get("fields") or {})
        backed = sum(1 for f in (rec.get("fields") or {}).values()
                     for v in f["values"] if any(e.get("rects") for e in v.get("evidence") or []))
        print(f"  {d}  filled {n:>2} fields  ({backed} evidence-backed values)  -> storage/fields/{d}.json")
    print(f"\nfield extractions → {_cache_dir(cfg)}")


if __name__ == "__main__":
    main()
