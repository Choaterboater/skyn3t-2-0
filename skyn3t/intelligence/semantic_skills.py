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


def _cosine(a: list[float], b: list[float]) -> float:
    # Embedder vectors are L2-normalized, so the dot product IS the cosine.
    return sum(x * y for x, y in zip(a, b))


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
        self._index = [(slug, v) for (slug, _), v in zip(items, vecs)]
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
    """Top-K skill slugs most semantically relevant to ``brief``. Never raises;
    returns [] on an empty brief / skill set."""
    idx = SemanticSkillIndex(embedder).build(list(skills or []))
    return [slug for slug, _ in idx.query(brief, k=k, min_score=min_score)]


def rank_texts(items: list[Any], query: str, *, get_text: Callable[[Any], str],
               embedder: Embedder | None = None, k: int = 5,
               min_score: float = 0.0) -> list[Any]:
    """Rank arbitrary items by embedding-cosine of ``get_text(item)`` to
    ``query``, best first; return the top-K ORIGINAL items. With an empty query
    (or no items) it preserves the input order, capped at k — so callers can
    use it as a best-effort re-rank that degrades to the prior ordering. Never
    raises beyond a caller-supplied get_text."""
    items = list(items or [])
    if not items:
        return []
    if not (query or "").strip():
        return items[:k]
    emb = embedder or Embedder()
    vecs = emb.embed_batch([str(get_text(it) or "") for it in items])
    qv = emb.embed(query)
    scored = [(it, _cosine(qv, vec)) for it, vec in zip(items, vecs)]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [it for it, c in scored if c > min_score][:k]
