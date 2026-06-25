# skyn3t/intelligence/semantic_skills.py
"""Semantic, brief-aware skill retrieval (Spec 2).

Finds skills relevant to the BRIEF by embedding similarity, not just by stack/
tag keyword match — the survey-flagged "semantic retrieval" lever. Reuses
rag.embeddings.Embedder, whose deterministic hashing fallback means this ALWAYS
produces usable vectors offline (and is reproducible in tests via
``Embedder(prefer_st=False)``). Even the hashing fallback is brief-aware: it is
bag-of-words cosine over the full skill text, so a brief surfaces skills sharing
its vocabulary even when they carry no matching tag.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from skyn3t.rag.embeddings import Embedder

# Reuse the offline BM25 + tokenizer from the hybrid retriever so lesson/skill
# brief-ranking fuses a LEXICAL (keyword) signal with the embedding-cosine signal
# via reciprocal-rank fusion — better recall than vector-only (gbrain-style), with
# no extra dependency (pure-Python BM25 fallback).
from skyn3t.rag.retrieval import _PureBM25, _tokenize


def _cosine(a: list[float], b: list[float]) -> float:
    # Embedder vectors are L2-normalized, so the dot product IS the cosine.
    # Different lengths mean different backends (hashing 256 vs ST 384) — zip
    # would silently truncate to a garbage partial dot, so refuse: not comparable.
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False))


def _bm25_scores(texts: list[str], query: str) -> list[float]:
    """BM25 relevance of each text to ``query`` (0.0 when no lexical overlap)."""
    corpus = [_tokenize(t) for t in texts]
    if not any(corpus):
        return [0.0] * len(texts)
    return _PureBM25(corpus).get_scores(_tokenize(query))


def _rrf(orders: list[list], *, rrf_k: int = 60) -> list:
    """Reciprocal-rank fusion of several ranked key-lists into one fused order.

    Each list contributes 1/(rrf_k + rank) to its keys; keys absent from a list
    simply don't get that list's contribution. Scale-free, so the vector cosine
    and BM25 signals combine without normalising their incomparable magnitudes."""
    combined: dict = {}
    for order in orders:
        for rank, key in enumerate(order):
            combined[key] = combined.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
    return [key for key, _ in sorted(combined.items(), key=lambda x: x[1], reverse=True)]


def _skill_text(skill: Any) -> str:
    parts = [str(getattr(skill, "slug", "") or ""),
             str(getattr(skill, "title", "") or "")]
    tags = getattr(skill, "tags", None) or []
    parts.append(" ".join(str(t) for t in tags))
    body = str(getattr(skill, "body", "") or "")
    return (" ".join(p for p in parts if p) + " " + body[:600]).strip()


class SemanticSkillIndex:
    """An in-memory embedding index over a skill set. Cheap to rebuild with the
    hashing embedder; cache it when using a real sentence-transformers model."""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder or Embedder()
        self._index: list[tuple[str, list[float]]] = []

    def build(self, skills: list[Any]) -> SemanticSkillIndex:
        items = [(str(getattr(s, "slug", "") or ""), _skill_text(s)) for s in skills]
        items = [(slug, text) for slug, text in items if slug]
        vecs = self.embedder.embed_batch([t for _, t in items]) if items else []
        self._index = [(slug, v) for (slug, _), v in zip(items, vecs, strict=False)]
        return self

    def query(self, text: str, *, k: int = 5, min_score: float = 0.0) -> list[tuple[str, float]]:
        if not self._index or not (text or "").strip():
            return []
        qv = self.embedder.embed(text)
        scored = [(slug, _cosine(qv, vec)) for slug, vec in self._index]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(slug, round(c, 4)) for slug, c in scored if c > min_score][:k]


def relevant_skills(skills: list[Any], brief: str, *, embedder: Embedder | None = None,
                    k: int = 5, min_score: float = 0.03) -> list[str]:
    """Top-K skill slugs most relevant to ``brief``, by HYBRID retrieval: the
    embedding-cosine ranking fused (RRF) with a BM25 keyword ranking over the
    skill text. The lexical pass rescues skills whose brief-vocabulary overlap the
    vector missed. Never raises; returns [] on an empty brief / skill set."""
    skills = list(skills or [])
    if not skills or not (brief or "").strip():
        return []
    emb = embedder or Embedder()
    idx = SemanticSkillIndex(emb).build(skills)
    pool = max(k * 4, k)
    sem_order = [slug for slug, _ in idx.query(brief, k=pool, min_score=min_score)]
    keyed = [(str(getattr(s, "slug", "") or ""), _skill_text(s)) for s in skills]
    keyed = [(slug, t) for slug, t in keyed if slug]
    bm = _bm25_scores([t for _, t in keyed], brief)
    lex_order = [keyed[i][0] for i in sorted(range(len(keyed)), key=lambda j: bm[j], reverse=True)
                 if bm[i] > 0.0]
    return _rrf([sem_order, lex_order])[:k]


def rank_texts(items: list[Any], query: str, *, get_text: Callable[[Any], str],
               embedder: Embedder | None = None, k: int = 5,
               min_score: float = 0.0) -> list[Any]:
    """Rank items by HYBRID relevance of ``get_text(item)`` to ``query`` — the
    embedding-cosine ranking fused (RRF) with a BM25 keyword ranking — best first;
    return the top-K ORIGINAL items. With an empty query (or no items) it preserves
    the input order, capped at k, so callers can use it as a best-effort re-rank
    that degrades to the prior ordering. The BM25 pass adds lexical recall (a
    keyword match the vector missed) over the old vector-only rank. Never raises
    beyond a caller-supplied get_text."""
    items = list(items or [])
    if not items:
        return []
    if not (query or "").strip():
        return items[:k]
    texts = [str(get_text(it) or "") for it in items]
    emb = embedder or Embedder()
    vecs = emb.embed_batch(texts)
    qv = emb.embed(query)
    cos = [_cosine(qv, v) for v in vecs]
    # Semantic order (above the relevance floor) fused with the BM25 lexical order.
    sem_order = [i for i in sorted(range(len(items)), key=lambda j: cos[j], reverse=True)
                 if cos[i] > min_score]
    bm = _bm25_scores(texts, query)
    lex_order = [i for i in sorted(range(len(items)), key=lambda j: bm[j], reverse=True)
                 if bm[i] > 0.0]
    fused = _rrf([sem_order, lex_order])
    if not fused:  # nothing above the floor and no lexical hit — degrade to cosine order
        fused = sorted(range(len(items)), key=lambda j: cos[j], reverse=True)
    return [items[i] for i in fused][:k]
