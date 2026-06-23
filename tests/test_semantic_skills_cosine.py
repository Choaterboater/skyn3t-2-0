# tests/test_semantic_skills_cosine.py
"""_cosine must not silently truncate mismatched-dimension vectors (zip stops at
the shorter), which yields a garbage score when backends mix (256 vs 384 dims)."""
from __future__ import annotations

from skyn3t.intelligence.semantic_skills import _cosine


def test_cosine_mismatched_dims_returns_zero():
    # 256-dim vs 384-dim (hashing vs sentence-transformers) are not comparable.
    assert _cosine([0.1] * 256, [0.1] * 384) == 0.0


def test_cosine_same_dims_is_dot_product():
    assert abs(_cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9
    assert abs(_cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
