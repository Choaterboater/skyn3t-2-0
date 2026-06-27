"""Codegen must be TOLD to render role sprites (roadmap #6, Phase 1.1).

Verify-by-running a real build exposed that the agentic codegen rewrites
src/main.js from scratch and DROPS the scaffold's sprite preload — so the art
tier was invisible on real builds. The fix: inject a game-art directive into the
codegen prompt for game stacks, instructing it to preload role sprites and render
them with a colored-primitive fallback. The pure src/sim.js stays logic-only.
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus


def _agent():
    return CodeAgent(event_bus=EventBus())


def _plan():
    return {"summary": "a game", "files": []}


def test_phaser_codegen_prompt_includes_sprite_directive():
    prompt = _agent()._agentic_prompt("a space shooter", "phaser", _plan(), "")
    low = prompt.lower()
    assert "/assets/sprites/" in prompt, "codegen must be told where role sprites live"
    assert "textures.exists" in prompt, "codegen must use the primitive fallback"
    assert "preload" in low


def test_non_game_codegen_prompt_omits_sprite_directive():
    prompt = _agent()._agentic_prompt("a marketing site", "nextjs", _plan(), "")
    assert "/assets/sprites/" not in prompt
