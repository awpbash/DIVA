"""pack.py — compile a Doctype Pack into runtime objects (KB rebuild, Phase A).

A Doctype Pack (``configs/packs/<doctype>.yaml`` + the inherited
``_base.yaml``) is the single source of truth spanning the SEAM between the two
halves of the rebuild:

    PART 1 — NORMALISE (per-doc)   │   PART 2 — GRAPH (cross-doc, deterministic)
    raw_labels / shape / bundling  │   label / properties / edges / hubs / pivots

This module loads + merges + VALIDATES a pack and exposes the compiled objects
the rest of the system consumes — replacing the hardcoded dicts that today
live in ``load.py`` (CATEGORY_TO_LABEL) and ``tools.py`` (the per-label
filterable / summary / id-key maps).

Validation is the Phase-A DRIFT GATE. A pack that is internally inconsistent —
a filterable prop that isn't declared, an edge endpoint that isn't a label, a
pivot whose hub isn't pivotable, a measure whose parameter field is missing —
fails to compile, and the error lists EVERY problem at once. The complementary
pack <-> live-graph direction is enforced in Phase B, once the loader exists
and writes nodes/edges we can read back.

This module is deliberately self-contained (its own YAML load + merge) so it
does not touch the legacy ``pipeline/ontology.py`` path that still drives the
running system during the rebuild.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from ..ontology import DEFAULT_DOCTYPE

PACKS_DIR = Path(__file__).resolve().parents[2] / "configs" / "packs"


class PackError(ValueError):
    """A pack failed validation. The message lists every problem found (not
    just the first) so the drift gate surfaces all drift in one shot."""


# ---------------------------------------------------------------------------
# Raw load + `extends` merge
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursive dict merge; ``over`` (the doctype pack) wins. Lists and
    scalars replace wholesale — only mappings merge key-by-key."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_pack_dict(doctype: str) -> dict:
    """Load ``<doctype>.yaml`` and, if it declares ``extends: <base>``, merge
    the base pack underneath it. Returns a fresh dict each call (safe to
    mutate — the drift test relies on that)."""
    path = PACKS_DIR / f"{doctype}.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base_name = doc.get("extends")
    if base_name:
        base = yaml.safe_load(
            (PACKS_DIR / f"{base_name}.yaml").read_text(encoding="utf-8")
        ) or {}
        doc = _deep_merge(base, doc)
    return doc


# ---------------------------------------------------------------------------
# Compiled value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactType:
    """One ``fact_types`` entry — drives both halves of the seam."""
    category: str
    label: Optional[str]                 # None = edge-only (e.g. reference)
    key: Optional[str]
    raw_labels: tuple[str, ...]
    shape: str
    bundling: str
    properties: tuple[str, ...]
    indexed: tuple[str, ...]
    filterable: tuple[str, ...]
    summary: tuple[str, ...]
    role_join: Optional[dict]
    measure: Optional[dict]
    load: Optional[dict]                 # declarative normalised → node-prop map
    required: tuple[str, ...]            # normalised keys the repair pass backfills if null


@dataclass(frozen=True)
class DerivedEdge:
    name: str
    from_labels: tuple[str, ...]
    to_labels: tuple[str, ...]
    method: str                          # co_location | role_join | text_match
    tiers: tuple[str, ...]               # co_location precision tiers
    on: Optional[str]                    # role_join: the role field to join on
    provenance: str


@dataclass(frozen=True)
class Assertable:
    name: str
    from_labels: tuple[str, ...]
    to_labels: tuple[str, ...]
    llm_description: str
    evidence: str
    insert: dict


@dataclass(frozen=True)
class Hub:
    name: str
    from_label: Optional[str]
    key: Optional[str]
    edge: Optional[str]
    pivotable: bool
    role_scoped: bool


@dataclass(frozen=True)
class Pack:
    """The compiled pack. Accessors mirror what ``tools.py`` / ``load.py``
    used to hardcode."""
    doctype: str
    raw: dict
    fact_types: dict[str, FactType]
    by_label: dict[str, dict]            # label -> merged node config
    category_to_label: dict[str, str]
    raw_label_to_category: dict[str, Optional[str]]
    known_categories: frozenset[str]
    fact_labels: frozenset[str]
    structural_labels: frozenset[str]
    structural_edges: frozenset[str]
    derived_edges: dict[str, DerivedEdge]
    assertable: dict[str, Assertable]
    provenance_tiers: dict[str, dict]
    hubs: dict[str, Hub]
    aggregation: dict
    # Display grouping for the graph explorer's legend, served by
    # GET /graph/legend. `ui_groups` is the ordered group list, `ui_group_for`
    # maps every label the graph can emit to one of those group keys.
    ui_groups: tuple[dict, ...] = ()
    ui_group_for: dict[str, str] = field(default_factory=dict)
    # Structural node label -> its primary-key property. `by_label` covers only
    # fact labels, and the graph API has to key Document / Agreement / Section
    # nodes too.
    node_keys: dict[str, str] = field(default_factory=dict)

    # ----- retrieval accessors (per graph node label) -----
    def id_key(self, label: str) -> Optional[str]:
        return self.by_label[label]["key"]

    def properties(self, label: str) -> frozenset[str]:
        return self.by_label[label]["properties"]

    def filterable(self, label: str) -> frozenset[str]:
        return self.by_label[label]["filterable"]

    def summary_keys(self, label: str) -> tuple[str, ...]:
        return self.by_label[label]["summary"]

    def indexed(self, label: str) -> frozenset[str]:
        return self.by_label[label]["indexed"]

    # ----- aggregation surface -----
    def pivots(self) -> list[dict]:
        return list(self.aggregation.get("pivots") or [])

    def measures(self) -> list[dict]:
        return list(self.aggregation.get("measures") or [])


# ---------------------------------------------------------------------------
# Compile + validate
# ---------------------------------------------------------------------------


def compile_mapping(doctype: str, doc: dict) -> Pack:
    """Compile an already-merged pack mapping into a ``Pack``. Raises
    ``PackError`` listing every inconsistency. Separated from file IO so the
    drift test can feed deliberately-broken mappings."""
    errors: list[str] = []

    structural = doc.get("structural") or {}
    node_types = structural.get("node_types") or {}
    shared_label = ((structural.get("fact_node") or {}).get("shared_label")) or "Fact"
    structural_labels = set(node_types) | {shared_label}
    structural_edges = set((structural.get("edges") or {}).keys())
    provenance_tiers = doc.get("provenance_tiers") or {}

    # ----- fact types -----
    load_maps = doc.get("load_maps") or {}
    fact_types: dict[str, FactType] = {}
    for cat, spec in (doc.get("fact_types") or {}).items():
        spec = spec or {}
        fact_types[cat] = FactType(
            category=cat,
            label=spec.get("label"),
            key=spec.get("key"),
            raw_labels=tuple(spec.get("raw_labels") or ()),
            shape=str(spec.get("shape") or ""),
            bundling=str(spec.get("bundling") or ""),
            properties=tuple(spec.get("properties") or ()),
            indexed=tuple(spec.get("indexed") or ()),
            filterable=tuple(spec.get("filterable") or ()),
            summary=tuple(spec.get("summary") or ()),
            role_join=spec.get("role_join"),
            measure=spec.get("measure"),
            load=load_maps.get(cat),
            required=tuple(spec.get("required") or ()),
        )

    category_to_label = {c: ft.label for c, ft in fact_types.items() if ft.label}
    known_categories = frozenset(fact_types)
    fact_labels = frozenset(ft.label for ft in fact_types.values() if ft.label)

    # raw_label -> category (must be a 1:1 routing)
    raw_label_to_category: dict[str, Optional[str]] = {}
    for c, ft in fact_types.items():
        for rl in ft.raw_labels:
            prev = raw_label_to_category.get(rl)
            if prev is not None and prev != c:
                errors.append(f"raw_label {rl!r} routes to both {prev!r} and {c!r}")
            raw_label_to_category[rl] = c
    for rl in (doc.get("dropped_raw_labels") or []):
        if rl in raw_label_to_category:
            errors.append(
                f"dropped_raw_label {rl!r} also routes to "
                f"{raw_label_to_category[rl]!r}"
            )
        raw_label_to_category[rl] = None

    # ----- merge per-label node config (organization + person -> Party) -----
    by_label: dict[str, dict] = {}
    for ft in fact_types.values():
        if not ft.label:
            continue
        agg = by_label.setdefault(
            ft.label,
            {"key": ft.key, "properties": set(), "indexed": set(),
             "filterable": set(), "summary": []},
        )
        if ft.key and agg["key"] and ft.key != agg["key"]:
            errors.append(
                f"label {ft.label!r} has conflicting keys "
                f"{agg['key']!r} vs {ft.key!r}"
            )
        agg["properties"].update(ft.properties)
        agg["indexed"].update(ft.indexed)
        agg["filterable"].update(ft.filterable)
        for k in ft.summary:                 # summary order matters for display
            if k not in agg["summary"]:
                agg["summary"].append(k)

    # ----- per-fact-type consistency (props superset of its projections) -----
    for ft in fact_types.values():
        if not ft.label:
            continue
        props = by_label[ft.label]["properties"]
        if not ft.key:
            errors.append(f"{ft.category}: missing key")
        elif ft.key not in props:
            errors.append(f"{ft.category}: key {ft.key!r} not in properties")
        for grp in ("indexed", "filterable", "summary"):
            missing = set(getattr(ft, grp)) - props
            if missing:
                errors.append(
                    f"{ft.category}.{grp}: {sorted(missing)} not in properties"
                )
        if ft.measure:
            for fld in ("value_field", "unit_field", "parameter_field", "bound_field"):
                v = ft.measure.get(fld)
                if v and v not in props:
                    errors.append(
                        f"{ft.category}.measure.{fld}={v!r} not in properties"
                    )
        if ft.role_join:
            rf = ft.role_join.get("role_field")
            to = ft.role_join.get("to")
            if rf not in props:
                errors.append(f"{ft.category}.role_join.role_field {rf!r} not in properties")
            if to not in fact_labels:
                errors.append(f"{ft.category}.role_join.to {to!r} not a fact label")
        if ft.load:
            # Every node-property a load-map writes must be a declared property
            # — else the loader would set a field that the schema doesn't know.
            targets: set[str] = set()
            for k in ("rename", "coalesce", "alias", "normalized"):
                targets |= set((ft.load.get(k) or {}).keys())
            if ft.load.get("name"):
                targets.add("name")
            missing = targets - props
            if missing:
                errors.append(
                    f"{ft.category}.load writes undeclared properties: {sorted(missing)}"
                )

    # ----- derived edges -----
    known_labels = set(fact_labels) | structural_labels
    derived_edges: dict[str, DerivedEdge] = {}
    for name, spec in ((doc.get("edges") or {}).get("derived") or {}).items():
        spec = spec or {}
        derive = spec.get("derive") or {}
        frm = tuple(spec.get("from") or ())
        to = tuple(spec.get("to") or ())
        prov = spec.get("provenance") or ""
        derived_edges[name] = DerivedEdge(
            name=name, from_labels=frm, to_labels=to,
            method=str(derive.get("method") or ""),
            tiers=tuple(derive.get("tiers") or ()),
            on=derive.get("on_field"), provenance=prov,
        )
        for lab in frm + to:
            if lab not in known_labels:
                errors.append(f"edge {name}: endpoint {lab!r} is not a declared label")
        if prov not in provenance_tiers:
            errors.append(f"edge {name}: provenance {prov!r} not a declared tier")
        # Co-location tiers must be from the known set — guards against a
        # typo'd / removed tier (e.g. the dropped `same_unit`) that the derive
        # engine would silently skip, leaving the recall tier dark.
        if str(derive.get("method")) == "co_location":
            for tier in (derive.get("tiers") or []):
                if tier not in ("same_block", "same_section", "cross_ref"):
                    errors.append(f"edge {name}: unknown co_location tier {tier!r}")

    # role_join edges must be declared derived edges
    for ft in fact_types.values():
        if ft.role_join:
            e = ft.role_join.get("edge")
            if e not in derived_edges:
                errors.append(
                    f"{ft.category}.role_join.edge {e!r} is not a derived edge"
                )

    # ----- assertable menu (fact-to-fact, evidence-bearing, tiered) -----
    assertable: dict[str, Assertable] = {}
    for name, spec in (doc.get("assertable") or {}).items():
        spec = spec or {}
        frm = tuple(spec.get("from") or ())
        to = tuple(spec.get("to") or ())
        assertable[name] = Assertable(
            name=name, from_labels=frm, to_labels=to,
            llm_description=str(spec.get("llm_description") or ""),
            evidence=str(spec.get("evidence") or ""),
            insert=spec.get("insert") or {},
        )
        for lab in frm + to:
            if lab not in fact_labels:
                errors.append(f"assertable {name}: {lab!r} not a fact label")
        if not str(spec.get("llm_description") or "").strip():
            errors.append(f"assertable {name}: empty llm_description")
        ins = spec.get("insert") or {}
        if "min_confidence" not in (ins.get("asserted_llm") or {}):
            errors.append(f"assertable {name}: insert.asserted_llm.min_confidence missing")
        els = ins.get("else")
        if els and els not in provenance_tiers:
            errors.append(f"assertable {name}: insert.else {els!r} not a declared tier")

    # ----- identity hubs -----
    hubs: dict[str, Hub] = {}
    for name, spec in ((doc.get("identity") or {}).get("hubs") or {}).items():
        spec = spec or {}
        hubs[name] = Hub(
            name=name, from_label=spec.get("from"), key=spec.get("key"),
            edge=spec.get("edge"), pivotable=bool(spec.get("pivotable")),
            role_scoped=bool(spec.get("role_scoped")),
        )
        if spec.get("from") not in fact_labels:
            errors.append(f"hub {name}: from {spec.get('from')!r} not a fact label")

    # ----- aggregation surface (pivots × measures) -----
    aggregation = doc.get("aggregation") or {}
    for p in (aggregation.get("pivots") or []):
        h = (p or {}).get("hub")
        if h not in hubs:
            errors.append(f"aggregation pivot hub {h!r} not a declared hub")
        elif not hubs[h].pivotable:
            errors.append(f"aggregation pivot hub {h!r} is not pivotable")
    for m in (aggregation.get("measures") or []):
        m = m or {}
        lab, pf = m.get("label"), m.get("parameter_field")
        same = [ft for ft in fact_types.values() if ft.label == lab]
        if not same:
            errors.append(f"aggregation measure label {lab!r} has no fact_type")
        elif not any(ft.measure for ft in same):
            errors.append(f"aggregation measure label {lab!r} has no measure block")
        if lab in by_label and pf and pf not in by_label[lab]["properties"]:
            errors.append(f"aggregation measure {lab}.{pf} not in properties")

    # ----- display grouping (the graph explorer's legend) -----
    # Structural node types name their own group; the two that VARY per domain
    # are filled from the pack itself, which is the whole point: a domain with
    # different fact types or hubs gets a correct legend for free.
    ui_groups = tuple({"key": str((g or {}).get("key") or ""),
                       "label": str((g or {}).get("label") or ""),
                       "hint": str((g or {}).get("hint") or "")}
                      for g in (structural.get("ui_groups") or []))
    group_keys = {g["key"] for g in ui_groups}
    ui_group_for: dict[str, str] = {}
    node_keys = {lab: (spec or {}).get("key") for lab, spec in node_types.items()
                 if (spec or {}).get("key")}
    for lab, spec in node_types.items():
        grp = (spec or {}).get("ui_group")
        if grp is None:
            errors.append(f"node type {lab}: no ui_group (the legend cannot place it)")
        elif grp not in group_keys:
            errors.append(f"node type {lab}: ui_group {grp!r} is not a declared group")
        else:
            ui_group_for[lab] = grp
    for lab in (*fact_labels, shared_label):
        ui_group_for[lab] = "fact"
    for name in hubs:
        ui_group_for[name] = "identity"
    for required in ("fact", "identity"):
        if ui_groups and required not in group_keys:
            errors.append(f"ui_groups must declare a {required!r} group")

    if errors:
        raise PackError(
            f"pack {doctype!r} failed validation:\n  - " + "\n  - ".join(errors)
        )

    # freeze the per-label config
    by_label_frozen = {
        lab: {
            "key": v["key"],
            "properties": frozenset(v["properties"]),
            "indexed": frozenset(v["indexed"]),
            "filterable": frozenset(v["filterable"]),
            "summary": tuple(v["summary"]),
        }
        for lab, v in by_label.items()
    }
    return Pack(
        doctype=doctype, raw=doc, fact_types=fact_types, by_label=by_label_frozen,
        category_to_label=category_to_label,
        raw_label_to_category=raw_label_to_category,
        known_categories=known_categories, fact_labels=fact_labels,
        structural_labels=frozenset(structural_labels),
        structural_edges=frozenset(structural_edges),
        derived_edges=derived_edges, assertable=assertable,
        provenance_tiers=provenance_tiers, hubs=hubs, aggregation=aggregation,
        ui_groups=ui_groups, ui_group_for=ui_group_for,
        node_keys=node_keys,
    )


@lru_cache(maxsize=None)
def load(doctype: str = DEFAULT_DOCTYPE) -> Pack:
    """Load, merge, and compile a doctype pack (cached)."""
    return compile_mapping(doctype, load_pack_dict(doctype))
