"""Embedding backends for the RAG subsystem.

Prefers ``sentence-transformers`` when installed; otherwise falls back to a
deterministic hashing embedding so the module ALWAYS produces usable vectors
offline with zero heavy dependencies (design rule #6: degrade, don't crash).

Import has zero side effects: the heavy model is only loaded on first use.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, List, Sequence

try:  # optional heavy dependency
    from sentence_transformers import SentenceTransformer  # type: ignore

    _ST_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when dep present
    SentenceTransformer = None  # type: ignore
    _ST_AVAILABLE = False


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


class Embedder:
    """Unified embedding interface.

    Parameters
    ----------
    model_name:
        sentence-transformers model id (used only when the lib is present).
    dim:
        Dimensionality of the hashing fallback vectors. Ignored when the
        real model is active (its native dimension is used instead).
    prefer_st:
        Set False to force the deterministic fallback (handy for tests).
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        dim: int = 256,
        prefer_st: bool = True,
    ) -> None:
        self.model_name = model_name
        self.fallback_dim = max(16, int(dim))
        self._model = None
        self._use_st = bool(prefer_st and _ST_AVAILABLE)
        self.backend = "sentence_transformers" if self._use_st else "hashing"

    # -- lifecycle ---------------------------------------------------------
    def _ensure_model(self) -> None:
        if self._use_st and self._model is None:
            try:
                self._model = SentenceTransformer(self.model_name)  # type: ignore
            except Exception:
                # Network/model load failure -> degrade to hashing.
                self._use_st = False
                self.backend = "hashing"

    @property
    def dim(self) -> int:
        if self._use_st:
            self._ensure_model()
            if self._model is not None:
                try:
                    return int(self._model.get_sentence_embedding_dimension())
                except Exception:
                    pass
        return self.fallback_dim

    # -- encoding ----------------------------------------------------------
    def embed(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        texts = list(texts)
        if not texts:
            return []
        if self._use_st:
            self._ensure_model()
            if self._model is not None:
                try:
                    vecs = self._model.encode(  # type: ignore
                        texts, normalize_embeddings=True, show_progress_bar=False
                    )
                    return [list(map(float, v)) for v in vecs]
                except Exception:
                    self._use_st = False
                    self.backend = "hashing"
        return [self._hash_embed(t) for t in texts]

    # -- deterministic fallback -------------------------------------------
    def _hash_embed(self, text: str) -> List[float]:
        """Hashing-trick bag-of-words embedding, L2-normalized.

        Deterministic, offline, and good enough for cosine similarity over
        short chunks. Uses signed feature hashing to reduce collisions.
        """
        dim = self.fallback_dim
        vec = [0.0] * dim
        tokens = _tokenize(text)
        if not tokens:
            return vec
        for tok in tokens:
            h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % dim
            sign = 1.0 if (h[4] & 1) else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two vectors. Returns 0.0 on degenerate input."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        x = a[i]
        y = b[i]
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def embedding_available() -> bool:
    """True if the real sentence-transformers backend is importable."""
    return _ST_AVAILABLE


__all__ = ["Embedder", "cosine_similarity", "embedding_available"]
