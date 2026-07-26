"""Offline tests for the skyn3t.rag package.

No network, no heavy deps. Forces the deterministic fallbacks (hashing
embeddings, in-memory vector store, pure-Python BM25, regex repo map) so the
suite is fully reproducible regardless of which optional libs are installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import skyn3t.rag.repo_map as repo_map_module
from skyn3t.rag.document_processor import DocumentProcessor, detect_kind, estimate_tokens
from skyn3t.rag.embeddings import Embedder, cosine_similarity
from skyn3t.rag.rag_engine import RagEngine
from skyn3t.rag.repo_map import (
    RepoMapIndex,
    build_repo_context_pack,
    build_repo_map,
    clear_repo_context_caches,
    get_repo_map,
    hash_text,
)
from skyn3t.rag.retrieval import HybridRetriever
from skyn3t.rag.vector_store import VectorStore


@pytest.fixture
def isolated_repo_context_caches():
    clear_repo_context_caches()
    yield
    clear_repo_context_caches()


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


def test_context_pack_prioritizes_query_relevance_over_symbol_density(tmp_path: Path):
    (tmp_path / "dense.py").write_text(
        "\n".join(f"def unrelated_{index}(): return {index}" for index in range(80))
    )
    (tmp_path / "billing.py").write_text(
        "def recalculate_invoice_total(invoice):\n    return invoice.total\n"
    )

    pack = build_repo_context_pack(
        str(tmp_path),
        query="repair invoice total calculation",
        product_contract_version=3,
        max_tokens=160,
    )

    assert "# billing.py" in pack.context
    assert pack.context.index("# billing.py") < pack.context.find("# dense.py") or (
        "# dense.py" not in pack.context
    )
    assert "recalculate_invoice_total" in pack.context
    assert pack.product_contract_version == 3
    assert pack.requested_change_sha256 == hash_text(
        "repair invoice total calculation"
    )
    assert pack.ranking_algorithm == "lexical-path-symbol-content-v2"
    assert pack.estimated_tokens <= pack.max_tokens


def test_context_pack_honors_small_requested_token_budget(tmp_path: Path):
    (tmp_path / "billing.py").write_text(
        "def recalculate_invoice_total(invoice):\n    return invoice.total\n"
    )

    pack = build_repo_context_pack(
        str(tmp_path), query="repair invoice total calculation", max_tokens=7
    )

    assert pack.max_tokens == 7
    assert pack.estimated_tokens <= 7
    assert len(pack.context) <= 28


def test_context_pack_ranks_matching_symbols_within_dense_file(tmp_path: Path):
    source = "\n".join(
        [f"def unrelated_{index}(): return {index}" for index in range(80)]
        + ["def refresh_authentication_token(): return 'token'"]
    )
    (tmp_path / "services.py").write_text(source)

    pack = build_repo_context_pack(
        str(tmp_path),
        query="refresh authentication token",
        max_tokens=120,
    )

    assert "refresh_authentication_token" in pack.context


def test_context_pack_identity_is_stable_and_privacy_bounded(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    # Create the same tree in different filesystem order.
    (first / "z.py").write_text("def zebra(): return 1\n")
    (first / "a.py").write_text("def alpha(): return 2\n")
    (second / "a.py").write_text("def alpha(): return 2\n")
    (second / "z.py").write_text("def zebra(): return 1\n")
    goal = "Improve the alpha workflow without exposing this sentence"

    pack_a = build_repo_context_pack(
        str(first), query=goal, product_contract_version=2, max_tokens=256
    )
    pack_b = build_repo_context_pack(
        str(second), query=goal, product_contract_version=2, max_tokens=256
    )

    assert pack_a.context_key == pack_b.context_key
    assert pack_a.source_merkle_root == pack_b.source_merkle_root
    assert pack_a.context == pack_b.context
    assert str(first.resolve()) not in pack_a.context
    assert goal not in pack_a.context
    assert goal not in repr(pack_a.summary())

    changed_goal = build_repo_context_pack(
        str(first), query=f"{goal}.", product_contract_version=2, max_tokens=256
    )
    changed_contract = build_repo_context_pack(
        str(first), query=goal, product_contract_version=3, max_tokens=256
    )
    assert changed_goal.context_key != pack_a.context_key
    assert changed_contract.context_key != pack_a.context_key

    (first / "a.py").write_text("def alpha(): return 99\n")
    changed_source = build_repo_context_pack(
        str(first), query=goal, product_contract_version=2, max_tokens=256
    )
    assert changed_source.source_merkle_root != pack_a.source_merkle_root
    assert changed_source.context_key != pack_a.context_key


def test_context_pack_excludes_symlinked_source(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "safe.py").write_text("def safe_symbol(): return True\n")
    outside = tmp_path / "outside.py"
    outside.write_text("def private_external_secret(): return True\n")
    try:
        (project / "leak.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    pack = build_repo_context_pack(
        str(project), query="private external secret", max_tokens=256
    )

    assert pack.files_considered == 1
    assert "leak.py" not in pack.context
    assert "private_external_secret" not in pack.context


def test_context_pack_reports_deterministic_scan_truncation(tmp_path: Path):
    (tmp_path / "b.py").write_text("def b(): return 2\n")
    (tmp_path / "a.py").write_text("def a(): return 1\n")

    pack = build_repo_context_pack(
        str(tmp_path),
        query="function",
        max_tokens=128,
        max_files=1,
    )

    assert pack.scan_truncated is True
    assert pack.files_considered == 1
    assert "# a.py" in pack.context
    assert "# b.py" not in pack.context
    assert pack.summary()["scan_truncated"] is True


def test_repo_scan_bounds_non_code_and_directory_entry_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(repo_map_module, "_MAX_TRAVERSAL_ENTRIES", 5)
    for index in range(8):
        (tmp_path / f"junk-{index}.bin").write_bytes(b"junk")
    (tmp_path / "z_target.py").write_text(
        "def should_not_require_unbounded_walk(): return True\n"
    )

    repo_map = build_repo_map(str(tmp_path), max_files=2)

    assert repo_map.scan_truncated is True
    assert len(repo_map.files) <= 2


def test_context_pack_supports_generated_app_component_extensions(tmp_path: Path):
    (tmp_path / "AccountView.vue").write_text(
        "<script>\nexport function loadAccount(){ return true }\n</script>\n"
    )
    (tmp_path / "theme.css").write_text(".account { color: blue; }\n")
    (tmp_path / "Profile.swift").write_text(
        "struct Profile {\n  func displayName() -> String { \"Sky\" }\n}\n"
    )

    pack = build_repo_context_pack(
        str(tmp_path), query="account profile display theme", max_tokens=512
    )

    assert pack.files_considered == 3
    assert "AccountView.vue" in pack.context
    assert "Profile.swift" in pack.context
    assert "theme.css" in pack.context


def test_context_pack_ranks_markup_style_and_config_content(tmp_path: Path):
    (tmp_path / "dense.py").write_text(
        "\n".join(f"def unrelated_{index}(): return {index}" for index in range(80))
    )
    (tmp_path / "styles.css").write_text(
        ".checkout-button { background: green; }\n"
    )
    (tmp_path / "settings.json").write_text(
        '{"checkoutButtonLabel": "Pay securely"}\n'
    )

    pack = build_repo_context_pack(
        str(tmp_path), query="checkout button label", max_tokens=160
    )

    css_index = pack.context.index("# styles.css")
    config_index = pack.context.index("# settings.json")
    dense_index = pack.context.find("# dense.py")
    assert dense_index == -1 or css_index < dense_index
    assert dense_index == -1 or config_index < dense_index


def test_context_pack_ignores_generated_output_before_file_limit(tmp_path: Path):
    generated = tmp_path / ".next"
    generated.mkdir()
    (generated / "artifact.js").write_text(
        "export function generatedArtifact(){ return false }\n"
    )
    authored = tmp_path / "app"
    authored.mkdir()
    (authored / "page.tsx").write_text(
        "export function AccountPage(){ return <main /> }\n"
    )

    pack = build_repo_context_pack(
        str(tmp_path), query="account page", max_tokens=256, max_files=1
    )

    assert pack.scan_truncated is False
    assert pack.files_considered == 1
    assert "app/page.tsx" in pack.context
    assert ".next" not in pack.context


def test_context_pack_merkle_tracks_oversized_files(tmp_path: Path):
    huge = tmp_path / "huge.py"
    huge.write_bytes(b"a" * 1_048_577)
    first = build_repo_context_pack(
        str(tmp_path), query="huge module", max_tokens=128
    )

    huge.write_bytes(b"b" * 1_048_577)
    second = build_repo_context_pack(
        str(tmp_path), query="huge module", max_tokens=128
    )

    assert first.files_considered == 1
    assert first.scan_truncated is False
    assert first.cacheable is True
    assert first.source_merkle_root != second.source_merkle_root
    assert first.context_key != second.context_key


def test_context_pack_reuses_bounded_structural_and_render_caches(tmp_path: Path):
    clear_repo_context_caches()
    (tmp_path / "account.py").write_text(
        "def account_summary(): return 'ready'\n"
    )

    first = build_repo_context_pack(
        str(tmp_path),
        query="account summary",
        product_contract_version=4,
        max_tokens=256,
    )
    second = build_repo_context_pack(
        str(tmp_path),
        query="account summary",
        product_contract_version=4,
        max_tokens=256,
    )
    different_goal = build_repo_context_pack(
        str(tmp_path),
        query="account details",
        product_contract_version=4,
        max_tokens=256,
    )

    assert first.cache_hit is False
    assert first.structural_cache_hits == 0
    assert second.cache_hit is True
    assert second.structural_cache_hits == 1
    assert second.context_key == first.context_key
    assert different_goal.cache_hit is False
    assert different_goal.structural_cache_hits == 1
    assert different_goal.context_key != first.context_key


def test_context_pack_reserves_index_budget_for_late_relevant_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_repo_context_caches,
):
    monkeypatch.setattr(repo_map_module, "_TS_AVAILABLE", False)
    monkeypatch.setattr(repo_map_module, "_MAX_TOTAL_INDEX_ITEMS", 4)
    monkeypatch.setattr(repo_map_module, "_MAX_TOTAL_SEARCH_TERMS", 4)
    (tmp_path / "a_dense.py").write_text(
        "\n".join(
            f"def unrelated_symbol_{index}(): return {index}"
            for index in range(20)
        )
    )
    (tmp_path / "z_target.py").write_text(
        "\n".join(
            [f"def z_unrelated_{index}(): return {index}" for index in range(12)]
            + [
                "def late_relevant_checkout_target():",
                "    return 'late relevant checkout target'",
            ]
        )
    )

    pack = build_repo_context_pack(
        str(tmp_path),
        query="late relevant checkout target",
        max_tokens=160,
    )

    assert pack.scan_truncated is True
    assert pack.cacheable is False
    assert pack.index_items <= 4
    assert pack.search_terms_indexed <= 4
    assert "# z_target.py" in pack.context
    dense_index = pack.context.find("# a_dense.py")
    assert dense_index == -1 or pack.context.index("# z_target.py") < dense_index
    assert "late_relevant_checkout_target" in pack.context


def test_context_pack_retains_exact_query_term_after_file_lexical_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_repo_context_caches,
):
    monkeypatch.setattr(repo_map_module, "_TS_AVAILABLE", False)
    for index in range(8):
        (tmp_path / f"a_{index}.css").write_text(
            f".ordinary-{index} {{ color: black; }}\n"
        )
    early_terms = " ".join(f"token{index:04d}" for index in range(1024))
    (tmp_path / "z_target.css").write_text(
        f"/* {early_terms} ultrauniquelateterm */\n"
        ".target { color: rebeccapurple; }\n"
    )

    pack = build_repo_context_pack(
        str(tmp_path),
        query="ultrauniquelateterm",
        max_tokens=64,
    )

    assert pack.scan_truncated is True
    assert pack.cacheable is False
    assert "# z_target.css" in pack.context
    first_file_header = next(
        line for line in pack.context.splitlines() if line.startswith("# ")
    )
    assert first_file_header == "# z_target.css"


def test_deferred_cache_writes_preserve_later_structural_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_repo_context_caches,
):
    monkeypatch.setattr(repo_map_module, "_TS_AVAILABLE", False)
    monkeypatch.setattr(repo_map_module, "_FILE_MAP_CACHE_MAX", 6)
    monkeypatch.setattr(repo_map_module, "_FILE_MAP_CACHE_MAX_WEIGHT", 10_000)
    first_project = tmp_path / "first"
    second_project = tmp_path / "second"
    first_project.mkdir()
    second_project.mkdir()
    for index in range(6):
        (first_project / f"a{index}.py").write_text(
            f"def first_project_{index}(): return {index}\n"
        )
    for index in range(2):
        (second_project / f"b{index}.py").write_text(
            f"def second_project_{index}(): return {index}\n"
        )

    initial = build_repo_context_pack(
        str(first_project), query="initial navigation", max_tokens=256
    )
    intervening = build_repo_context_pack(
        str(second_project), query="intervening navigation", max_tokens=256
    )
    rescanned = build_repo_context_pack(
        str(first_project), query="different navigation", max_tokens=256
    )

    assert initial.structural_cache_hits == 0
    assert intervening.structural_cache_hits == 0
    # The two early misses are published only after all six lookups, so they
    # cannot evict a2-a5 before those four resident entries are reused.
    assert rescanned.structural_cache_hits == 4


def test_structural_cache_weight_matches_stored_pathless_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_repo_context_caches,
):
    monkeypatch.setattr(repo_map_module, "_TS_AVAILABLE", False)
    monkeypatch.setattr(repo_map_module, "_FILE_MAP_CACHE_MAX", 100)
    monkeypatch.setattr(repo_map_module, "_FILE_MAP_CACHE_MAX_WEIGHT", 12)
    for index in range(12):
        (tmp_path / f"long_generated_module_name_{index}.py").write_text(
            f"def cached_symbol_{index}(): return {index}\n"
        )

    build_repo_context_pack(
        str(tmp_path), query="cache weight accounting", max_tokens=256
    )

    stored_weight = sum(
        repo_map_module._file_map_cache_weight(file_map)
        for file_map in repo_map_module._FILE_MAP_CACHE.values()
    )
    assert repo_map_module._FILE_MAP_CACHE_WEIGHT == stored_weight
    assert stored_weight <= repo_map_module._FILE_MAP_CACHE_MAX_WEIGHT


def test_pending_structural_cache_publication_buffer_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(repo_map_module, "_FILE_MAP_CACHE_MAX", 3)
    monkeypatch.setattr(repo_map_module, "_FILE_MAP_CACHE_MAX_WEIGHT", 18)
    pending = repo_map_module._PendingFileMapWrites()

    for index in range(20):
        pending.add(
            ("schema", f"digest-{index}", "text:regex"),
            repo_map_module.FileMap(
                path=f"very-long-path-{index}.css",
                language="css",
                sha256=f"digest-{index}",
                search_terms=(f"term-{index}",),
            ),
        )

    measured = sum(
        repo_map_module._file_map_cache_weight(file_map)
        for file_map in pending.maps.values()
    )
    assert len(pending.maps) <= 3
    assert pending.weight == measured
    assert pending.weight <= 18


def test_bounded_tree_sitter_walk_falls_back_with_regex_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_repo_context_caches,
):
    class _FakeNode:
        type = "module"
        start_point = (0, 0)
        start_byte = 0
        end_byte = 0

        def __init__(self, children=()):
            self.children = list(children)

        def child_by_field_name(self, _name):
            return None

    class _FakeParser:
        def __init__(self):
            self.parse_calls = 0

        def parse(self, _source):
            self.parse_calls += 1
            root = _FakeNode([_FakeNode(), _FakeNode(), _FakeNode()])
            return type("_FakeTree", (), {"root_node": root})()

    class _FakeLanguagePack:
        def __init__(self, parser):
            self.parser = parser

        def get_parser(self, _language):
            return self.parser

    parser = _FakeParser()
    monkeypatch.setattr(repo_map_module, "_TS_AVAILABLE", True)
    monkeypatch.setattr(
        repo_map_module,
        "tree_sitter_languages",
        _FakeLanguagePack(parser),
    )
    monkeypatch.setattr(repo_map_module, "_MAX_TREE_SITTER_NODES", 2)
    (tmp_path / "bounded.py").write_text(
        "def regex_fallback_target(): return True\n"
    )

    pack = build_repo_context_pack(
        str(tmp_path), query="regex fallback target", max_tokens=256
    )

    assert parser.parse_calls == 1
    assert pack.backend == "regex"
    assert "regex_fallback_target" in pack.context
    assert pack.scan_truncated is True
    assert pack.cacheable is False


def test_empty_tree_sitter_result_caches_regex_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_repo_context_caches,
):
    class _FakeRoot:
        type = "module"
        start_point = (0, 0)
        start_byte = 0
        end_byte = 0
        children = ()

        def child_by_field_name(self, _name):
            return None

    class _FakeParser:
        def __init__(self):
            self.parse_calls = 0

        def parse(self, _source):
            self.parse_calls += 1
            return type("_FakeTree", (), {"root_node": _FakeRoot()})()

    class _FakeLanguagePack:
        def __init__(self, parser):
            self.parser = parser

        def get_parser(self, _language):
            return self.parser

    parser = _FakeParser()
    monkeypatch.setattr(repo_map_module, "_TS_AVAILABLE", True)
    monkeypatch.setattr(
        repo_map_module,
        "tree_sitter_languages",
        _FakeLanguagePack(parser),
    )
    (tmp_path / "fallback.py").write_text(
        "def actual_regex_backend_target(): return True\n"
    )

    first = build_repo_context_pack(
        str(tmp_path), query="actual regex backend", max_tokens=256
    )
    second = build_repo_context_pack(
        str(tmp_path), query="different regex lookup", max_tokens=256
    )

    assert first.backend == "regex"
    assert second.backend == "regex"
    assert first.scan_truncated is False
    assert parser.parse_calls == 1
    assert second.structural_cache_hits == 1


def test_context_pack_total_hash_budget_is_honest_and_non_cacheable(
    tmp_path: Path,
    isolated_repo_context_caches,
):
    first_source = "def alpha_budget_target(): return 1\n"
    second_source = "def beta_budget_target(): return 2\n"
    (tmp_path / "a.py").write_text(first_source)
    (tmp_path / "b.py").write_text(second_source)
    budget = len(first_source.encode()) + 5

    first = build_repo_context_pack(
        str(tmp_path),
        query="budget target",
        max_tokens=256,
        max_total_hash_bytes=budget,
    )
    second = build_repo_context_pack(
        str(tmp_path),
        query="budget target",
        max_tokens=256,
        max_total_hash_bytes=budget,
    )

    assert first.files_considered == 2
    assert first.bytes_hashed == budget
    assert first.hash_budget_bytes == budget
    assert first.scan_truncated is True
    assert first.cacheable is False
    assert first.cache_hit is False
    assert second.cache_hit is False
    assert second.source_merkle_root == first.source_merkle_root


def test_incremental_index_recovers_after_root_disappears(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "a.py"
    source.write_text("def before_missing_root(): return True\n")
    index = RepoMapIndex(str(project))
    initial = index.scan()
    assert initial["changed"] == ["a.py"]

    source.unlink()
    project.rmdir()
    missing = index.scan()

    assert missing == {"changed": [], "removed": ["a.py"], "unchanged": []}
    assert index.repo_map().files == []
    assert index.merkle_root == hash_text("")
    assert index.scan_truncated is False

    project.mkdir()
    (project / "b.py").write_text("def after_missing_root(): return True\n")
    recovered = index.scan()

    assert recovered == {"changed": ["b.py"], "removed": [], "unchanged": []}
    assert [file_map.path for file_map in index.repo_map().files] == ["b.py"]
    assert "after_missing_root" in index.get_repo_map(query="after missing root")


def test_incremental_query_rebuild_retains_late_term_from_truncated_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_repo_context_caches,
):
    monkeypatch.setattr(repo_map_module, "_TS_AVAILABLE", False)
    for index in range(100):
        (tmp_path / f"a_{index:03d}.css").write_text(
            f".ordinary-{index} {{ color: black; }}\n"
        )
    early_terms = " ".join(f"token{index:04d}" for index in range(1024))
    (tmp_path / "z_target.css").write_text(
        f"/* {early_terms} ultrauniquelateterm */\n"
    )
    index = RepoMapIndex(str(tmp_path))
    index.scan()

    context = index.get_repo_map(
        query="ultrauniquelateterm",
        max_tokens=64,
    )

    assert index.scan_truncated is True
    first_file_header = next(
        line for line in context.splitlines() if line.startswith("# ")
    )
    assert first_file_header == "# z_target.css"


def test_repo_map_excludes_only_exact_root_internal_contract_and_proof_paths(
    tmp_path: Path,
):
    internal = tmp_path / ".skyn3t"
    internal.mkdir()
    (internal / "product.json").write_text('{"secret": "root contract"}\n')
    root_proof = internal / "proof-ladder"
    root_visual = internal / "visual-proof"
    root_proof.mkdir()
    root_visual.mkdir()
    (root_proof / "runner.py").write_text(
        "def root_internal_proof(): return False\n"
    )
    (root_visual / "runner.py").write_text(
        "def root_internal_visual(): return False\n"
    )

    authored = tmp_path / "src" / ".skyn3t"
    nested_proof = authored / "proof-ladder"
    nested_visual = authored / "visual-proof"
    nested_proof.mkdir(parents=True)
    nested_visual.mkdir()
    (authored / "product.json").write_text('{"feature": "authored contract"}\n')
    (nested_proof / "feature.py").write_text(
        "def nested_authored_proof(): return True\n"
    )
    (nested_visual / "feature.py").write_text(
        "def nested_authored_visual(): return True\n"
    )

    paths = {file_map.path for file_map in build_repo_map(str(tmp_path)).files}

    assert ".skyn3t/product.json" not in paths
    assert ".skyn3t/proof-ladder/runner.py" not in paths
    assert ".skyn3t/visual-proof/runner.py" not in paths
    assert "src/.skyn3t/product.json" in paths
    assert "src/.skyn3t/proof-ladder/feature.py" in paths
    assert "src/.skyn3t/visual-proof/feature.py" in paths


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
    engine = RagEngine(embedder=Embedder(prefer_st=False, dim=128), prefer_chroma=False)
    # force in-memory backends (deterministic regardless of chromadb install)
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
        embedder=Embedder(prefer_st=False, dim=64), llm_client=_FakeLLM(), prefer_chroma=False
    )
    engine.ingest_text("The orchestrator routes tasks via the event bus.", "d.txt")
    ans = asyncio.run(engine.answer("how are tasks routed?"))
    assert ans.backend == "llm"
    assert ans.model == "fake-model"
    assert "event bus" in ans.answer


def test_rag_engine_info():
    engine = RagEngine(embedder=Embedder(prefer_st=False), prefer_chroma=False)
    info = engine.info()
    assert info["vector_backend"] == "memory"
    assert "bm25_backend" in info


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
