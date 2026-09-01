# tests/test_rag_backend_degrade.py
"""A transient chroma failure permanently flipped VectorStore to the memory
backend, orphaning every document already persisted in chroma for the life of
the instance — and all rag degrade paths swallowed exceptions without logging.
The flip must be per-call and every degrade must emit a log event."""
from __future__ import annotations

import skyn3t.rag.embeddings as embeddings_module
import skyn3t.rag.vector_store as vector_store_module
from skyn3t.rag.embeddings import Embedder
from skyn3t.rag.rag_engine import RagEngine
from skyn3t.rag.vector_store import VectorStore


class _FakeCollection:
    """Chroma collection double: fails the first N calls, then succeeds."""

    def __init__(self, fail_upserts: int = 0, fail_queries: int = 0) -> None:
        self.fail_upserts = fail_upserts
        self.fail_queries = fail_queries
        self.docs: dict[str, str] = {}

    def upsert(self, ids, documents, embeddings, metadatas):
        if self.fail_upserts > 0:
            self.fail_upserts -= 1
            raise RuntimeError("database is locked")
        for i, _id in enumerate(ids):
            self.docs[_id] = documents[i]

    def query(self, query_embeddings, n_results):
        if self.fail_queries > 0:
            self.fail_queries -= 1
            raise RuntimeError("database is locked")
        ids = list(self.docs.keys())[:n_results]
        return {
            "ids": [ids],
            "documents": [[self.docs[i] for i in ids]],
            "metadatas": [[{} for _ in ids]],
            "distances": [[0.0 for _ in ids]],
        }


def _chroma_backed_store(collection) -> VectorStore:
    # Wire the double directly: _ensure_chroma() short-circuits to True when
    # a collection is already attached, so no chromadb install is needed.
    vs = VectorStore(prefer_chroma=False)
    vs._use_chroma = True
    vs.backend = "chromadb"
    vs._client = object()
    vs._collection = collection
    return vs


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[str] = []

    def warning(self, event, **kwargs):
        self.events.append(event)

    def debug(self, event, **kwargs):
        self.events.append(event)


# -- M9c: no permanent backend flip on transient failures -------------------
def test_transient_query_failure_falls_back_per_call_without_flip():
    col = _FakeCollection(fail_queries=1)
    vs = _chroma_backed_store(col)
    vs.add(["a"], ["alpha"], [[1.0, 0.0]])
    # First query: chroma raises -> in-memory scoring for this call only
    # (empty here because the doc lives in chroma).
    assert vs.query([1.0, 0.0]) == []
    assert vs._use_chroma is True
    assert vs.backend == "chromadb"
    # Second query reaches the recovered collection: the persisted doc is
    # still retrievable instead of being orphaned forever.
    hits = vs.query([1.0, 0.0])
    assert [h.id for h in hits] == ["a"]


def test_add_failure_keeps_batch_retrievable_without_flip():
    col = _FakeCollection(fail_upserts=1)
    vs = _chroma_backed_store(col)
    vs.add(["a"], ["alpha"], [[1.0, 0.0]])
    assert vs.backend == "chromadb"  # no permanent flip
    assert "a" in vs._docs  # failed batch preserved in the memory fallback
    vs.add(["b"], ["beta"], [[0.0, 1.0]])  # next write reaches chroma again
    assert "b" in col.docs


# -- H3b: degrade paths must log ------------------------------------------
def test_chroma_query_failure_is_logged(monkeypatch):
    rec = _RecordingLog()
    monkeypatch.setattr(vector_store_module, "log", rec)
    vs = _chroma_backed_store(_FakeCollection(fail_queries=1))
    vs.query([1.0, 0.0])
    assert "vector_store.chroma_query_failed" in rec.events


def test_chroma_add_failure_is_logged(monkeypatch):
    rec = _RecordingLog()
    monkeypatch.setattr(vector_store_module, "log", rec)
    vs = _chroma_backed_store(_FakeCollection(fail_upserts=1))
    vs.add(["a"], ["alpha"], [[1.0, 0.0]])
    assert "vector_store.chroma_add_failed" in rec.events


def test_encode_failure_degrades_to_hashing_and_logs(monkeypatch):
    class _BoomModel:
        def encode(self, texts, **kwargs):
            raise RuntimeError("model exploded")

    rec = _RecordingLog()
    monkeypatch.setattr(embeddings_module, "log", rec)
    emb = Embedder(prefer_st=False, dim=32)
    emb._use_st = True
    emb._model = _BoomModel()
    vecs = emb.embed_batch(["alpha"])
    assert emb.backend == "hashing"
    assert len(vecs[0]) == 32
    assert "embeddings.encode_failed" in rec.events


def test_model_load_failure_degrades_and_logs(monkeypatch):
    class _BoomST:
        def __init__(self, name):
            raise RuntimeError("no network")

    rec = _RecordingLog()
    monkeypatch.setattr(embeddings_module, "log", rec)
    monkeypatch.setattr(embeddings_module, "SentenceTransformer", _BoomST)
    emb = Embedder(prefer_st=False)
    emb._use_st = True
    emb._ensure_model()
    assert emb.backend == "hashing"
    assert "embeddings.model_load_failed" in rec.events


# -- H3b: engines must be closable -----------------------------------------
def test_rag_engine_close_releases_the_store_client():
    class _FakeClient:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    engine = RagEngine(embedder=Embedder(prefer_st=False), prefer_chroma=False)
    fake = _FakeClient()
    engine.store._client = fake
    engine.close()
    engine.close()  # idempotent
    assert fake.closed == 1
    assert engine.store._client is None
