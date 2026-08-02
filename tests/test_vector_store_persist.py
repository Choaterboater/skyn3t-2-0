# tests/test_vector_store_persist.py
"""The in-memory backend used to ignore ``persist_path`` entirely: with
chromadb absent (the common case — it is declared but not installed) every
ingest evaporated at process exit. The memory backend must now mirror docs to
a bounded JSONL file: save on mutation, lazy-load on construction, atomic
writes, oldest-first eviction at the cap, and never raise on a corrupt store.
"""
from __future__ import annotations

import json

import skyn3t.rag.vector_store as vector_store_module
from skyn3t.rag.vector_store import VectorStore


def _store(tmp_path, collection: str = "skyn3t_rag") -> VectorStore:
    return VectorStore(collection=collection, persist_path=tmp_path, prefer_chroma=False)


def _file(tmp_path, collection: str = "skyn3t_rag"):
    return tmp_path / f"{collection}.memory.jsonl"


def test_ingest_survives_new_instance(tmp_path):
    vs = _store(tmp_path)
    assert vs.backend == "memory"
    n = vs.add(
        ["a", "b"],
        ["alpha doc", "beta doc"],
        [[1.0, 0.0], [0.0, 1.0]],
        metadatas=[{"source": "s1"}, {"source": "s2"}],
    )
    assert n == 2
    assert _file(tmp_path).exists()

    # Simulate a process restart: a fresh instance on the same directory.
    vs2 = _store(tmp_path)
    assert vs2.count() == 2
    hits = vs2.query([1.0, 0.0], top_k=1)
    assert hits and hits[0].id == "a"
    assert hits[0].text == "alpha doc"
    assert hits[0].metadata.get("source") == "s1"


def test_unicode_docs_round_trip(tmp_path):
    vs = _store(tmp_path)
    vs.add(["u"], ["héllo — wörld ★ 中文"], [[1.0, 0.0]])
    vs2 = _store(tmp_path)
    assert vs2.query([1.0, 0.0], top_k=1)[0].text == "héllo — wörld ★ 中文"


def test_corrupt_store_starts_empty_and_never_raises(tmp_path):
    _file(tmp_path).write_bytes(b"\x00\xff not json at all {{{\n\"half\n")
    vs = _store(tmp_path)  # must not raise
    assert vs.count() == 0
    # ...and the store keeps working afterwards.
    vs.add(["a"], ["alpha"], [[1.0, 0.0]])
    assert _store(tmp_path).count() == 1


def test_partially_corrupt_store_keeps_good_lines(tmp_path):
    good = json.dumps({"id": "a", "text": "alpha", "embedding": [1.0, 0.0], "metadata": {}})
    _file(tmp_path).write_text(good + "\n{bad line\n", encoding="utf-8")
    vs = _store(tmp_path)
    assert vs.count() == 1
    assert vs.query([1.0, 0.0], top_k=1)[0].id == "a"


def test_cap_evicts_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(vector_store_module, "_MEMORY_STORE_MAX_DOCS", 3)
    vs = _store(tmp_path)
    for i in range(5):
        vs.add([f"d{i}"], [f"doc {i}"], [[float(i), 1.0]])
    assert vs.count() == 3
    assert "d0" not in vs._docs and "d1" not in vs._docs  # oldest evicted
    assert {"d2", "d3", "d4"} == set(vs._docs)
    # The persisted file is bounded too, and a restart sees the same docs.
    lines = [l for l in _file(tmp_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3
    assert set(_store(tmp_path)._docs) == {"d2", "d3", "d4"}


def test_delete_by_source_is_persisted(tmp_path):
    vs = _store(tmp_path)
    vs.add(
        ["a", "b"],
        ["alpha", "beta"],
        [[1.0, 0.0], [0.0, 1.0]],
        metadatas=[{"source": "s1"}, {"source": "s2"}],
    )
    assert vs.delete_by_source("s1") == 1
    vs2 = _store(tmp_path)
    assert vs2.count() == 1
    assert vs2.query([1.0, 0.0], top_k=5)[0].id == "b"


def test_reset_removes_the_persistent_file(tmp_path):
    vs = _store(tmp_path)
    vs.add(["a"], ["alpha"], [[1.0, 0.0]])
    assert _file(tmp_path).exists()
    vs.reset()
    assert not _file(tmp_path).exists()
    assert _store(tmp_path).count() == 0


def test_no_persist_path_stays_pure_memory(tmp_path):
    # Without persist_path nothing touches disk (old in-memory behavior).
    vs = VectorStore(prefer_chroma=False)
    vs.add(["a"], ["alpha"], [[1.0, 0.0]])
    assert vs.query([1.0, 0.0], top_k=1)[0].id == "a"


def test_chroma_semantics_unchanged_no_memory_file_written(tmp_path):
    # With a (fake) chroma backend active, the memory mirror must not engage:
    # no JSONL file, and docs written during a transient chroma failure stay
    # process-local exactly as before.
    class _Col:
        def __init__(self):
            self.docs = {}

        def upsert(self, ids, documents, embeddings, metadatas):
            for i, _id in enumerate(ids):
                self.docs[_id] = documents[i]

    vs = VectorStore(persist_path=tmp_path, prefer_chroma=False)
    vs._use_chroma = True
    vs.backend = "chromadb"
    vs._client = object()
    vs._collection = _Col()
    vs.add(["a"], ["alpha"], [[1.0, 0.0]])
    assert not _file(tmp_path).exists()
    assert vs._docs == {}  # batch went to chroma, not the memory mirror


def test_collections_do_not_share_a_file(tmp_path):
    _store(tmp_path, "one").add(["a"], ["alpha"], [[1.0, 0.0]])
    assert _store(tmp_path, "two").count() == 0
    assert _store(tmp_path, "one").count() == 1
