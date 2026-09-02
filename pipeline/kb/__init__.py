"""kb — the rebuilt, pack-driven knowledge-graph layer (Part 2 of the seam).

Consumes canonical.json (the typed-fact contract) + the compiled Doctype Pack
and builds the Neo4j graph: dual-label (:Fact:<Type>) nodes, evidence
anchoring, and deterministically-derived edges. Everything that was a
hardcoded Python dict in pipeline/extraction/load.py is read from the pack
here, so a new doctype is a new pack with zero loader changes.
"""
