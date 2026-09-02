"""pipeline/store — the Cosmos DB storage layer (replaces the Neo4j driver).

Usage:
    from pipeline.store import model, get_store          # sync (pipeline/scripts)
    from pipeline.store.aio import AsyncCosmosStore      # async (the API)
"""
from . import model  # noqa: F401
from .client import CosmosStore, get_store  # noqa: F401
