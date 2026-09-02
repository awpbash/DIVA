"""store/client.py — the sync Cosmos store (pipeline + scripts).

The single place the pipeline talks to Cosmos DB. The async twin for the API
lives in store/aio.py; both share the document model (store/model.py) and the
pure ranking helpers (store/query.py).

Container bootstrap always REQUESTS a vector policy on /embedding (DiskANN,
3072 dims, cosine). Where the backend accepts it (real Azure), native
VectorDistance queries are available behind COSMOS_VECTOR_MODE=native; where
it doesn't (the local emulator), creation silently retries without the policy
and vector search runs in exact client mode (store/query.RamVectors). Either
way the caller sees the same ``vector_search``.

Wipe strategy: DROP + recreate the container. A rebuild is the pipeline's
universal rollback lever, so it must stay cheap — container recreation is a
metadata operation, far cheaper than deleting 20k items one by one.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from azure.cosmos import ContainerProxy, CosmosClient, PartitionKey, exceptions

from ..config import Config
from . import model
from .query import RamVectors, inject_pk

log = logging.getLogger("store")

_VECTOR_POLICY = {
    "vectorEmbeddings": [{
        "path": "/embedding",
        "dataType": "float32",
        "distanceFunction": "cosine",
        "dimensions": model.EMBED_DIMS,
    }]
}
# Exclude the fat embedding arrays from the range index — indexing 3072
# floats per item explodes write RUs for zero query value.
_INDEXING_POLICY = {
    "indexingMode": "consistent",
    "includedPaths": [{"path": "/*"}],
    "excludedPaths": [{"path": "/embedding/*"}, {"path": '/"_etag"/?'}],
}
_VECTOR_INDEXES = [{"path": "/embedding", "type": "diskANN"}]


def _client(cfg: Config) -> CosmosClient:
    https = cfg.cosmos_uri.startswith("https://")
    return CosmosClient(cfg.cosmos_uri, credential=cfg.cosmos_key,
                        connection_verify=https and
                        "localhost" not in cfg.cosmos_uri and
                        "127.0.0.1" not in cfg.cosmos_uri,
                        # Emulator (plain http) advertises 127.0.0.1 as its
                        # region endpoint — never follow it (breaks clients
                        # on other hosts, e.g. containers). Real Azure stays
                        # https with discovery on.
                        enable_endpoint_discovery=https)


class CosmosStore:
    """Sync store. Construct once per process (`CosmosStore(cfg)`), reuse."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = _client(cfg)
        self._container: ContainerProxy | None = None
        self._vectors: dict[str, RamVectors] = {}
        self._vectors_stamp: str | None = None

    # -- bootstrap ----------------------------------------------------------

    def wait_ready(self, timeout: float = 120.0) -> None:
        """Block until Cosmos answers (the emulator cold-boots in ~10-60s)."""
        deadline = time.monotonic() + timeout
        delay = 2.0
        while True:
            try:
                list(self.client.list_databases())
                return
            except Exception as exc:  # noqa: BLE001 — any failure means keep waiting
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"Cosmos not reachable at {self.cfg.cosmos_uri}: {exc}") from exc
                log.info("cosmos warming: %s", exc)
                time.sleep(delay)
                delay = min(delay * 1.5, 10.0)

    def ensure(self) -> ContainerProxy:
        """Create database + container if absent. Idempotent."""
        db = self.client.create_database_if_not_exists(self.cfg.cosmos_db)
        try:
            self._container = db.create_container_if_not_exists(
                id=self.cfg.cosmos_container,
                partition_key=PartitionKey(path="/pk"),
                vector_embedding_policy=_VECTOR_POLICY,
                indexing_policy={**_INDEXING_POLICY,
                                 "vectorIndexes": _VECTOR_INDEXES},
            )
        except exceptions.CosmosHttpResponseError as exc:
            # Emulator (or an old API version) rejecting the vector policy is
            # expected — client-mode vector search needs no index.
            log.info("container create with vector policy failed (%s); "
                     "retrying without — vector mode will be client-side", exc.status_code)
            self._container = db.create_container_if_not_exists(
                id=self.cfg.cosmos_container,
                partition_key=PartitionKey(path="/pk"),
                indexing_policy=_INDEXING_POLICY,
            )
        return self._container

    @property
    def container(self) -> ContainerProxy:
        if self._container is None:
            db = self.client.get_database_client(self.cfg.cosmos_db)
            self._container = db.get_container_client(self.cfg.cosmos_container)
        return self._container

    def nuke(self) -> int:
        """Drop + recreate the container. Returns the prior item count (best
        effort — 0 if the container didn't exist)."""
        n = 0
        try:
            n = self.count("SELECT VALUE COUNT(1) FROM c")
            db = self.client.get_database_client(self.cfg.cosmos_db)
            db.delete_container(self.cfg.cosmos_container)
        except exceptions.CosmosResourceNotFoundError:
            pass
        self._container = None
        self._vectors.clear()
        self.ensure()
        return n

    # -- writes ---------------------------------------------------------------

    def upsert(self, item: dict[str, Any]) -> None:
        self.container.upsert_item(item)

    def _upsert_retry(self, item: dict[str, Any]) -> None:
        try:
            self.container.upsert_item(item)
        except exceptions.CosmosResourceExistsError:
            # Emulator quirk: a concurrent same-partition upsert can surface
            # as Conflict. The item exists now, so the retry is a replace.
            self.container.upsert_item(item)

    def upsert_many(self, items: list[dict[str, Any]]) -> int:
        """Bulk upsert. Threaded for real batches — the client is
        thread-safe and the rebuild writes tens of thousands of items, so
        sequential round-trips dominate wall-clock otherwise."""
        if len(items) <= 8:
            for it in items:
                self._upsert_retry(it)
            return len(items)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self._upsert_retry, items))
        return len(items)

    def delete(self, item_id: str, pk: str) -> None:
        try:
            self.container.delete_item(item_id, partition_key=pk)
        except exceptions.CosmosResourceNotFoundError:
            pass

    def delete_where(self, sql: str, params: list[dict] | None = None) -> int:
        """Query (id, pk) pairs then delete them. For per-doc clears."""
        rows = self.query(f"SELECT c.id, c.pk FROM c WHERE {sql}", params)
        for r in rows:
            self.delete(r["id"], r["pk"])
        return len(rows)

    def patch(self, item_id: str, pk: str, ops: list[dict[str, Any]]) -> None:
        """Partial update; falls back to read-merge-replace where the backend
        lacks PATCH (older emulators). ops: [{'op':'set','path':'/x','value':v}]"""
        try:
            self.container.patch_item(item=item_id, partition_key=pk,
                                      patch_operations=ops)
        except (exceptions.CosmosHttpResponseError, AttributeError):
            doc = self.point(item_id, pk)
            if doc is None:
                return
            for op in ops:
                key = op["path"].lstrip("/")
                if op["op"] in ("set", "add", "replace"):
                    doc[key] = op["value"]
                elif op["op"] == "remove":
                    doc.pop(key, None)
            self.container.upsert_item(doc)

    def set_build_stamp(self, note: str = "") -> None:
        """Bump after every (re)build/embed pass — invalidates RAM vectors."""
        self.upsert(model.item(model.META, model.BUILD_STAMP_ID, model.META_PK,
                               {"stamp": f"{time.time_ns()}", "note": note}))

    # -- reads ----------------------------------------------------------------

    def point(self, item_id: str, pk: str) -> dict | None:
        try:
            return self.container.read_item(item_id, partition_key=pk)
        except exceptions.CosmosResourceNotFoundError:
            return None

    def query(self, sql: str, params: list[dict] | None = None,
              pk: str | None = None) -> list[dict]:
        sql, params = inject_pk(sql, params or [], pk)
        return list(self.container.query_items(
            query=sql, parameters=params,
            partition_key=pk,
            enable_cross_partition_query=(pk is None)))

    def count(self, sql: str, params: list[dict] | None = None) -> int:
        # Cross-partition VALUE COUNT on real Cosmos can return one PARTIAL
        # aggregate per partition range (the emulator's single partition always
        # returns one row) — sum them, never take rows[0].
        rows = self.query(sql, params)
        return int(sum(rows)) if rows else 0

    # -- vector search ----------------------------------------------------------

    # The emulator's query engine dies on multi-MB embedding result sets
    # (PostgresError 3000X), so fat vectors are always fetched in id-chunks.
    _EMB_CHUNK = 40

    def fetch_embeddings(self, kind: str | None = None,
                         pk: str | None = None) -> list[dict]:
        """[{id, doc_id, embedding}] rows, ids first then <=_EMB_CHUNK fat
        arrays per round-trip — the only safe way to bulk-read vectors."""
        where = "IS_DEFINED(c.embedding)"
        params: list[dict] = []
        if kind:
            where += " AND c.kind = @k"
            params.append({"name": "@k", "value": kind})
        ids = self.query(f"SELECT c.id FROM c WHERE {where}", params, pk=pk)
        out: list[dict] = []
        for i in range(0, len(ids), self._EMB_CHUNK):
            chunk = [r["id"] for r in ids[i:i + self._EMB_CHUNK]]
            out.extend(self.query(
                "SELECT c.id, c.doc_id, c.embedding FROM c "
                "WHERE ARRAY_CONTAINS(@ids, c.id)",
                [{"name": "@ids", "value": chunk}], pk=pk))
        return out

    def _build_stamp(self) -> str:
        doc = self.point(model.BUILD_STAMP_ID, model.META_PK)
        return (doc or {}).get("stamp", "")

    def _ram_vectors(self, kind: str) -> RamVectors:
        stamp = self._build_stamp()
        if stamp != self._vectors_stamp:
            self._vectors.clear()
            self._vectors_stamp = stamp
        if kind not in self._vectors:
            rows = self.fetch_embeddings(kind)
            self._vectors[kind] = RamVectors(
                (r["id"], r.get("doc_id", ""), r["embedding"]) for r in rows)
            log.info("ram vectors loaded: kind=%s n=%d", kind, len(self._vectors[kind]))
        return self._vectors[kind]

    def vector_search(self, kind: str, query_vec: list[float], k: int,
                      doc_ids: list[str] | None = None) -> list[tuple[str, float]]:
        """[(item_id, cosine_similarity)] best-first."""
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
            rows = self.query(
                f"SELECT TOP @top c.id, VectorDistance(c.embedding, @qv) AS score "
                f"FROM c WHERE c.kind = @k AND IS_DEFINED(c.embedding) AND {scope} "
                f"ORDER BY VectorDistance(c.embedding, @qv)", params)
            return [(r["id"], float(r["score"])) for r in rows]
        return self._ram_vectors(kind).top_k(
            query_vec, k, set(doc_ids) if doc_ids else None)


_STORE: CosmosStore | None = None


def get_store(cfg: Config | None = None) -> CosmosStore:
    """Process-wide singleton (mirrors the old module-level driver)."""
    global _STORE
    if _STORE is None:
        _STORE = CosmosStore(cfg or Config.load())
    return _STORE
