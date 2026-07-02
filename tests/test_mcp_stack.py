"""MCP server stack (wave-2 §3.3, the first LLM-era app type).

An ``mcp`` build is a Python stdio Model Context Protocol server (run with
``python server.py``) — NOT a web app. Its proof story is fully deterministic
(zero LLM): the mcp_check gate speaks the JSON-RPC protocol to the delivered
server. This module pins the stack vocabularies that must all agree or an
"mcp server" brief silently falls back to the React/FastAPI scaffold (the
"3-stack-vocabulary gotcha"):

  1. ``skyn3t.studio.planner``     — signature + file checklist (selection layer)
  2. ``skyn3t.agents._common``     — KNOWN_STACKS + normalization + keyword detect
  3. ``skyn3t.agents._scaffold``   — a real stdio MCP server (NOT React, NO npm)

Plus the planner menu (REAL_BUILDER_STACKS), the app-type/engine classification,
proof-run entrypoint recognition, and gate EXCLUSION (an MCP server is not a
web/game/SEO target). Offline-first: these assert on the in-memory scaffold dict
and the registries — no network, no subprocess. Mirrors ``test_swift_stack.py``.
"""

from __future__ import annotations

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
def test_planner_detects_mcp_briefs_as_mcp():
    assert plan_detect_stack("an MCP server that exposes my notes to Claude") == "mcp"
    assert plan_detect_stack("a model context protocol server for weather data") == "mcp"
    assert plan_detect_stack("build an mcp tool server for querying a database") == "mcp"
    assert plan_detect_stack("an MCP server exposing filesystem tools") == "mcp"


def test_planner_checklist_for_mcp():
    cl = file_checklist("mcp")
    for required in ("README.md", "server.py"):
        assert required in cl, f"{required} missing from mcp checklist"


# ---- 2. agent (_common) vocabulary ---------------------------------------
def test_common_knows_mcp_stack():
    assert "mcp" in KNOWN_STACKS


def test_common_normalizes_mcp_aliases():
    for alias in (
        "mcp", "mcp server", "mcp_server", "model context protocol",
        "model_context_protocol", "mcp tool server",
    ):
        assert _normalize_stack(alias) == "mcp", alias


def test_common_detects_mcp_keywords():
    assert agent_detect_stack(brief="an mcp server for my notes") == "mcp"
    assert agent_detect_stack(brief="a model context protocol tool server") == "mcp"
    assert agent_detect_stack(plan={"stack": "mcp"}) == "mcp"


# ---- keyword NON-theft, both directions ----------------------------------
def test_mcp_keywords_do_not_steal_non_mcp_briefs():
    # An ordinary "server"/"tool" brief with NO explicit MCP signal must never
    # route to the MCP stack — only "mcp"/"model context protocol" do.
    for brief in (
        "a rest api server for my todo app",
        "a simple command-line tool for organizing files",
        "a server monitoring dashboard",
        "a node api server with express",
    ):
        assert plan_detect_stack(brief) != "mcp", f"planner stole: {brief}"
        assert agent_detect_stack(brief=brief) != "mcp", f"_common stole: {brief}"


def test_mcp_briefs_do_not_leak_to_fastapi_or_express():
    # A genuine MCP brief contains "server"/"tool" (fastapi/express territory);
    # it must still resolve to mcp, not the generic HTTP-server stacks.
    for brief in (
        "an mcp server exposing weather tools",
        "a model context protocol tool server",
        "an mcp server for querying my postgres database",
    ):
        assert plan_detect_stack(brief) == "mcp", f"did not route to mcp: {brief}"
        assert agent_detect_stack(brief=brief) == "mcp", f"_common missed mcp: {brief}"


