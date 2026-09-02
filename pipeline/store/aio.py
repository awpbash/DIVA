"""store/aio.py — the async Cosmos store (the API's twin of store/client.py).

Same document model, same vector-mode behaviour; only the I/O is async. The
API opens ONE AsyncCosmosStore in its lifespan (api/deps.py) and closes it at
shutdown, exactly like the old Neo4j AsyncDriver.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.cosmos import exceptions
from azure.cosmos.aio import ContainerProxy, CosmosClient

from ..config import Config
from . import model
from .query import RamVectors, inject_pk

log = logging.getLogger("store")

# The store's "database is down / waking" signature — what the old code
# caught as neo4j ServiceUnavailable/SessionExpired/TransientError. Callers
# abort the turn instead of narrating tool errors to the model.
STORE_UNAVAILABLE_ERRORS = (ServiceRequestError, ServiceResponseError)


class AsyncCosmosStore:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        https = cfg.cosmos_uri.startswith("https://")
        self.client = CosmosClient(
            cfg.cosmos_uri, credential=cfg.cosmos_key,
            connection_verify=https and
            "localhost" not in cfg.cosmos_uri and
            "127.0.0.1" not in cfg.cosmos_uri,
            # The emulator (always plain http) advertises 127.0.0.1 as its
            # region endpoint; following it breaks any client not on the
            # emulator's own host (e.g. the app container). Real Azure is
            # always https and keeps discovery for multi-region routing.
            enable_endpoint_discovery=https)
        self._container: ContainerProxy | None = None
        self._vectors: dict[str, RamVectors] = {}
        self._vectors_stamp: str | None = None

    async def close(self) -> None:
        await self.client.close()

    async def verify_connectivity(self) -> None:
        """One cheap call that fails if Cosmos is unreachable (readiness probe)."""
        db = self.client.get_database_client(self.cfg.cosmos_db)
        await db.read()

    @property
    def container(self) -> ContainerProxy:
        if self._container is None:
            db = self.client.get_database_client(self.cfg.cosmos_db)
            self._container = db.get_container_client(self.cfg.cosmos_container)
        return self._container

    # -- reads ----------------------------------------------------------------

    async def point(self, item_id: str, pk: str) -> dict | None:
        try:
            return await self.container.read_item(item_id, partition_key=pk)
        except exceptions.CosmosResourceNotFoundError:
            return None

    async def query(self, sql: str, params: list[dict] | None = None,
                    pk: str | None = None) -> list[dict]:
        # inject_pk: the emulator can ignore partition_key routing, so the
        # filter also lives in the SQL (see store/query.py).
        sql, params = inject_pk(sql, params or [], pk)
        it = self.container.query_items(query=sql, parameters=params,
                                        partition_key=pk)
        return [row async for row in it]

    async def count(self, sql: str, params: list[dict] | None = None) -> int:
        rows = await self.query(sql, params)
        return int(rows[0]) if rows else 0

    # 100 ids per ARRAY_CONTAINS stays well under the engine's parameter
    # limits while collapsing N point reads (N network round trips on real
    # Azure) into ceil(N/100) queries, fired concurrently.
    _IDS_CHUNK = 100

    async def fetch_many(self, ids: list[str], pk: str | None = None,
                         select: list[str] | None = None) -> dict[str, dict]:
        """{id: item} for a batch of ids — the bulk twin of ``point``.
        ``select`` projects named fields (id is always included). Without it
        the whole item comes back, so never use the default on
        embedding-bearing kinds (see fetch_embeddings)."""
        want = sorted(set(ids))
        if not want:
            return {}
        cols = "*"
        if select:
            cols = ", ".join(f"c.{f}" for f in dict.fromkeys(["id", *select]))
        chunks = [want[i:i + self._IDS_CHUNK]
                  for i in range(0, len(want), self._IDS_CHUNK)]
        results = await asyncio.gather(*[
            self.query(f"SELECT {cols} FROM c WHERE ARRAY_CONTAINS(@ids, c.id)",
                       [{"name": "@ids", "value": chunk}], pk=pk)
            for chunk in chunks])
        return {row["id"]: row for rows in results for row in rows}

    # -- writes (review propagation + jobs need a small write surface) --------

    async def upsert(self, item: dict[str, Any]) -> None:
        await self.container.upsert_item(item)

    async def upsert_many(self, items: list[dict[str, Any]]) -> int:
        for it in items:
            await self.container.upsert_item(it)
        return len(items)

    async def delete(self, item_id: str, pk: str) -> None:
        try:
            await self.container.delete_item(item_id, partition_key=pk)
        except exceptions.CosmosResourceNotFoundError:
            pass

    async def set_build_stamp(self, note: str = "") -> None:
        await self.upsert(model.item(model.META, model.BUILD_STAMP_ID,
                                     model.META_PK,
                                     {"stamp": f"{time.time_ns()}", "note": note}))

    # -- vector search ----------------------------------------------------------

    # The emulator's query engine dies on multi-MB embedding result sets
    # (PostgresError 3000X), so fat vectors are always fetched in id-chunks.
    _EMB_CHUNK = 40

    async def fetch_embeddings(self, kind: str | None = None,
                               pk: str | None = None) -> list[dict]:
        """[{id, doc_id, embedding}] rows, ids first then <=_EMB_CHUNK fat
        arrays per round-trip — the only safe way to bulk-read vectors."""
        where = "IS_DEFINED(c.embedding)"
        params: list[dict] = []
        if kind:
            where += " AND c.kind = @k"
            params.append({"name": "@k", "value": kind})
        ids = await self.query(f"SELECT c.id FROM c WHERE {where}", params, pk=pk)
        out: list[dict] = []
        for i in range(0, len(ids), self._EMB_CHUNK):
            chunk = [r["id"] for r in ids[i:i + self._EMB_CHUNK]]
            out.extend(await self.query(
                "SELECT c.id, c.doc_id, c.embedding FROM c "
                "WHERE ARRAY_CONTAINS(@ids, c.id)",
                [{"name": "@ids", "value": chunk}], pk=pk))
        return out

    async def _build_stamp(self) -> str:
        doc = await self.point(model.BUILD_STAMP_ID, model.META_PK)
        return (doc or {}).get("stamp", "")

    async def _ram_vectors(self, kind: str) -> RamVectors:
        stamp = await self._build_stamp()
        if stamp != self._vectors_stamp:
            self._vectors.clear()
            self._vectors_stamp = stamp
        if kind not in self._vectors:
            rows = await self.fetch_embeddings(kind)
            self._vectors[kind] = RamVectors(
                (r["id"], r.get("doc_id", ""), r["embedding"]) for r in rows)
            log.info("ram vectors loaded: kind=%s n=%d", kind, len(self._vectors[kind]))
        return self._vectors[kind]

    async def vector_search(self, kind: str, query_vec: list[float], k: int,
                            doc_ids: list[str] | None = None
                            ) -> list[tuple[str, float]]:
        if self.cfg.cosmos_vector_mode == "native":
            params: list[dict] = [
                {"name": "@k", "value": kind},
                {"name": "@qv", "value": list(query_vec)},
                {"name": "@top", "value": int(k)},
            ]
            scope = "true"
            if doc_ids:
                params.append({"name": "@ids", "value": list(doc_ids)})
                scope = "ARRAY_CONTAINS(@ids, c.doc_id)"
            rows = await self.query(
                f"SELECT TOP @top c.id, VectorDistance(c.embedding, @qv) AS score "
                f"FROM c WHERE c.kind = @k AND IS_DEFINED(c.embedding) AND {scope} "
                f"ORDER BY VectorDistance(c.embedding, @qv)", params)
            return [(r["id"], float(r["score"])) for r in rows]
        vecs = await self._ram_vectors(kind)
        return vecs.top_k(query_vec, k, set(doc_ids) if doc_ids else None)
