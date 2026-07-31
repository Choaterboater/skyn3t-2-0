"""Vector store abstraction.

Uses ChromaDB when available (persistent or in-memory client), otherwise an
in-memory cosine-similarity store so retrieval ALWAYS works offline with zero
heavy dependencies. Import has zero side effects: no client is created and no
disk is touched until a method is called.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from skyn3t.rag.embeddings import cosine_similarity

try:  # optional heavy dependency
    import chromadb  # type: ignore

    _CHROMA_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when dep present
    chromadb = None  # type: ignore
    _CHROMA_AVAILABLE = False

log = structlog.get_logger(__name__)

# Sidecar written next to a persistent chroma store recording the dim the
# base collection was pinned at, so a later process can detect embedder
# backend drift (e.g. sentence-transformers 384 vs hashing fallback 256)
# before chroma starts rejecting every upsert/query.
_MARKER_NAME = "embedder.json"


def _is_dim_mismatch(exc: Exception) -> bool:
    """True when a chroma error is a dimensionality rejection (embedder
    drift), not a transient failure like a locked SQLite file."""
    return "dimension" in str(exc).lower()


@dataclass
class StoredDoc:
    id: str
    text: str
    embedding: list[float]
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class SearchHit:
    id: str
    text: str
    score: float
    metadata: dict[str, object] = field(default_factory=dict)


class VectorStore:
    """Add embedded documents and query by vector similarity.

    Parameters
    ----------
    collection:
        Logical collection name.
    persist_path:
        If set and chromadb is available, use a persistent client there.
        Ignored by the in-memory fallback.
    prefer_chroma:
        Set False to force the in-memory backend (handy for tests).
    """

    def __init__(
        self,
        collection: str = "skyn3t_rag",
        persist_path: Path | None = None,
        prefer_chroma: bool = True,
    ) -> None:
        self.collection_name = collection
        self.persist_path = Path(persist_path) if persist_path else None
        self._use_chroma = bool(prefer_chroma and _CHROMA_AVAILABLE)
        self.backend = "chromadb" if self._use_chroma else "memory"
        self._client = None
        self._collection = None
        # in-memory fallback storage
        self._docs: dict[str, StoredDoc] = {}
        # Dimensionality pinning: chroma fixes a collection's dim at first
        # add and raises on every mismatched upsert/query afterwards. When
        # the embedder backend drifts across processes (ST 384 vs hashing
        # 256), that used to silently divert every write to process-local
        # memory and turn recall into []. Track the base collection's pinned
        # dim and route drifted dims to a dim-suffixed sibling collection so
        # writes keep persisting and queries keep answering — loudly.
        self._store_dim: int | None = None
        self._drift_dims: set[int] = set()
        self._drift_collections: dict[int, object] = {}
        self.dim_drift: dict[str, object] | None = None

    # -- chroma lazy init --------------------------------------------------
    def _ensure_chroma(self) -> bool:
        if not self._use_chroma:
            return False
        if self._collection is not None:
            return True
        try:
            if self.persist_path is not None:
                self.persist_path.mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(  # type: ignore
                    path=str(self.persist_path)
                )
            else:
                self._client = chromadb.Client()  # type: ignore
            self._collection = self._client.get_or_create_collection(  # type: ignore
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            self._seed_store_dim()
            return True
        except Exception:
            # Degrade to memory backend on any chroma failure.
            log.warning(
                "vector_store.chroma_init_failed",
                collection=self.collection_name,
                exc_info=True,
            )
            self._use_chroma = False
            self.backend = "memory"
            self._collection = None
            self._client = None
            return False

    # -- dimensionality pinning -------------------------------------------
    def _marker_path(self) -> Path | None:
        return (self.persist_path / _MARKER_NAME) if self.persist_path else None

    def _seed_store_dim(self) -> None:
        """Best-effort discovery of the dim the base collection is pinned
        at: sidecar marker first, then a one-doc peek. Never raises."""
        if self._store_dim is not None:
            return
        marker = self._marker_path()
        if marker is not None:
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
                if data.get("collection") == self.collection_name:
                    dim = int(data.get("dim") or 0)
                    if dim > 0:
                        self._store_dim = dim
                        return
            except Exception:
                pass  # legacy store without a marker -> fall through to peek
        col = self._collection
        if col is None:
            return
        try:
            if int(col.count()) > 0:  # type: ignore[union-attr]
                res = col.peek(1)  # type: ignore[union-attr]
                embs = (res or {}).get("embeddings")
                if embs is not None and len(embs) > 0 and len(embs[0]) > 0:
                    self._store_dim = int(len(embs[0]))
        except Exception:
            pass

    def _write_marker(self, dim: int) -> None:
        """Write-once sidecar recording the base collection's pinned dim so
        a later process can detect embedder drift up front. Never raises."""
        marker = self._marker_path()
        if marker is None or dim <= 0:
            return
        try:
            if marker.exists():
                return
            marker.write_text(
                json.dumps(
                    {"collection": self.collection_name, "dim": int(dim), "version": 1}
                ),
                encoding="utf-8",
            )
        except Exception:
            log.debug("vector_store.marker_write_failed", exc_info=True)

    def _drift_name(self, dim: int) -> str:
        return f"{self.collection_name}_d{dim}"

    def _drift_collection(self, dim: int):
        """Get or create the dim-suffixed sibling collection for a drifted
        embedder. May raise; callers already treat chroma errors as
        per-call fallbacks."""
        col = self._drift_collections.get(dim)
        if col is not None:
            return col
        name = self._drift_name(dim)
        col = self._client.get_or_create_collection(  # type: ignore[union-attr]
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
        self._drift_collections[dim] = col
        self._drift_dims.add(dim)
        self.dim_drift = {
            "store_dim": self._store_dim,
            "embedder_dim": dim,
            "active_collection": name,
        }
        log.warning(
            "vector_store.embedder_dim_drift",
            collection=self.collection_name,
            store_dim=self._store_dim,
            embedder_dim=dim,
            active_collection=name,
        )
        return col

    def _collection_for(self, dim: int):
        """Return the chroma collection compatible with ``dim`` vectors:
        the base collection when the dim matches (or is unknown), else the
        dim-suffixed sibling. The two spaces are incomparable, so siloing
        them is correct — and keeps writes persisted and queries answering
        instead of exceptioning into silent memory-only fallbacks."""
        if dim <= 0:
            return self._collection
        drifted = (
            self._store_dim is not None and dim != self._store_dim
        ) or dim in self._drift_dims
        if not drifted:
            return self._collection
        return self._drift_collection(dim)

    # -- writes ------------------------------------------------------------
    def add(
        self,
        ids: Sequence[str],
        texts: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, object]] | None = None,
    ) -> int:
        ids = list(ids)
        texts = list(texts)
        embeddings = [list(map(float, e)) for e in embeddings]
        metas = list(metadatas) if metadatas else [{} for _ in ids]
        if not ids:
            return 0
        dim = len(embeddings[0]) if embeddings else 0
        if self._ensure_chroma():
            # chroma rejects empty metadata dicts in some versions
            safe_metas = [m or {"_": ""} for m in metas]
            try:
                col = self._collection_for(dim)
                col.upsert(  # type: ignore[union-attr]
                    ids=ids,
                    documents=texts,
                    embeddings=embeddings,
                    metadatas=safe_metas,
                )
                if col is self._collection and self._store_dim is None and dim > 0:
                    self._store_dim = dim
                    self._write_marker(dim)
                return len(ids)
            except Exception as exc:
                if dim > 0 and dim not in self._drift_dims and _is_dim_mismatch(exc):
                    # The base collection is pinned at another dim and the
                    # pin could not be discovered up front: this is embedder
                    # drift, not a transient failure. Route the batch (and
                    # this dim from now on) to the suffixed sibling so it
                    # persists instead of evaporating with the process.
                    self._drift_dims.add(dim)
                    try:
                        col = self._drift_collection(dim)
                        col.upsert(  # type: ignore[union-attr]
                            ids=ids,
                            documents=texts,
                            embeddings=embeddings,
                            metadatas=safe_metas,
                        )
                        return len(ids)
                    except Exception:
                        log.warning(
                            "vector_store.chroma_add_failed",
                            collection=self.collection_name,
                            docs=len(ids),
                            exc_info=True,
                        )
                else:
                    # Transient chroma write failure: keep the batch
                    # retrievable in memory but leave chroma active for
                    # future calls — a permanent flip would orphan everything
                    # already persisted. Only _ensure_chroma (client
                    # construction) degrades for good.
                    log.warning(
                        "vector_store.chroma_add_failed",
                        collection=self.collection_name,
                        docs=len(ids),
                        exc_info=True,
                    )
        for i, _id in enumerate(ids):
            self._docs[_id] = StoredDoc(
                id=_id,
                text=texts[i],
                embedding=list(embeddings[i]),
                metadata=metas[i] or {},
            )
        return len(ids)

    def delete_by_source(self, source: str) -> int:
        """Remove every document whose metadata ``source`` matches.

        Chunk ids are content hashes, so re-ingesting edited text mints new
        ids and stale chunks must be purged explicitly. Best-effort on both
        backends (a batch can live in either after a chroma write failure);
        returns the number of in-memory docs removed (chroma reports no
        delete count). Documents without a ``source`` key are never matched.
        Never raises.
        """
        if not source:
            return 0
        if self._ensure_chroma():
            cols = [self._collection, *self._drift_collections.values()]
            for col in cols:
                try:
                    col.delete(where={"source": source})  # type: ignore[union-attr]
                except Exception:
                    log.warning(
                        "vector_store.chroma_delete_failed",
                        collection=self.collection_name,
                        source=source,
                        exc_info=True,
                    )
        stale = [
            _id
            for _id, doc in self._docs.items()
            if doc.metadata.get("source") == source
        ]
        for _id in stale:
            del self._docs[_id]
        return len(stale)

    # -- queries -----------------------------------------------------------
    @staticmethod
    def _hits_from_result(res: dict) -> list[SearchHit]:
        hits: list[SearchHit] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, _id in enumerate(ids):
            dist = dists[i] if i < len(dists) else 1.0
            # cosine distance -> similarity
            score = 1.0 - float(dist)
            hits.append(
                SearchHit(
                    id=_id,
                    text=docs[i] if i < len(docs) else "",
                    score=score,
                    metadata=metas[i] if i < len(metas) else {},
                )
            )
        return hits

    def query(
        self,
        embedding: Sequence[float],
        top_k: int = 5,
    ) -> list[SearchHit]:
        embedding = list(map(float, embedding))
        dim = len(embedding)
        if self._ensure_chroma():
            try:
                col = self._collection_for(dim)
                res = col.query(  # type: ignore[union-attr]
                    query_embeddings=[embedding],
                    n_results=max(1, top_k),
                )
                return self._hits_from_result(res)
            except Exception as exc:
                if dim > 0 and dim not in self._drift_dims and _is_dim_mismatch(exc):
                    # The base collection is pinned at another dim (embedder
                    # drift with no discoverable pin): query the suffixed
                    # sibling — this session's vector space — instead of
                    # exceptioning into a silent [].
                    self._drift_dims.add(dim)
                    try:
                        col = self._drift_collection(dim)
                        res = col.query(  # type: ignore[union-attr]
                            query_embeddings=[embedding],
                            n_results=max(1, top_k),
                        )
                        return self._hits_from_result(res)
                    except Exception:
                        log.warning(
                            "vector_store.chroma_query_failed",
                            collection=self.collection_name,
                            exc_info=True,
                        )
                else:
                    # Transient chroma query failure (e.g. locked SQLite):
                    # score the in-memory docs for this call only. A
                    # permanent flip would make every document persisted in
                    # chroma unreachable for the life of this instance.
                    log.warning(
                        "vector_store.chroma_query_failed",
                        collection=self.collection_name,
                        exc_info=True,
                    )
        scored = [
            SearchHit(
                id=d.id,
                text=d.text,
                score=cosine_similarity(embedding, d.embedding),
                metadata=d.metadata,
            )
            for d in self._docs.values()
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[: max(1, top_k)]

    # -- introspection -----------------------------------------------------
    def count(self) -> int:
        total = 0
        counted = False
        if self._use_chroma and self._collection is not None:
            try:
                total += int(self._collection.count())  # type: ignore
                counted = True
            except Exception:
                pass
            for col in self._drift_collections.values():
                try:
                    total += int(col.count())  # type: ignore[union-attr]
                    counted = True
                except Exception:
                    pass
        if counted:
            return total
        return len(self._docs)

    def reset(self) -> None:
        self._docs.clear()
        if self._use_chroma and self._client is not None:
            try:
                self._client.delete_collection(self.collection_name)  # type: ignore
            except Exception:
                pass
            for dim in list(self._drift_collections):
                try:
                    self._client.delete_collection(self._drift_name(dim))  # type: ignore
                except Exception:
                    pass
            self._collection = None
        self._drift_collections.clear()
        self._drift_dims.clear()
        self._store_dim = None
        self.dim_drift = None
        marker = self._marker_path()
        if marker is not None:
            # The marker describes a store that no longer exists; a stale
            # pin would misroute the next process's first writes.
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
                if data.get("collection") == self.collection_name:
                    marker.unlink()
            except Exception:
                pass

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Release the chroma client. A PersistentClient holds an open SQLite
        handle to persist_path; without closing it those file descriptors leak
        as VectorStore instances accumulate. Idempotent and never raises."""
        client = self._client
        if client is not None:
            closer = getattr(client, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    # close() must stay non-raising; the leak is already
                    # being released, so a failure here is only worth a log.
                    log.debug("vector_store.chroma_close_failed", exc_info=True)
        self._client = None
        self._collection = None
        # Collection handles belong to the released client; drop them so a
        # later _ensure_chroma rebuilds everything from the fresh client.
        self._drift_collections.clear()

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["VectorStore", "SearchHit", "StoredDoc"]
