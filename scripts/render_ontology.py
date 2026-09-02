"""render_ontology.py — draw the declared ontology as Mermaid diagrams.

Reads configs/ontology/<doctype>.yaml (the single source of truth) and emits
docs/ontology_graph.md with two views:

  1. Architecture & data flow — layers as subgraphs; the 15 typed fact labels
     collapsed into one FACTS node so the backbone (layout -> fact -> evidence
     -> identity) reads cleanly. Solid edges = loader; dashed = non-loader.
  2. Fact graph — semantic edges between the SPECIFIC fact types
     (IMPOSED_ON, CONDITIONED_ON, LIMITED_BY, RESOLVES_TO, ...).

Because it's generated from the YAML, the picture never drifts from the
schema. Re-run after any ontology edit::

    python -m scripts.render_ontology
"""
from __future__ import annotations

import sys

from pipeline import ontology
from pipeline.storage import Paths  # noqa: F401  (kept for path conventions)
from pipeline.config import Config


# Layer -> mermaid fill colour (light, readable on white).
_LAYER_FILL = {
    "document":   "#e3f2fd",
    "layout":     "#e8f5e9",
    "fact":       "#fce4ec",
    "provenance": "#f3e5f5",
    "evidence":   "#e0f7fa",
    "identity":   "#ede7f6",
    "quarantine": "#ffebee",
}

_FACTS = "FACTS"  # collapsed fact-layer node id used in view 1


def _relationship_pairs(ont: dict, name: str) -> set[tuple[str, str]]:
    return ontology.relationship_endpoint_pairs(ont, name)


