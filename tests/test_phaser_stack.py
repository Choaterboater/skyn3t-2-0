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
    vite_config = files.get("vite.config.js", "")
    assert "plugin-react" not in vite_config
    assert "chunkSizeWarningLimit" in vite_config


def test_phaser_scaffold_has_pure_sim_render_split():
    # The headless invariant gate (roadmap #5) requires a PURE sim core separate
    # from Phaser. The scaffold ships src/sim.js exporting the contract, and
    # src/main.js (the Phaser scene) consumes it — it does NOT own the logic.
    files = scaffold_for("phaser", "dino-run", "a dino jump game")
    assert "src/sim.js" in files, "phaser scaffold must ship a pure src/sim.js"
    sim = files["src/sim.js"]
    for export in ("createState", "step", "isWin", "isLose"):
        assert export in sim, f"sim.js missing {export} export"
    # sim.js is PURE — it must not IMPORT Phaser (else it can't run headless in
    # Node). Comments may still mention Phaser; we forbid the actual import.
    assert "import Phaser" not in sim, "sim.js must not import Phaser"
    assert "from 'phaser'" not in sim, "sim.js must not import from phaser"
    # main.js consumes the sim core rather than owning the logic.
    main = files["src/main.js"]
    assert "./sim" in main, "main.js must import the sim core"
    assert "step(" in main, "main.js must call step()"


def test_phaser_dino_runner_scaffold_renders_cacti_and_enemy_sprite():
    files = scaffold_for(
        "phaser",
        "dino-runner",
        "a phaser browser game where a dino jumps over cacti to score points",
        art=True,
    )

    main = files["src/main.js"]
    sim = files["src/sim.js"]

    assert "cacti" in main.lower() or "cactus" in main.lower()
    assert "dino" in main.lower()
    assert "this.load.image('enemy'" in main
    assert "this.add.sprite(0, 0, 'enemy')" in main
    assert "this.load.image('coin'" in main
    assert "this.add.sprite(0, 0, 'coin')" in main
    assert "obstacles" in sim
    assert "jump" in sim.lower()


def test_phaser_codegen_prompt_pins_dt_in_seconds():
    # A model writing its OWN Phaser scene (not the scaffold's) drifted to passing
    # Phaser's raw delta (MILLISECONDS) into step(), so the headless gate (dt=1/60
    # SECONDS) ran the game ~1000x too slow and never exercised it (a false PASS).
    # The agentic codegen prompt must restate the seconds contract the scaffold pins.
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    p = CodeAgent(event_bus=EventBus())._agentic_prompt(
        "a brick breaker game", "phaser", {"files": []}, "K ")
    assert "delta / 1000" in p, "prompt must tell the scene to pass delta/1000"
    low = p.lower()
    assert "second" in low and "millisecond" in low, "prompt must pin dt UNITS (seconds, not ms)"


def test_phaser_codegen_prompt_demands_generous_entity_sizing():
    # Generated games rendered entities as tiny specks on a big canvas (a 256px sprite
    # shrunk to ~40px), wasting the screen. The prompt must demand generous,
    # screen-filling entity sizes so the game is readable.
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    p = CodeAgent(event_bus=EventBus())._agentic_prompt(
        "a space shooter", "phaser", {"files": []}, "K ")
    low = p.lower()
    assert "setdisplaysize" in low or "size" in low
    assert "tiny" in low or "fill the screen" in low or "generous" in low


def test_phaser_scaffold_wires_the_action_control(tmp_path):
    # The {left,right,up,down,action,pause} input contract includes `action`; the
    # scaffold floor must actually SEND it (Space/Click), not hardcode `action:false`
    # — otherwise one-button games (jump/shoot/launch) ship uncontrollable even
    # though their sim reads input.action (found by adversarial review of #8).
    main = scaffold_for("phaser", "flap", "a one-button flappy jumper")["src/main.js"]
    assert "action: false" not in main, "action must be bound to a real control, not hardcoded false"
    assert "addKey('SPACE')" in main or "SPACE" in main, "scaffold must bind a Space key for action"
    assert "this.space.isDown" in main or "pointerDown" in main, "action must read the bound key/pointer"


def test_scaffold_phaser_has_no_react_confusion():
    # A Phaser game must NOT accidentally produce the React Vite scaffold (the
    # silent fallback this whole stack guards against).
    files = scaffold_for("phaser", "demo", "an arcade game")
    assert "src/main.jsx" not in files
    assert "src/App.jsx" not in files
    pkg = json.loads(files["package.json"])
    assert "react" not in pkg.get("dependencies", {})
    assert "react-dom" not in pkg.get("dependencies", {})


def test_phaser_contract_rejects_react_site_shell():
    # The agent can produce a large React marketing/site shell while the selected
    # stack is still "phaser". Size alone must not make that deliverable: the
    # contract has to require a real Phaser game entrypoint.
    from skyn3t.agents.code_agent import CodeAgent

    files = {
        "package.json": json.dumps({
            "dependencies": {
                "react": "^19.0.0",
                "react-dom": "^19.0.0",
                "react-router-dom": "^7.0.0",
                "phaser": "^3.90.0",
            },
            "devDependencies": {"vite": "^7.0.0"},
        }),
        "index.html": '<div id="root"></div><script type="module" src="/src/main.jsx"></script>',
        "src/main.js": "export const stack = 'phaser';\n",
        "src/main.jsx": "import React from 'react';\nimport { createRoot } from 'react-dom/client';\n",
        "src/App.jsx": "import { Routes, Route } from 'react-router-dom';\nexport default function App(){return <Routes />}\n",
        "src/pages/Play.jsx": "export default function Play(){return <main>Coming soon</main>}\n",
        "src/sim.js": "export function createState(){return {}}\nexport function step(s){return s}\nexport function isWin(){return false}\nexport function isLose(){return false}\n",
    }

    gap = CodeAgent._agentic_contract_gap("phaser", files)
    assert gap
    assert "Phaser game" in gap


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
