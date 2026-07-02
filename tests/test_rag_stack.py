"""RAG app stack (wave-2 §3.1, the demand-#1 LLM-era app type).

A ``rag`` build is a FastAPI "chat with your documents" app: ``/ingest`` +
``/query`` (zero-LLM retrieval) + ``/chat`` (LLM over the retrieved context via
the ``OPENAI_BASE_URL`` seam) + ``/health`` + ``/v1/stats`` (the claw-code RAG
health contract), with the chunker/retriever split into a PURE ``rag_core.py``
(the sim-core split reapplied) so its logic is unit-testable without a server.

This module pins the stack vocabularies that must all agree or a "chat with my
docs" brief silently falls back to the React/FastAPI scaffold (the recorded
"3-stack-vocabulary gotcha"):

  1. ``skyn3t.studio.planner``     — signature + file checklist (selection layer)
  2. ``skyn3t.agents._common``     — KNOWN_STACKS + normalization + keyword detect
  3. ``skyn3t.agents._scaffold``   — a real FastAPI RAG app with a pure core

Plus the planner menu (REAL_BUILDER_STACKS), the app-type/engine classification,
proof-run entrypoint recognition, and gate MEMBERSHIP (a RAG app serves an HTTP
UI, so liveness applies — like fastapi — but it is NOT a crawlable UI web stack).
Offline-first: these assert on the in-memory scaffold dict and the registries —
the ONE exception is the standalone pytest run of the scaffold's own
``test_rag_core.py`` (proving the pure core genuinely works). Mirrors
``test_mcp_stack.py``.
"""

from __future__ import annotations

import subprocess
import sys

from skyn3t.agents._common import (
    KNOWN_STACKS,
    _normalize_stack,
)
from skyn3t.agents._common import (
    detect_stack as agent_detect_stack,
)
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.studio.planner import detect_stack as plan_detect_stack
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import _NODE_STACKS, _SWIFT_STACKS, proof_run
from skyn3t.studio.stack_selector import _COLLAPSE, REAL_BUILDER_STACKS, classify_build


# ---- 1. planner vocabulary -----------------------------------------------
def test_planner_detects_rag_briefs_as_rag():
    assert plan_detect_stack("a RAG app to chat with my documents") == "rag"
    assert plan_detect_stack("chat with my pdf files using AI") == "rag"
    assert plan_detect_stack("build a knowledge base I can ask questions") == "rag"
    assert plan_detect_stack("a document q&a service over my notes") == "rag"
    assert plan_detect_stack("semantic search over my company handbook") == "rag"


def test_planner_checklist_for_rag():
    cl = file_checklist("rag")
    for required in ("README.md", "main.py", "rag_core.py", "requirements.txt"):
        assert required in cl, f"{required} missing from rag checklist"


# ---- 2. agent (_common) vocabulary ---------------------------------------
def test_common_knows_rag_stack():
    assert "rag" in KNOWN_STACKS


def test_common_normalizes_rag_aliases():
    for alias in (
        "rag", "rag_app", "retrieval_augmented_generation", "knowledge_base",
        "chat_with_docs", "document_qa",
    ):
        assert _normalize_stack(alias) == "rag", alias


def test_common_detects_rag_keywords():
    assert agent_detect_stack(brief="a rag app for my docs") == "rag"
    assert agent_detect_stack(brief="chat with my documents") == "rag"
    assert agent_detect_stack(plan={"stack": "rag"}) == "rag"


# ---- keyword NON-theft, both directions ----------------------------------
def test_rag_keywords_do_not_steal_non_rag_briefs():
    # An ordinary "search"/"document" brief with NO explicit RAG signal must
    # never route to the RAG stack — only the multi-word RAG phrases do. Bare
    # "search" / "document" are deliberately NOT keywords (the phaser lesson).
    for brief in (
        "a search bar for my e-commerce store",
        "document my api with swagger",
        "a full-text search index for my blog",
        "a react dashboard with a search filter",
    ):
        assert plan_detect_stack(brief) != "rag", f"planner stole: {brief}"
        assert agent_detect_stack(brief=brief) != "rag", f"_common stole: {brief}"


