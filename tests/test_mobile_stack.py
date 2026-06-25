"""Phase C — react_native / Expo mobile stack.

These tests pin the THREE stack vocabularies that must agree or builds silently
fall back to the React web scaffold:

  1. ``skyn3t.studio.planner``     — signature + file checklist (web-plan layer)
  2. ``skyn3t.agents._common``     — KNOWN_STACKS + normalization + keyword detect
  3. ``skyn3t.agents._scaffold``   — a real multi-file Expo app

Plus the skill-library stack group and the proof-run node-stack handling.

Offline-first: no network, no subprocess. We assert on the in-memory scaffold
dict and the deterministic local proof check.
"""

from __future__ import annotations

import json

from skyn3t.agents._common import (
    KNOWN_STACKS,
    _normalize_stack,
)
from skyn3t.agents._common import (
    detect_stack as agent_detect_stack,
)
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.intelligence.skill_library import _stack_aliases
from skyn3t.studio.planner import detect_stack as plan_detect_stack
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import _NODE_STACKS, proof_run


# ---- 1. planner vocabulary -----------------------------------------------
def test_planner_detects_mobile_briefs_as_react_native():
    assert plan_detect_stack("Build a mobile app for tracking habits") == "react_native"
    assert plan_detect_stack("an iOS app with a login screen") == "react_native"
    assert plan_detect_stack("an Android app that lists tasks") == "react_native"
    assert plan_detect_stack("a react native expo app") == "react_native"


def test_planner_checklist_for_react_native():
    cl = file_checklist("react_native")
    for required in ("README.md", "package.json", "app.json", "App.tsx"):
        assert required in cl, f"{required} missing from react_native checklist"


# ---- 2. agent (_common) vocabulary ---------------------------------------
def test_common_knows_react_native_stack():
    assert "react_native" in KNOWN_STACKS


def test_common_normalizes_mobile_aliases():
    for alias in ("mobile", "expo", "react-native", "react_native", "react native"):
        assert _normalize_stack(alias) == "react_native", alias


def test_common_detects_mobile_keywords():
    assert agent_detect_stack(brief="a mobile app to log workouts") == "react_native"
    assert agent_detect_stack(brief="an expo app") == "react_native"
    assert agent_detect_stack(plan={"stack": "react_native"}) == "react_native"


# ---- 3. scaffold produces a REAL multi-file Expo app ---------------------
def test_scaffold_react_native_is_runnable_shape():
    files = scaffold_for("react_native", "habit-tracker", "A habit tracker")
    for required in ("package.json", "app.json", "App.tsx", "README.md"):
        assert required in files, f"{required} missing from react_native scaffold"
    # delivered != empty: a real app, not a one-file stub.
    assert len(files) >= 4

    # package.json is valid JSON and declares expo deps + a typecheck script.
    pkg = json.loads(files["package.json"])
    assert "expo" in pkg.get("dependencies", {})
    assert "react-native" in pkg.get("dependencies", {})
    assert "typecheck" in pkg.get("scripts", {})

    # app.json is valid JSON with an expo block.
    appcfg = json.loads(files["app.json"])
    assert "expo" in appcfg

    # App.tsx implements a real default-exported screen, not a placeholder.
    app = files["App.tsx"]
    assert "export default" in app
    assert "react-native" in app

    # Does NOT ship an unrunnable jest test (no jest dep) that only inflates the
    # score — and package.json must not advertise a `test` script it can't run.
    assert not any("__tests__" in p or p.endswith(".test.tsx") for p in files), \
        "scaffold ships an unrunnable jest test (theater)"
    assert "test" not in pkg.get("scripts", {}), "package.json claims a jest test command but ships no jest"


def test_scaffold_react_native_has_no_python_or_web_entrypoint_confusion():
    # A mobile app must NOT accidentally produce a Vite index.html (the silent
    # fallback this whole task guards against).
    files = scaffold_for("react_native", "demo", "an iOS app")
    assert "index.html" not in files
    assert "vite.config.js" not in files


# ---- 4. proof-run handles react_native as a node stack -------------------
def test_proof_run_passes_react_native_scaffold(tmp_path):
    files = scaffold_for("react_native", "demo", "a mobile app")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(
        tmp_path,
        checklist=file_checklist("react_native"),
        stack="react_native",
    )
    assert res.passed, res.to_dict()


def test_react_native_is_a_node_stack_for_proof():
    assert "react_native" in _NODE_STACKS


# ---- 5. three-vocabulary reconciliation ----------------------------------
def test_react_native_resolves_in_all_three_vocabularies():
    # planner -> react_native
    assert plan_detect_stack("a mobile app") == "react_native"
    # _common normalize/detect -> react_native
    assert _normalize_stack("mobile") == "react_native"
    assert agent_detect_stack(brief="a mobile app") == "react_native"
    # scaffold builder is registered (no fallback to react_vite)
    files = scaffold_for("react_native", "demo", "a mobile app")
    assert "app.json" in files  # an Expo-only marker the web scaffold never emits


def test_skill_library_groups_mobile_aliases():
    aliases = _stack_aliases("react_native")
    assert {"react_native", "mobile", "expo"} <= aliases