def _layer_of(ont: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for layer, labels in ont["layers"].items():
        for lab in labels:
            out[lab] = layer
    return out


def _arrow(prov: str) -> str:
    """Solid for loader-written edges, dashed for non-loader semantic edges."""
    return "-->" if prov == "loader" else "-.->"


def _view_architecture(ont: dict) -> str:
    rels = ont["relationships"]
    facts = set(ont["layers"]["fact"])

    def collapse(label: str) -> str:
        return _FACTS if label in facts else label

    # Collect edges, collapsing the fact layer to a single node.
    edges: dict[tuple[str, str, str], str] = {}  # (src,dst,label) -> arrow
    for name, spec in rels.items():
        prov = spec.get("provenance", "loader")
        for f, t in _relationship_pairs(ont, name):
            s, d = collapse(f), collapse(t)
            if s == _FACTS and d == _FACTS:
                continue  # fact-to-fact belongs to view 2
            label = name
            if s == "Agreement" and d == _FACTS and name.startswith("HAS_"):
                label = "HAS_* (attach)"   # collapse 15 attachment edges
            edges[(s, d, label)] = _arrow(prov)

    lines = ["flowchart TB"]
    # Subgraphs per layer (fact layer -> the single FACTS node).
    for layer, labels in ont["layers"].items():
        lines.append(f'  subgraph {layer}["{layer} layer"]')
        if layer == "fact":
            inside = " · ".join(labels)
            lines.append(f'    {_FACTS}["15 typed facts<br/>{inside}"]')
        else:
            for lab in labels:
                lines.append(f'    {lab}["{lab}"]')
        lines.append("  end")
    # Edges.
    for (s, d, label), arrow in sorted(edges.items()):
        lines.append(f"  {s} {arrow}|{label}| {d}")
    # Colours.
    for layer, fill in _LAYER_FILL.items():
        if layer not in ont["layers"]:
            continue  # layer declared no labels (e.g. retired retrieval tier)
        members = [_FACTS] if layer == "fact" else ont["layers"][layer]
        lines.append(f"  classDef {layer}Cls fill:{fill},stroke:#555,color:#111;")
        lines.append(f"  class {','.join(members)} {layer}Cls;")
    return "\n".join(lines)


def _view_fact_graph(ont: dict) -> str:
    rels = ont["relationships"]
    facts = set(ont["layers"]["fact"])

    # Semantic edges; expand specific-domain edges, collapse all-fact fans.
    edges: dict[tuple[str, str, str], str] = {}
    nodes: set[str] = set()
    for name, spec in rels.items():
        prov = spec.get("provenance", "loader")
        if not (prov.startswith("derived") or prov.startswith("asserted")):
            continue
        frm = set(spec["from"])
        fan = frm == facts                      # e.g. USES_TERM from all 15
        if fan and not spec.get("pairs"):
            pairs = {(_FACTS, d) for d in spec["to"]}
        else:
            pairs = _relationship_pairs(ont, name)
        for s, d in sorted(pairs):
            edges[(s, d, name)] = _arrow(prov)
            nodes.add(s)
            nodes.add(d)

    lines = ["flowchart LR"]
    if _FACTS in nodes:
        lines.append(f'  {_FACTS}["any fact"]')
        nodes.discard(_FACTS)
    for n in sorted(nodes):
        lines.append(f'  {n}["{n}"]')
    for (s, d, label), arrow in sorted(edges.items()):
        lines.append(f"  {s} {arrow}|{label}| {d}")
    return "\n".join(lines)


def _view_relation_proposals(ont: dict) -> str:
    specs = ontology.relation_proposal_specs(ont)
    if not specs:
        return "No LLM relation-proposal menu declared.\n"
    lines = [
        "These are LLM-suggested review items, not asserted graph edges by "
        "themselves. `relation_propose` writes `Proposal` nodes; "
        "`proposal_review promote` materializes approved items.\n"
    ]
    for rel, spec in sorted(specs.items()):
        lines.append(
            f"- **{rel}**: `{', '.join(spec.get('from') or [])}` -> "
            f"`{', '.join(spec.get('to') or [])}`. "
            f"{spec.get('llm_description') or ''}"
        )
    return "\n".join(lines)


def main() -> int:
    cfg = Config.load()
    ont = ontology.load_ontology(ontology.DEFAULT_DOCTYPE)
    arch = _view_architecture(ont)
    fact_graph = _view_fact_graph(ont)
    proposal_menu = _view_relation_proposals(ont)

    out = (
        f"# Ontology: {ont['doctype']} (generated)\n\n"
        "> Generated by `python -m scripts.render_ontology` from\n"
        "> `configs/ontology/" + ont["doctype"] + ".yaml`. **Do not edit by hand.**\n"
        "> Re-run after any ontology change so the picture can't drift from the schema.\n\n"
        f"- **{len(ont['node_types'])}** node types across **{len(ont['layers'])}** layers\n"
        f"- **{len(ont['relationships'])}** relationship types\n"
        "- Solid edge = `loader` (written from extraction artifacts). "
        "Dashed = non-loader semantic edge (`derived:*` or `asserted:*`)\n\n"
        "Storage note: the declared graph is projected into one Azure Cosmos DB NoSQL\n"
        "container. Each node type below is stored as a record with a `kind`, solid\n"
        "attachment edges become fields on the child record, and dashed semantic edges\n"
        "become `kind=edge` items. The full mapping is in\n"
        "`storage/migration/port_notes.md`, and `pipeline/store/` is the only layer that\n"
        "talks to the container.\n\n"
        "## 1. Architecture & data flow\n\n"
        "```mermaid\n" + arch + "\n```\n\n"
        "## 2. Fact graph: semantic edges\n\n"
        "Deterministic derived edges plus review-promoted asserted edges between facts.\n\n"
        "```mermaid\n" + fact_graph + "\n```\n"
        "\n## 3. Relation proposal menu\n\n"
        + proposal_menu + "\n"
    )

    out_path = cfg.repo_root / "docs" / "ontology_graph.md" if hasattr(cfg, "repo_root") \
        else __import__("pathlib").Path("docs/ontology_graph.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    print(f"[render] wrote {out_path}  "
          f"({len(ont['node_types'])} nodes, {len(ont['relationships'])} rels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
