# tests/test_rag_reingest.py
"""Chunk ids are content hashes, so re-ingesting an edited file minted new
ids while the old chunks stayed behind (VectorStore had no delete-by-source
API) — stale content kept surfacing as current context. Purging must stay
opt-in: experience ingestion accumulates distinct docs under one source."""
from __future__ import annotations

from pathlib import Path

from skyn3t.rag.embeddings import Embedder
from skyn3t.rag.rag_engine import RagEngine
from skyn3t.rag.vector_store import VectorStore


def _engine(alpha: float = 0.5, dim: int = 128) -> RagEngine:
    return RagEngine(
        embedder=Embedder(prefer_st=False, dim=dim),
        alpha=alpha,
        prefer_chroma=False,
    )


# -- engine-level replace-on-reingest ---------------------------------------
def test_reingest_file_replaces_stale_chunks(tmp_path: Path):
    engine = _engine()
    doc = tmp_path / "doc.txt"
    doc.write_text("The orchestrator routes tasks to agents over an event bus.")
    engine.ingest_file(str(doc))
    doc.write_text("Sourdough bread needs flour, water, salt, and a starter.")
    n = engine.ingest_file(str(doc))
    assert engine.document_count == n  # stale chunks purged, not accumulated
    hits = engine.query("orchestrator event bus agents", top_k=5)
    assert hits
    assert all("orchestrator" not in h.text.lower() for h in hits)


def test_reingest_purges_lexical_mirror(tmp_path: Path):
    # alpha=0.0 is lexical-only: a stale BM25 mirror would keep ranking the
    # deleted chunk even after the vector store forgot it.
    engine = _engine(alpha=0.0, dim=64)
    doc = tmp_path / "notes.txt"
    doc.write_text("zebra quagga unique lexical marker")
    engine.ingest_file(str(doc))
    doc.write_text("entirely different replacement body")
    engine.ingest_file(str(doc))
    hits = engine.query("zebra quagga marker", top_k=5)
    assert all("zebra" not in h.text for h in hits)


def test_ingest_text_same_source_accumulates_by_default():
    engine = _engine(dim=64)
    first = engine.ingest_text("first lesson about retries", source="experience")
    second = engine.ingest_text("second lesson about backoff", source="experience")
    assert engine.document_count == first + second


# -- store-level delete_by_source -------------------------------------------
def test_delete_by_source_memory_backend_tolerates_missing_metadata():
    vs = VectorStore(prefer_chroma=False)
    vs.add(
        ["a", "b", "c"],
        ["one", "two", "three"],
        [[1.0], [0.5], [0.2]],
        metadatas=[{"source": "s1"}, {"source": "s2"}, {}],
    )
    assert vs.delete_by_source("s1") == 1
    assert vs.count() == 2
    assert vs.delete_by_source("") == 0  # empty source never matches
    assert vs.count() == 2


def test_delete_by_source_issues_chroma_delete_and_never_raises():
    class _Collection:
        def __init__(self) -> None:
            self.deletes: list[dict] = []

        def delete(self, where):
            self.deletes.append(where)

    class _BoomCollection:
        def delete(self, where):
            raise RuntimeError("database is locked")

    def _store(collection) -> VectorStore:
        vs = VectorStore(prefer_chroma=False)
        vs._use_chroma = True
        vs.backend = "chromadb"
        vs._client = object()
        vs._collection = collection
        return vs

    col = _Collection()
    _store(col).delete_by_source("doc.txt")
    assert col.deletes == [{"source": "doc.txt"}]
    _store(_BoomCollection()).delete_by_source("doc.txt")  # must not raise