# ---- 3. scaffold produces a REAL stdio MCP server (NOT React, NO npm) -----
def test_scaffold_mcp_is_runnable_shape():
    files = scaffold_for("mcp", "notes-server", "an MCP server exposing my notes")
    for required in ("server.py", "tools.py", "requirements.txt", "README.md"):
        assert required in files, f"{required} missing from mcp scaffold"
    # A real multi-file server, not a one-file stub.
    assert len(files) >= 5

    server = files["server.py"]
    # A real MCP server using the official Python SDK + stdio run loop.
    assert "mcp" in server.lower(), "server.py must use the mcp SDK"
    assert "def " in server, "server.py must register real tool functions"
    assert '__name__ == "__main__"' in server, "server.py must have a runnable entry"

    # The pure logic core is split into tools.py and must be MCP-free (so it is
    # unit-testable without the SDK) — the sim-core split reapplied.
    tools = files["tools.py"]
    assert "import mcp" not in tools and "from mcp" not in tools, (
        "tools.py must be pure (no mcp import) so it is testable without the SDK"
    )
    assert "def " in tools, "tools.py must ship real functions"

    # A pytest test over the PURE functions ships with the scaffold.
    test_files = [k for k in files if k.startswith("tests/") and k.endswith(".py")]
    assert test_files, "mcp scaffold ships no pytest test for the pure tools"
    assert "def test_" in files[test_files[0]]

    # requirements.txt declares the mcp SDK runtime dep.
    assert "mcp" in files["requirements.txt"].lower()


def test_scaffold_mcp_has_no_npm_or_web_confusion():
    # An MCP server is a Python stdio program — it must NOT accidentally produce
    # the React/Vite web scaffold (the silent fallback this stack guards against).
    files = scaffold_for("mcp", "demo", "an mcp server")
    assert "package.json" not in files
    assert "index.html" not in files
    assert "vite.config.js" not in files
    assert not any(k.endswith((".jsx", ".tsx", ".js")) for k in files), "mcp scaffold emitted web/JS files"


# ---- 4. proof-run: mcp is python-family, NOT node / NOT swift -------------
def test_mcp_is_not_a_node_or_swift_stack_for_proof():
    assert "mcp" not in _NODE_STACKS
    assert "mcp" not in _SWIFT_STACKS


def test_mcp_server_py_is_a_recognized_entrypoint():
    from skyn3t.agents import _verify_common as vc

    assert "server.py" in vc.ENTRYPOINT_NAMES


def test_proof_run_passes_mcp_scaffold_structurally(tmp_path):
    # Structural proof (no real build): the scaffold satisfies the checklist, has
    # substantive Python sources, and a recognised entrypoint (server.py). The
    # boot smoke soft-skips a missing mcp SDK (offline) rather than failing.
    files = scaffold_for("mcp", "demo", "an mcp server exposing tools")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(
        tmp_path,
        checklist=file_checklist("mcp"),
        stack="mcp",
    )
    assert res.passed, res.to_dict()


# ---- 5. planner menu + classification (selection) ------------------------
def test_mcp_is_a_real_builder_stack():
    assert "mcp" in REAL_BUILDER_STACKS
    assert "mcp" not in _COLLAPSE  # a real builder, not a collapse alias


def test_mcp_classifies_as_mcp_server_app_type():
    c = classify_build("an mcp server for my notes", "mcp")
    assert c.app_type == "mcp_server"
    assert c.engine == "mcp"


# ---- 6. gate EXCLUSION (an MCP server is not web/game/SEO) ----------------
def test_mcp_is_excluded_from_web_game_and_seo_gate_sets():
    from skyn3t.studio import runner as _runner
    from skyn3t.studio.seo_check import _SEO_WEB_STACKS

    assert "mcp" not in _runner._WEB_STACKS
    assert "mcp" not in _runner._UI_WEB_STACKS
    assert "mcp" not in _runner._DESIGN_STACKS
    assert "mcp" not in _runner._GAME_STACKS
    assert "mcp" not in _SEO_WEB_STACKS


# ---- 7. reconciliation across vocabularies -------------------------------
def test_mcp_resolves_in_all_vocabularies():
    assert plan_detect_stack("an mcp server for my notes") == "mcp"
    assert _normalize_stack("model context protocol") == "mcp"
    assert agent_detect_stack(brief="an mcp tool server") == "mcp"
    files = scaffold_for("mcp", "demo", "an mcp server")
    # An MCP-only marker the react scaffold never emits.
    assert "server.py" in files
    assert "package.json" not in files
