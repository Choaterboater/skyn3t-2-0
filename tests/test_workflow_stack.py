"""Agent-workflow app stack (wave-2 §3.2, the demand-#2 LLM-era app type).

A ``workflow`` build is a FastAPI multi-step runner: ``POST /trigger`` (dry-run
by default, returning the ``{run_id, dry_run, status, brief, delivery}``
contract) + an append-only run ledger (``/runs``) + ``/health`` + ``/v1/stats``,
with the engine split into a PURE ``workflow_core.py`` (steps call ONLY
registered tools; typed error envelopes; the engine never raises — the sim-core
split reapplied).

Pins the stack vocabularies that must all agree (the recorded
"3-stack-vocabulary gotcha"), mirroring ``test_rag_stack.py``:

  1. ``skyn3t.studio.planner``     — signature + file checklist
  2. ``skyn3t.agents._common``     — KNOWN_STACKS + normalization + keyword detect
  3. ``skyn3t.agents._scaffold``   — a real FastAPI runner with a pure engine

Plus the planner menu, app-type/engine classification, proof recognition, and
gate MEMBERSHIP (HTTP-served → WEB_STACKS/liveness, NOT a crawlable UI stack).
Offline-first; the ONE exception is the standalone pytest run of the scaffold's
own ``test_workflow_core.py``.
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
def test_planner_detects_workflow_briefs():
    assert plan_detect_stack("an agent that monitors hacker news and sends me a daily briefing") == "workflow"
    assert plan_detect_stack("a multi-step workflow to process invoices") == "workflow"
    assert plan_detect_stack("a team of agents that research and write reports") == "workflow"
    assert plan_detect_stack("an automation pipeline for our nightly data sync") == "workflow"


def test_planner_checklist_for_workflow():
    cl = file_checklist("workflow")
    for required in ("README.md", "main.py", "workflow_core.py", "requirements.txt"):
        assert required in cl, f"{required} missing from workflow checklist"


# ---- keyword NON-theft, both directions ----------------------------------
def test_workflow_keywords_do_not_steal_non_workflow_briefs():
    # Plural "workflows" is a dashboard/CRUD noun; bare "automation"/"pipeline"/
    # "scheduled" are deliberately NOT keywords.
    for brief, not_stack in (
        ("a react dashboard for my workflows", "workflow"),
        ("a kanban board with an approval process", "workflow"),
        ("a landing page for my automation startup", "workflow"),
        ("a CI pipeline status page", "workflow"),
    ):
        assert plan_detect_stack(brief) != not_stack, f"planner stole: {brief}"
        assert agent_detect_stack(brief=brief) != not_stack, f"_common stole: {brief}"


def test_workflow_briefs_do_not_leak_to_fastapi_or_react():
    for brief in (
        "an agent workflow app for my team",
        "an agent that summarizes my inbox every morning",
        "a team of agents to triage support tickets",
    ):
        assert plan_detect_stack(brief) == "workflow", f"did not route: {brief}"
        assert agent_detect_stack(brief=brief) == "workflow", f"_common missed: {brief}"


# ---- 2. agent (_common) vocabulary ---------------------------------------
def test_common_knows_workflow_stack():
    assert "workflow" in KNOWN_STACKS


def test_common_normalizes_workflow_aliases():
    for alias in (
        "workflow", "workflow_app", "agent_workflow", "agent_team",
        "multi_agent", "automation",
    ):
        assert _normalize_stack(alias) == "workflow", alias


def test_common_detects_workflow_keywords():
    assert agent_detect_stack(brief="a workflow that posts a daily briefing") == "workflow"
    assert agent_detect_stack(plan={"stack": "workflow"}) == "workflow"


# ---- 3. scaffold produces a REAL FastAPI runner (pure engine split) -------
def test_scaffold_workflow_is_runnable_shape():
    files = scaffold_for("workflow", "briefing-bot", "an agent that sends daily briefings")
    for required in ("main.py", "workflow_core.py", "requirements.txt", "README.md",
                     "test_workflow_core.py"):
        assert required in files, f"{required} missing from workflow scaffold"

    main = files["main.py"]
    assert "fastapi" in main.lower(), "main.py must use FastAPI"
    for route in ("/trigger", "/runs", "/health", "/v1/stats"):
        assert route in main, f"main.py is missing the {route} route"
    # The spec's /trigger contract keys + safe-by-default dry_run.
    for key in ("run_id", "dry_run", "brief", "delivery"):
        assert key in main
    assert "dry_run: bool = True" in main, "dry_run must DEFAULT to true"
    # Delivery is optional env config, never a required key.
    assert "WEBHOOK_URL" in main

    core = files["workflow_core.py"]
    for banned in ("import fastapi", "from fastapi", "import uvicorn", "import httpx"):
        assert banned not in core, f"workflow_core.py must be pure (found {banned!r})"
    for contract in ("skipped_no_delivery", "dry_run", "retryable", "ToolRegistry"):
        assert contract in core, f"workflow_core.py missing the {contract} contract"

    reqs = files["requirements.txt"].lower()
    for dep in ("fastapi", "uvicorn"):
        assert dep in reqs, f"requirements.txt is missing {dep}"


def test_scaffold_workflow_serves_a_root_dashboard():
    files = scaffold_for("workflow", "demo", "an agent workflow app")
    main = files["main.py"]
    assert '@app.get("/"' in main, "main.py must serve a root page (liveness probes /)"
    assert "HTMLResponse" in main
    low = main.lower()
    assert "<!doctype html>" in low
    assert "https://" not in low.split("<!doctype html>")[1], "dashboard must not load external assets"


def test_scaffold_workflow_has_no_npm_or_react_confusion():
    files = scaffold_for("workflow", "demo", "an agent workflow app")
    assert "package.json" not in files
    assert not any(k.endswith((".jsx", ".tsx")) for k in files)


def test_scaffold_workflow_engine_passes_its_own_tests_with_zero_deps():
    # The engine's PROMISE: pure stdlib. Prove it by running the scaffold's OWN
    # pure-engine test suite standalone under this python.
    import tempfile
    from pathlib import Path

    files = scaffold_for("workflow", "demo", "an agent workflow app")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel, contents in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(contents, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "test_workflow_core.py"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, (
        f"scaffold test_workflow_core.py failed:\n{proc.stdout}\n{proc.stderr}"
    )


# ---- 4. proof-run: workflow is python-family, NOT node / NOT swift ---------
def test_workflow_is_not_a_node_or_swift_stack_for_proof():
    assert "workflow" not in _NODE_STACKS
    assert "workflow" not in _SWIFT_STACKS


def test_proof_run_passes_workflow_scaffold_structurally(tmp_path):
    files = scaffold_for("workflow", "demo", "an agent workflow app")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")
    res = proof_run(
        tmp_path,
        checklist=file_checklist("workflow"),
        stack="workflow",
        run_tests=True,
        enable_mock_llm=False,
    )
    assert res.passed, res.to_dict()


# ---- 5. planner menu + classification (selection) ------------------------
def test_workflow_is_a_real_builder_stack():
    assert "workflow" in REAL_BUILDER_STACKS
    assert "workflow" not in _COLLAPSE


def test_workflow_classifies_as_agent_workflow_app_type():
    c = classify_build("an agent that sends daily briefings", "workflow")
    assert c.app_type == "agent_workflow"
    assert c.engine == "workflow"


# ---- 6. gate MEMBERSHIP (HTTP-served like fastapi/rag) --------------------
def test_workflow_serves_http_but_is_not_a_ui_web_stack():
    from skyn3t.core import stacks
    from skyn3t.studio import runner as _runner
    from skyn3t.studio.seo_check import _SEO_WEB_STACKS

    assert "workflow" in _runner._WEB_STACKS
    assert "workflow" in stacks.WORKFLOW_STACKS
    assert "workflow" not in _runner._UI_WEB_STACKS
    assert "workflow" not in _runner._GAME_STACKS
    assert "workflow" not in _SEO_WEB_STACKS
    # The dedicated end-of-build gate is registered for THIS stack only.
    assert stacks.gate_applies("workflow_check", "workflow")
    assert not stacks.gate_applies("workflow_check", "fastapi")
    assert not stacks.gate_applies("rag_check", "workflow")
    assert not stacks.gate_applies("mcp_check", "workflow")


# ---- 7. reconciliation across vocabularies -------------------------------
def test_workflow_resolves_in_all_vocabularies():
    assert plan_detect_stack("a multi-step workflow to process invoices") == "workflow"
    assert _normalize_stack("agent_workflow") == "workflow"
    assert agent_detect_stack(brief="a team of agents that write reports") == "workflow"
    files = scaffold_for("workflow", "demo", "an agent workflow app")
    assert "workflow_core.py" in files
    assert "package.json" not in files