def test_rag_briefs_do_not_leak_to_fastapi_or_react():
    # A genuine RAG brief may contain "app"/"service"/"ai" (react/fastapi
    # territory); it must still resolve to rag, not the generic web stacks.
    for brief in (
        "a RAG app to chat with my documents",
        "chat with my pdf using ai",
        "a knowledge base web app for my team",
    ):
        assert plan_detect_stack(brief) == "rag", f"did not route to rag: {brief}"
        assert agent_detect_stack(brief=brief) == "rag", f"_common missed rag: {brief}"


# ---- 3. scaffold produces a REAL FastAPI RAG app (pure core split) --------
def test_scaffold_rag_is_runnable_shape():
    files = scaffold_for("rag", "docs-chat", "chat with my documents")
    for required in ("main.py", "rag_core.py", "requirements.txt", "README.md",
                     "corpus/seed.md", "test_rag_core.py"):
        assert required in files, f"{required} missing from rag scaffold"
    assert len(files) >= 6

    main = files["main.py"]
    # A real FastAPI app with the load-bearing routes + the health contract.
    assert "fastapi" in main.lower(), "main.py must use FastAPI"
    for route in ("/ingest", "/query", "/chat", "/health", "/v1/stats"):
        assert route in main, f"main.py is missing the {route} route"
    # The LLM seam is the configurable base URL, never a hardcoded provider/key.
    assert "OPENAI_BASE_URL" in main, "main.py must read the OPENAI_BASE_URL seam"

    # The pure retrieval core is split into rag_core.py and must be FastAPI/HTTP
    # free (unit-testable without a server) — the sim-core split reapplied.
    core = files["rag_core.py"]
    for banned in ("import fastapi", "from fastapi", "import uvicorn", "import httpx"):
        assert banned not in core, f"rag_core.py must be pure (found {banned!r})"
    assert "def " in core, "rag_core.py must ship real functions"

    # The shipped fixture corpus carries the planted marker fact the proof queries.
    assert "41.7" in files["corpus/seed.md"], "seed corpus is missing its marker fact"

    # A pytest test over the PURE core ships with the scaffold.
    assert "def test_" in files["test_rag_core.py"]

    # requirements.txt declares fastapi/uvicorn/httpx — nothing key-gated or native.
    reqs = files["requirements.txt"].lower()
    for dep in ("fastapi", "uvicorn", "httpx"):
        assert dep in reqs, f"requirements.txt is missing {dep}"


def test_scaffold_rag_has_no_npm_or_react_confusion():
    # A RAG app is a Python FastAPI server — it must NOT accidentally produce the
    # React/Vite web scaffold (the silent fallback this stack guards against).
    files = scaffold_for("rag", "demo", "a rag app")
    assert "package.json" not in files
    assert "vite.config.js" not in files
    assert not any(k.endswith((".jsx", ".tsx")) for k in files), "rag scaffold emitted React files"


def test_scaffold_rag_boots_and_queries_with_zero_keys():
    # The scaffold's PROMISE: /ingest + /query work with NO API keys. Prove it by
    # running the scaffold's OWN pure-core test suite standalone under this python.
    import tempfile
    from pathlib import Path

    files = scaffold_for("rag", "demo", "chat with my documents")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel, contents in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(contents)
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "test_rag_core.py"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, (
        f"scaffold test_rag_core.py failed:\n{proc.stdout}\n{proc.stderr}"
    )


def test_scaffold_rag_serves_a_root_chat_page():
    # The app must be USABLE from a browser as-is: '/' serves a self-contained
    # HTML chat page (liveness probes '/' on every WEB_STACKS build, and a bare
    # 404 root reads as a failed delivery even when the API works).
    files = scaffold_for("rag", "demo", "a rag app")
    main = files["main.py"]
    assert '@app.get("/"' in main, "main.py must serve a root page"
    assert "HTMLResponse" in main
    low = main.lower()
    assert "<!doctype html>" in low
    # The page drives the real API — not a static placeholder.
    for wired in ("/chat", "/ingest", "/v1/stats"):
        assert wired in main
    # Self-contained: no external assets/CDNs in the embedded page.
    assert "https://" not in low.split("<!doctype html>")[1], "chat page must not load external assets"


