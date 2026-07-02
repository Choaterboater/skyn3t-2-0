"""Agent team pack stack (wave-2 §3.8, the persona-roster app type).

An ``agent_pack`` build is a ROSTER product, not a running app: markdown
personas under ``agents/<division>/`` with enforced structure (frontmatter +
Identity / Core Mission / Critical Rules), a ``catalog.json`` manifest, and
pure lint/originality/convert tooling — zero runtime deps. The proof story is
fully deterministic: the pack's OWN pytest runs the lint gate, the pairwise
shingle-originality check, and the convert round-trip.

Pins the stack vocabularies (the "3-stack-vocabulary gotcha"), mirroring
``test_rag_stack.py`` / ``test_workflow_stack.py``.
"""

from __future__ import annotations

import json
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
def test_planner_detects_pack_briefs():
    assert plan_detect_stack("an agent team pack for my law firm") == "agent_pack"
    assert plan_detect_stack("custom agents for my trading desk") == "agent_pack"
    assert plan_detect_stack("a subagents pack for code review") == "agent_pack"
    assert plan_detect_stack("writing personas for my newsletter team") == "agent_pack"


def test_planner_checklist_for_pack():
    cl = file_checklist("agent_pack")
    for required in ("README.md", "main.py", "catalog.json", "pack_tools.py"):
        assert required in cl, f"{required} missing from agent_pack checklist"


# ---- keyword NON-theft, both directions ----------------------------------
def test_pack_keywords_do_not_steal_runtime_or_web_briefs():
    # Runtime multi-agent behavior stays with `workflow`; bare "persona"/
    # "agents" briefs stay with their real stacks.
    assert plan_detect_stack("a team of agents that triage tickets") == "workflow"
    assert plan_detect_stack("an agent that sends daily briefings") == "workflow"
    for brief in (
        "a persona quiz web app",
        "a react dashboard showing user agents",
    ):
        assert plan_detect_stack(brief) != "agent_pack", f"planner stole: {brief}"
        assert agent_detect_stack(brief=brief) != "agent_pack", f"_common stole: {brief}"


# ---- 2. agent (_common) vocabulary ---------------------------------------
def test_common_knows_pack_stack():
    assert "agent_pack" in KNOWN_STACKS


def test_common_normalizes_pack_aliases():
    for alias in ("agent_pack", "agents_pack", "agent_team_pack",
                  "persona_pack", "personas", "subagents", "agent_roster"):
        assert _normalize_stack(alias) == "agent_pack", alias


def test_common_detects_pack_keywords():
    assert agent_detect_stack(brief="an agent team pack for my law firm") == "agent_pack"
    assert agent_detect_stack(plan={"stack": "agent_pack"}) == "agent_pack"


# ---- 3. scaffold produces a REAL pack (pure tooling split) ----------------
def test_scaffold_pack_is_complete_shape():
    files = scaffold_for("agent_pack", "law-pack", "an agent team pack for my law firm")
    for required in ("main.py", "pack_tools.py", "catalog.json", "README.md",
                     "test_agent_pack.py"):
        assert required in files, f"{required} missing from agent_pack scaffold"
    personas = [k for k in files if k.startswith("agents/") and k.endswith(".md")]
    assert len(personas) >= 3, "the pack must ship example personas"
    for rel in personas:
        text = files[rel]
        assert text.startswith("---"), f"{rel} missing frontmatter"
        for section in ("## Identity", "## Core Mission", "## Critical Rules"):
            assert section in text, f"{rel} missing {section}"
    catalog = json.loads(files["catalog.json"])
    listed = {a for div in catalog["divisions"].values() for a in div}
    assert listed == {rel.rsplit("/", 1)[-1][:-3] for rel in personas}, (
        "catalog.json must list exactly the shipped personas")
    # A roster product, never a running app.
    assert "package.json" not in files
    core = files["pack_tools.py"]
    for banned in ("import fastapi", "from fastapi", "import uvicorn", "import httpx"):
        assert banned not in core


def test_scaffold_pack_own_proof_passes_with_zero_deps():
    # Lint gate + originality + convert round-trip, standalone under this python.
    import tempfile
    from pathlib import Path

    files = scaffold_for("agent_pack", "demo", "an agent team pack")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel, contents in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(contents)
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "test_agent_pack.py"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, (
            f"pack proof failed:\n{proc.stdout}\n{proc.stderr}")
        # The CLI is genuinely runnable and the shipped roster lints clean.
        lint = subprocess.run(
            [sys.executable, "-B", "main.py", "lint"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
        assert lint.returncode == 0, lint.stdout + lint.stderr


# ---- 4. proof-run: python family, NOT node / NOT swift ---------------------
def test_pack_is_not_a_node_or_swift_stack_for_proof():
    assert "agent_pack" not in _NODE_STACKS
    assert "agent_pack" not in _SWIFT_STACKS


def test_proof_run_passes_pack_scaffold_structurally(tmp_path):
    files = scaffold_for("agent_pack", "demo", "an agent team pack")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(
        tmp_path,
        checklist=file_checklist("agent_pack"),
        stack="agent_pack",
        run_tests=True,
        enable_mock_llm=False,
    )
    assert res.passed, res.to_dict()


# ---- 5. planner menu + classification -------------------------------------
def test_pack_is_a_real_builder_stack():
    assert "agent_pack" in REAL_BUILDER_STACKS
    assert "agent_pack" not in _COLLAPSE


def test_pack_classifies_as_agent_pack_app_type():
    c = classify_build("an agent team pack for my law firm", "agent_pack")
    assert c.app_type == "agent_pack"


# ---- 6. gate MEMBERSHIP: a roster product belongs to ZERO groups -----------
def test_pack_is_not_a_web_ui_or_game_stack():
    from skyn3t.core import stacks
    from skyn3t.studio import runner as _runner

    assert "agent_pack" not in _runner._WEB_STACKS  # no HTTP server → no liveness
    assert "agent_pack" not in _runner._UI_WEB_STACKS
    assert "agent_pack" not in _runner._GAME_STACKS
    groups = [n for n, g in stacks.GROUPS.items() if "agent_pack" in g]
    assert groups == [], f"a roster product belongs to no gate group: {groups}"


# ---- 7. reconciliation across vocabularies ---------------------------------
def test_pack_resolves_in_all_vocabularies():
    assert plan_detect_stack("an agent team pack for my law firm") == "agent_pack"
    assert _normalize_stack("persona_pack") == "agent_pack"
    assert agent_detect_stack(brief="a subagents pack for code review") == "agent_pack"
    files = scaffold_for("agent_pack", "demo", "an agent pack")
    assert "pack_tools.py" in files
    assert "package.json" not in files
