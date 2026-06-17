"""Vector store abstraction.

Uses ChromaDB when available (persistent or in-memory client), otherwise an
in-memory cosine-similarity store so retrieval ALWAYS works offline with zero
heavy dependencies. Import has zero side effects: no client is created and no
disk is touched until a method is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from skyn3t.rag.embeddings import cosine_similarity

try:  # optional heavy dependency
    import chromadb  # type: ignore

    _CHROMA_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when dep present
    chromadb = None  # type: ignore
    _CHROMA_AVAILABLE = False


@dataclass
class StoredDoc:
    id: str
    text: str
    embedding: List[float]
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class SearchHit:
    id: str
    text: str
    score: float
    metadata: Dict[str, object] = field(default_factory=dict)


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
        persist_path: Optional[Path] = None,
        prefer_chroma: bool = True,
    ) -> None:
        self.collection_name = collection
        self.persist_path = Path(persist_path) if persist_path else None
        self._use_chroma = bool(prefer_chroma and _CHROMA_AVAILABLE)
        self.backend = "chromadb" if self._use_chroma else "memory"
        self._client = None
        self._collection = None
        # in-memory fallback storage
        self._docs: Dict[str, StoredDoc] = {}

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
            return True
        except Exception:
            # Degrade to memory backend on any chroma failure.
            self._use_chroma = False
            self.backend = "memory"
            self._collection = None
            self._client = None
            return False

    # -- writes ------------------------------------------------------------
    def add(
        self,
        ids: Sequence[str],
        texts: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Optional[Sequence[Dict[str, object]]] = None,
    ) -> int:
        ids = list(ids)
        texts = list(texts)
        embeddings = [list(map(float, e)) for e in embeddings]
        metas = list(metadatas) if metadatas else [{} for _ in ids]
        if not ids:
            return 0
        if self._ensure_chroma():
            try:
                # chroma rejects empty metadata dicts in some versions
                safe_metas = [m or {"_": ""} for m in metas]
                self._collection.upsert(  # type: ignore
                    ids=ids,
                    documents=texts,
                    embeddings=embeddings,
                    metadatas=safe_metas,
                )
                return len(ids)
            except Exception:
                self._use_chroma = False
                self.backend = "memory"
        for i, _id in enumerate(ids):
            self._docs[_id] = StoredDoc(
                id=_id,
                text=texts[i],
                embedding=embeddings[i],
                metadata=metas[i] or {},
            )
        return len(ids)

    # -- queries -----------------------------------------------------------
    def query(
        self,
        embedding: Sequence[float],
        top_k: int = 5,
    ) -> List[SearchHit]:
        embedding = list(map(float, embedding))
        if self._ensure_chroma():
            try:
                res = self._collection.query(  # type: ignore
                    query_embeddings=[embedding],
                    n_results=max(1, top_k),
                )
                hits: List[SearchHit] = []
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
            except Exception:
                self._use_chroma = False
                self.backend = "memory"
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
        if self._use_chroma and self._collection is not None:
            try:
                return int(self._collection.count())  # type: ignore
            except Exception:
                pass
        return len(self._docs)

    def reset(self) -> None:
        self._docs.clear()
        if self._use_chroma and self._client is not None:
            try:
                self._client.delete_collection(self.collection_name)  # type: ignore
            except Exception:
                pass
            self._collection = None


__all__ = ["VectorStore", "SearchHit", "StoredDoc"]