def test_scaffold_rag_streams_chat_over_sse():
    # Wave-2 §3.1 streaming tier: the same grounded answer over SSE, and the
    # chat page consumes it progressively via EventSource.
    files = scaffold_for("rag", "demo", "a rag app")
    main = files["main.py"]
    assert '@app.get("/chat/stream")' in main
    assert "text/event-stream" in main
    assert "[DONE]" in main, "the stream must terminate with data: [DONE]"
    assert "EventSource" in main, "the chat page must consume the SSE stream"


# ---- 4. proof-run: rag is python-family, NOT node / NOT swift -------------
def test_rag_is_not_a_node_or_swift_stack_for_proof():
    assert "rag" not in _NODE_STACKS
    assert "rag" not in _SWIFT_STACKS


def test_rag_main_py_is_a_recognized_entrypoint():
    from skyn3t.agents import _verify_common as vc

    assert "main.py" in vc.ENTRYPOINT_NAMES


def test_proof_run_passes_rag_scaffold_structurally(tmp_path):
    # Structural proof: the scaffold satisfies the checklist, has substantive
    # Python sources + a recognised entrypoint (main.py), and its own pure-core
    # pytest suite passes. Missing third-party deps soft-skip rather than fail.
    files = scaffold_for("rag", "demo", "chat with my documents")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(
        tmp_path,
        checklist=file_checklist("rag"),
        stack="rag",
        run_tests=True,
        enable_mock_llm=False,
    )
    assert res.passed, res.to_dict()


# ---- 5. planner menu + classification (selection) ------------------------
def test_rag_is_a_real_builder_stack():
    assert "rag" in REAL_BUILDER_STACKS
    assert "rag" not in _COLLAPSE  # a real builder, not a collapse alias


def test_rag_classifies_as_rag_app_type():
    c = classify_build("a rag app to chat with my docs", "rag")
    assert c.app_type == "rag_app"
    assert c.engine == "rag"


# ---- 6. gate MEMBERSHIP (a RAG app serves HTTP like fastapi) --------------
def test_rag_serves_http_like_fastapi_but_is_not_a_ui_web_stack():
    from skyn3t.core import stacks
    from skyn3t.studio import runner as _runner
    from skyn3t.studio.seo_check import _SEO_WEB_STACKS

    # In WEB_STACKS (liveness applies — it serves an HTTP app like fastapi).
    assert "rag" in _runner._WEB_STACKS
    assert "rag" in stacks.RAG_STACKS
    # NOT a crawlable UI web stack (an API+chat app whose '/' contract we don't
    # hard-gate) and never a game stack; seo_check (derived from UI_WEB_STACKS)
    # therefore soft-skips it, exactly like fastapi.
    assert "rag" not in _runner._UI_WEB_STACKS
    assert "rag" not in _runner._GAME_STACKS
    assert "rag" not in _SEO_WEB_STACKS


def test_rag_check_gate_is_registered_for_the_rag_stack():
    from skyn3t.core import stacks

    assert stacks.gate_applies("rag_check", "rag")
    assert not stacks.gate_applies("rag_check", "fastapi")


# ---- 7. reconciliation across vocabularies -------------------------------
def test_rag_resolves_in_all_vocabularies():
    assert plan_detect_stack("a rag app to chat with my docs") == "rag"
    assert _normalize_stack("retrieval_augmented_generation") == "rag"
    assert agent_detect_stack(brief="chat with my documents") == "rag"
    files = scaffold_for("rag", "demo", "a rag app")
    # A RAG-only marker the react scaffold never emits.
    assert "rag_core.py" in files
    assert "package.json" not in files
