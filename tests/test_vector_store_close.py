# tests/test_vector_store_close.py
"""VectorStore must release its chroma client (an open SQLite handle for a
PersistentClient) — it had no close()/context manager, leaking file descriptors."""
from __future__ import annotations

from skyn3t.rag.vector_store import VectorStore


class _FakeClient:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def test_close_is_idempotent_and_safe():
    vs = VectorStore(prefer_chroma=False)
    vs.close()
    vs.close()  # must not raise even with no client


def test_close_releases_the_client():
    vs = VectorStore(prefer_chroma=False)
    fake = _FakeClient()
    vs._client = fake
    vs.close()
    assert fake.closed == 1
    assert vs._client is None  # released so it can't be reused


def test_context_manager_closes_on_exit():
    fake = _FakeClient()
    with VectorStore(prefer_chroma=False) as vs:
        vs._client = fake
        assert vs is not None
    assert fake.closed == 1
