"""Codegen must be TOLD to add game FEEL / juice (retry-games 2026-06-29 finding).

deepseek built a structurally complete 1942 that FUNCTIONED but felt dead — no
hit-flash, no screen shake, no hitstop, no particle feedback. The user's verdict:
"maybe OK ... doesn't really feel good." The fix mirrors the art/depth directives:
inject a concrete per-event juice directive into game codegen (effects in the
Scene/renderer only — the pure src/sim.js stays deterministic). Backed by the
data/skills/game-feel-juice.md skill (retrieved into the prompt).
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus


def _agent():
    return CodeAgent(event_bus=EventBus())


def _plan():
    return {"summary": "a game", "files": []}


def test_phaser_prompt_mandates_per_event_juice():
    p = _agent()._agentic_prompt("a 1942 plane shooter", "phaser", _plan(), "")
    low = p.lower()
    # the four highest feel-per-effort techniques must be named concretely
    assert "settintfill" in low, "hit-flash"
    assert "camera.shake" in low or "shake(" in low, "screen shake"
    assert "hitstop" in low or "timescale" in low, "hitstop / freeze-frame"
    assert "particle" in low or ".explode(" in low, "impact/death particles"
    # event-driven, not vague
    assert "on-kill" in low or "on kill" in low or "kill" in low


def test_juice_stays_out_of_the_pure_sim():
    p = _agent()._agentic_prompt("a shmup", "phaser", _plan(), "")
    low = p.lower()
    assert "src/sim.js" in low
    # the directive must forbid putting effects in the pure sim (renderer/scene only)
    assert "renderer" in low or "scene" in low
    assert "never" in low or "not" in low


def test_non_game_prompt_omits_juice_directive():
    p = _agent()._agentic_prompt("a marketing site", "nextjs", _plan(), "")
    low = p.lower()
    assert "settintfill" not in low
    assert "camera.shake" not in low
