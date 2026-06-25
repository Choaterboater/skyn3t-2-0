"""Hybrid retrieval: semantic (vector) + lexical (BM25).

BM25 uses ``rank_bm25`` when available; otherwise a small pure-Python BM25
implementation provides identical behavior offline. Fusion uses reciprocal
rank fusion (RRF) so the two signals combine without score-scale assumptions.

Import has zero side effects.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from skyn3t.rag.embeddings import Embedder
from skyn3t.rag.vector_store import SearchHit, VectorStore

try:  # optional dependency
    from rank_bm25 import BM25Okapi  # type: ignore

    _BM25_AVAILABLE = True
except Exception:
    BM25Okapi = None  # type: ignore
    _BM25_AVAILABLE = False


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


class _PureBM25:
    """Minimal BM25 Okapi fallback (no numpy)."""

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = corpus
        self.n = len(corpus)
        self.doc_len = [len(d) for d in corpus]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.doc_freqs: list[Counter] = [Counter(d) for d in corpus]
        df: Counter = Counter()
        for d in corpus:
            for term in set(d):
                df[term] += 1
        self.idf: dict[str, float] = {}
        for term, freq in df.items():
            # BM25 idf with +1 to keep non-negative.
            self.idf[term] = math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))

    def get_scores(self, query: list[str]) -> list[float]:
        scores = [0.0] * self.n
        for i in range(self.n):
            freqs = self.doc_freqs[i]
            dl = self.doc_len[i]
            denom_norm = self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            for term in query:
                if term not in freqs:
                    continue
                idf = self.idf.get(term, 0.0)
                tf = freqs[term]
                scores[i] += idf * (tf * (self.k1 + 1)) / (tf + denom_norm)
        return scores


@dataclass
class Document:
    id: str
    text: str
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


class HybridRetriever:
    """Indexes documents and retrieves via fused semantic + lexical scores.

    When ``alpha`` is 1.0 it is semantic-only; 0.0 is lexical-only. The
    default blends both via reciprocal rank fusion.
    """

    def __init__(
        self,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        alpha: float = 0.5,
        rrf_k: int = 60,
    ) -> None:
        self.embedder = embedder or Embedder()
        self.store = vector_store or VectorStore(prefer_chroma=True)
        self.alpha = max(0.0, min(1.0, alpha))
        self.rrf_k = max(1, int(rrf_k))
        self._docs: dict[str, Document] = {}
        self._tokens: dict[str, list[str]] = {}
        self._bm25: Any | None = None
        self._bm25_ids: list[str] = []
        self._dirty = True
        self.bm25_backend = "rank_bm25" if _BM25_AVAILABLE else "pure_python"

    # -- indexing ----------------------------------------------------------
    def add_documents(
        self,
        ids: Sequence[str],
        texts: Sequence[str],
        metadatas: Sequence[dict[str, object]] | None = None,
    ) -> int:
        ids = list(ids)
        texts = list(texts)
        metas = list(metadatas) if metadatas else [{} for _ in ids]
        if not ids:
            return 0
        embeddings = self.embedder.embed_batch(texts)
        for i, _id in enumerate(ids):
            self._docs[_id] = Document(
                id=_id,
                text=texts[i],
                embedding=embeddings[i],
                metadata=metas[i] or {},
            )
            self._tokens[_id] = _tokenize(texts[i])
        self.store.add(ids, texts, embeddings, metas)
        self._dirty = True
        return len(ids)

    def _rebuild_bm25(self) -> None:
        self._bm25_ids = list(self._tokens.keys())
        corpus = [self._tokens[i] for i in self._bm25_ids]
        if not corpus:
            self._bm25 = None
        elif _BM25_AVAILABLE:
            try:
                self._bm25 = BM25Okapi(corpus)  # type: ignore
            except Exception:
                self._bm25 = _PureBM25(corpus)
        else:
            self._bm25 = _PureBM25(corpus)
        self._dirty = False

    # -- search ------------------------------------------------------------
    def _semantic(self, query: str, top_k: int) -> list[SearchHit]:
        qv = self.embedder.embed(query)
        return self.store.query(qv, top_k=top_k)

    def _lexical(self, query: str, top_k: int) -> list[SearchHit]:
        if self._dirty:
            self._rebuild_bm25()
        if self._bm25 is None or not self._bm25_ids:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(
            zip(self._bm25_ids, scores, strict=False), key=lambda x: x[1], reverse=True
        )
        hits: list[SearchHit] = []
        for _id, sc in ranked[:top_k]:
            if sc <= 0:
                continue
            d = self._docs.get(_id)
            if d is None:
                continue
            hits.append(
                SearchHit(id=_id, text=d.text, score=float(sc), metadata=d.metadata)
            )
        return hits

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        if not query or not query.strip():
            return []
        pool = max(top_k * 4, top_k)
        sem = self._semantic(query, pool)
        if self.alpha >= 1.0:
            return sem[:top_k]
        lex = self._lexical(query, pool)
        if self.alpha <= 0.0:
            return lex[:top_k] if lex else sem[:top_k]
        return self._fuse(sem, lex, top_k)

    def _fuse(
        self, sem: list[SearchHit], lex: list[SearchHit], top_k: int
    ) -> list[SearchHit]:
        """Reciprocal rank fusion weighted by alpha."""
        combined: dict[str, float] = {}
        meta: dict[str, SearchHit] = {}
        for rank, hit in enumerate(sem):
            combined[hit.id] = combined.get(hit.id, 0.0) + self.alpha / (
                self.rrf_k + rank + 1
            )
            meta[hit.id] = hit
        for rank, hit in enumerate(lex):
            combined[hit.id] = combined.get(hit.id, 0.0) + (1.0 - self.alpha) / (
                self.rrf_k + rank + 1
            )
            meta.setdefault(hit.id, hit)
        order = sorted(combined.items(), key=lambda x: x[1], reverse=True)
        out: list[SearchHit] = []
        for _id, score in order[:top_k]:
            h = meta[_id]
            out.append(
                SearchHit(id=_id, text=h.text, score=float(score), metadata=h.metadata)
            )
        return out

    def count(self) -> int:
        return len(self._docs)


__all__ = ["HybridRetriever", "Document"]
