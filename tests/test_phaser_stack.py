"""Phaser 3 + Vite game stack — a first-class buildable game stack.

Pins the THREE stack vocabularies that must agree or a game brief silently falls
back to the React web scaffold (the "3-stack-vocabulary gotcha"):

  1. ``skyn3t.studio.planner``     — signature + file checklist (selection layer)
  2. ``skyn3t.agents._common``     — KNOWN_STACKS + normalization + keyword detect
  3. ``skyn3t.agents._scaffold``   — a real VANILLA-JS Phaser app (NOT React)

Plus the planner menu (REAL_BUILDER_STACKS), proof-run node-stack handling, the
runner liveness/UI web-stack gates (which have no enumerating test of their own),
and the skill-library stack group.

Offline-first: no network, no subprocess. Asserts on the in-memory scaffold dict
and the deterministic local proof check. Mirrors ``test_mobile_stack.py``.
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
from skyn3t.studio.stack_selector import _COLLAPSE, REAL_BUILDER_STACKS


# ---- 1. planner vocabulary -----------------------------------------------
def test_planner_detects_game_briefs_as_phaser():
    assert plan_detect_stack("Build a 2D platformer game") == "phaser"
    assert plan_detect_stack("an arcade shooter where you dodge asteroids") == "phaser"
    assert plan_detect_stack("a browser game with a jumping dino") == "phaser"
    assert plan_detect_stack("a phaser game") == "phaser"


def test_planner_checklist_for_phaser():
    cl = file_checklist("phaser")
    for required in ("README.md", "package.json", "index.html", "src/main.js"):
        assert required in cl, f"{required} missing from phaser checklist"


# ---- 2. agent (_common) vocabulary ---------------------------------------
def test_common_knows_phaser_stack():
    assert "phaser" in KNOWN_STACKS


def test_common_normalizes_phaser_aliases():
    for alias in ("phaser", "phaser3", "phaserjs", "game"):
        assert _normalize_stack(alias) == "phaser", alias


def test_common_detects_game_keywords():
    assert agent_detect_stack(brief="a 2d arcade game to shoot asteroids") == "phaser"
    assert agent_detect_stack(brief="a platformer game") == "phaser"
    assert agent_detect_stack(plan={"stack": "phaser"}) == "phaser"


def test_game_keywords_do_not_steal_non_game_briefs():
    # Adversarial-review regression: a bare "game" token + substring matching of
    # "shooter"/"arcade" used to steal ordinary web briefs (board-game tracker,
    # video-game blog, troubleshooter tool). The keyword fallback must stay
    # conservative and defer these to the web/LLM path, NOT phaser.
    for brief in (
        "a board game night planner app",
        "a video game review blog",
        "a leaderboard website for our trivia game",
        "a game of thrones fan wiki",
        "a troubleshooter tool for network issues",
        "an endgame chess analyzer",
    ):
        assert plan_detect_stack(brief) != "phaser", f"planner stole: {brief}"
        assert agent_detect_stack(brief=brief) != "phaser", f"_common stole: {brief}"


# ---- 3. scaffold produces a REAL vanilla-JS Phaser app (NOT React) --------
def test_scaffold_phaser_is_runnable_shape():
    files = scaffold_for("phaser", "dino-run", "A dino jump game")
    for required in ("package.json", "index.html", "src/main.js", "README.md"):
        assert required in files, f"{required} missing from phaser scaffold"
    # delivered != empty: a real app, not a one-file stub.
    assert len(files) >= 4

    # package.json is valid JSON, declares the phaser dep + a real build script.
    pkg = json.loads(files["package.json"])
    assert "phaser" in pkg.get("dependencies", {})
    assert "build" in pkg.get("scripts", {})
    # Vite is the bundler (devDep); `npm run build` -> vite build -> dist/.
    assert "vite" in pkg.get("devDependencies", {})

    # main.js instantiates a REAL Phaser game with a Scene — not a placeholder.
    main = files["src/main.js"]
    assert "new Phaser.Game" in main
    assert "Phaser.Scene" in main

    # index.html wires the VANILLA JS entry, not a React/JSX one.
    assert "/src/main.js" in files["index.html"]

    # vite.config has NO react plugin (a Phaser game is not React).
    assert "plugin-react" not in files.get("vite.config.js", "")


def test_scaffold_phaser_has_no_react_confusion():
    # A Phaser game must NOT accidentally produce the React Vite scaffold (the
    # silent fallback this whole stack guards against).
    files = scaffold_for("phaser", "demo", "an arcade game")
    assert "src/main.jsx" not in files
    assert "src/App.jsx" not in files
    pkg = json.loads(files["package.json"])
    assert "react" not in pkg.get("dependencies", {})
    assert "react-dom" not in pkg.get("dependencies", {})


# ---- 4. proof-run handles phaser as a node stack -------------------------
def test_proof_run_passes_phaser_scaffold(tmp_path):
    files = scaffold_for("phaser", "demo", "a browser game")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(
        tmp_path,
        checklist=file_checklist("phaser"),
        stack="phaser",
    )
    assert res.passed, res.to_dict()


def test_phaser_is_a_node_stack_for_proof():
    assert "phaser" in _NODE_STACKS


# ---- 5. planner menu (selection) -----------------------------------------
def test_phaser_is_a_real_builder_stack():
    assert "phaser" in REAL_BUILDER_STACKS
    # phaser is a real builder, not a collapse-to-something-else alias.
    assert "phaser" not in _COLLAPSE


# ---- 6. three-vocabulary reconciliation ----------------------------------
def test_phaser_resolves_in_all_three_vocabularies():
    # planner -> phaser
    assert plan_detect_stack("a 2d platformer game") == "phaser"
    # _common normalize/detect -> phaser
    assert _normalize_stack("phaser") == "phaser"
    assert agent_detect_stack(brief="an arcade game") == "phaser"
    # scaffold builder is registered (no fallback to react_vite)
    files = scaffold_for("phaser", "demo", "a game")
    # a Phaser-only marker the react scaffold never emits
    assert "new Phaser.Game" in files["src/main.js"]


def test_skill_library_groups_phaser_aliases():
    aliases = _stack_aliases("phaser")
    assert {"phaser", "game"} <= aliases


# ---- 7. runner liveness / UI web-stack gates (no enumerating test) -------
def test_phaser_in_runner_web_stack_gates():
    # These two frozensets have no enumerating invariant of their own, so lock
    # phaser's membership here: _WEB_STACKS drives the liveness serve+probe +
    # design-token injection; _UI_WEB_STACKS drives the always-on '/' render gate.
    from skyn3t.studio.runner import _UI_WEB_STACKS, _WEB_STACKS

    assert "phaser" in _WEB_STACKS
    assert "phaser" in _UI_WEB_STACKS
