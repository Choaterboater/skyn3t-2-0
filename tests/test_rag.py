"""Offline tests for the skyn3t.rag package.

No network, no heavy deps. Forces the deterministic fallbacks (hashing
embeddings, in-memory vector store, pure-Python BM25, regex repo map) so the
suite is fully reproducible regardless of which optional libs are installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from skyn3t.rag.document_processor import DocumentProcessor, detect_kind, estimate_tokens
from skyn3t.rag.embeddings import Embedder, cosine_similarity
from skyn3t.rag.rag_engine import RagEngine
from skyn3t.rag.repo_map import (
    RepoMapIndex,
    build_repo_map,
    get_repo_map,
    hash_text,
)
from skyn3t.rag.retrieval import HybridRetriever
from skyn3t.rag.vector_store import VectorStore


# -- embeddings ------------------------------------------------------------
def test_hashing_embedding_deterministic_and_normalized():
    emb = Embedder(prefer_st=False, dim=64)
    a = emb.embed("the quick brown fox")
    b = emb.embed("the quick brown fox")
    assert a == b
    assert len(a) == 64
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6
    # similar text closer than dissimilar
    sim_close = cosine_similarity(a, emb.embed("the quick brown dog"))
    sim_far = cosine_similarity(a, emb.embed("zzz totally unrelated payload"))
    assert sim_close > sim_far


def test_embed_batch_empty():
    assert Embedder(prefer_st=False).embed_batch([]) == []


# -- document processor ----------------------------------------------------
def test_detect_kind():
    assert detect_kind("foo.py") == "code"
    assert detect_kind("README.md") == "markdown"
    assert detect_kind("notes.txt") == "text"
    assert detect_kind(None) == "text"


def test_code_chunking_keeps_symbols():
    code = "import os\n\ndef a():\n    return 1\n\nclass B:\n    def m(self):\n        return 2\n"
    chunks = DocumentProcessor(max_tokens=64).process(code, source="x.py")
    assert chunks
    assert all(c.kind == "code" for c in chunks)
    joined = "\n".join(c.text for c in chunks)
    assert "def a" in joined and "class B" in joined


def test_markdown_and_token_budget():
    md = "# Title\n\npara one\n\n## Section\n\n" + ("word " * 500)
    chunks = DocumentProcessor(max_tokens=80).process(md, source="d.md")
    assert len(chunks) >= 2
    assert all(c.tokens <= 120 for c in chunks)


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd" * 10) >= 1


# -- vector store ----------------------------------------------------------
def test_in_memory_vector_store():
    vs = VectorStore(prefer_chroma=False)
    assert vs.backend == "memory"
    emb = Embedder(prefer_st=False, dim=64)
    texts = ["alpha beta", "gamma delta", "alpha gamma"]
    ids = [f"id{i}" for i in range(3)]
    vecs = emb.embed_batch(texts)
    n = vs.add(ids, texts, vecs)
    assert n == 3
    assert vs.count() == 3
    hits = vs.query(emb.embed("alpha beta"), top_k=2)
    assert hits and hits[0].id == "id0"


# -- hybrid retrieval ------------------------------------------------------
def test_hybrid_retriever_finds_relevant():
    r = HybridRetriever(
        embedder=Embedder(prefer_st=False, dim=128),
        vector_store=VectorStore(prefer_chroma=False),
        alpha=0.5,
    )
    docs = {
        "d1": "python async event loop coroutine scheduling",
        "d2": "baking sourdough bread requires patience and flour",
        "d3": "the orchestrator dispatches tasks to agents over an event bus",
    }
    r.add_documents(list(docs.keys()), list(docs.values()))
    hits = r.search("event bus agents orchestrator", top_k=2)
    assert hits
    assert hits[0].id == "d3"


def test_retriever_empty_query():
    r = HybridRetriever(
        embedder=Embedder(prefer_st=False),
        vector_store=VectorStore(prefer_chroma=False),
    )
    assert r.search("") == []


# -- repo map (P0) ---------------------------------------------------------
def test_regex_repo_map(tmp_path: Path):
    (tmp_path / "mod.py").write_text(
        "import os\nfrom sys import argv\n\n"
        "class Widget:\n    def render(self):\n        return 1\n\n"
        "def helper(x, y):\n    return x + y\n"
    )
    rmap = build_repo_map(str(tmp_path))
    names = {s.name for s in rmap.all_symbols()}
    assert "Widget" in names
    assert "helper" in names
    assert "render" in names
    methods = [s for s in rmap.all_symbols() if s.kind == "method"]
    assert any(m.name == "render" and m.parent == "Widget" for m in methods)


def test_get_repo_map_token_bounded(tmp_path: Path):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text(
            f"def func_{i}():\n    return {i}\n" * 20
        )
    ctx = get_repo_map(str(tmp_path), max_tokens=120)
    assert ctx
    # respect the budget roughly (4 chars/token heuristic)
    assert estimate_tokens(ctx) <= 200
    assert "Repo map" in ctx


# -- incremental Merkle index (P1) ----------------------------------------
def test_incremental_index_reparses_only_changed(tmp_path: Path):
    f1 = tmp_path / "a.py"
    f2 = tmp_path / "b.py"
    f1.write_text("def a():\n    return 1\n")
    f2.write_text("def b():\n    return 2\n")
    idx = RepoMapIndex(str(tmp_path))

    first = idx.scan()
    assert set(first["changed"]) == {"a.py", "b.py"}
    root1 = idx.merkle_root

    # no changes -> nothing re-parsed
    second = idx.scan()
    assert second["changed"] == []
    assert set(second["unchanged"]) == {"a.py", "b.py"}
    assert idx.merkle_root == root1

    # modify one file -> only that one re-parses and root changes
    f1.write_text("def a():\n    return 99\n")
    third = idx.scan()
    assert third["changed"] == ["a.py"]
    assert idx.merkle_root != root1

    # remove a file -> reported as removed
    f2.unlink()
    fourth = idx.scan()
    assert fourth["removed"] == ["b.py"]


def test_hash_text_stable():
    assert hash_text("hello") == hash_text("hello")
    assert hash_text("hello") != hash_text("world")


# -- rag engine end to end -------------------------------------------------
def test_rag_engine_offline_answer():
    engine = RagEngine(embedder=Embedder(prefer_st=False, dim=128))
    # force in-memory backends
    assert engine.store.backend == "memory"
    engine.ingest_text(
        "The SkyN3t orchestrator routes tasks to agents using an event bus.",
        source="doc1.txt",
    )
    engine.ingest_text(
        "Sourdough bread needs flour, water, salt, and a starter culture.",
        source="doc2.txt",
    )
    assert engine.document_count >= 2

    ans = asyncio.run(engine.answer("how are tasks routed to agents?", top_k=2))
    assert ans.backend == "extractive"
    assert ans.sources
    assert "orchestrator" in ans.sources[0].text.lower()
    d = ans.to_dict()
    assert d["query"] and d["sources"]


def test_rag_engine_llm_path():
    class _FakeResult:
        text = "Tasks are routed via the event bus."
        model = "fake-model"

    class _FakeLLM:
        async def complete(self, prompt, **kwargs):
            return _FakeResult()

    engine = RagEngine(
        embedder=Embedder(prefer_st=False, dim=64), llm_client=_FakeLLM()
    )
    engine.ingest_text("The orchestrator routes tasks via the event bus.", "d.txt")
    ans = asyncio.run(engine.answer("how are tasks routed?"))
    assert ans.backend == "llm"
    assert ans.model == "fake-model"
    assert "event bus" in ans.answer


def test_rag_engine_info():
    engine = RagEngine(embedder=Embedder(prefer_st=False))
    info = engine.info()
    assert info["vector_backend"] == "memory"
    assert "bm25_backend" in info


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
