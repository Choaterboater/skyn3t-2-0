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


# ---- stack adherence: codegen must build a GAME, not a website (Phase: reliability) ----
# Verify-by-running exposed the cheap model ignoring a pinned `phaser` stack and
# building a Next.js marketing site. The codegen prompt must hard-enforce the stack.
def test_phaser_codegen_prompt_enforces_vanilla_game_stack():
    prompt = _agent()._agentic_prompt("a space shooter", "phaser", _plan(), "")
    low = prompt.lower()
    assert "src/sim.js" in prompt, "codegen must be told the pure sim entry"
    assert "vanilla" in low, "must demand vanilla JS"
    # the exact frameworks it wrongly built must be explicitly forbidden
    assert "react" in low and "next.js" in low
    assert "do not" in low


def test_phaser_codegen_forbids_website_scaffolding():
    prompt = _agent()._agentic_prompt("a space shooter", "phaser", _plan(), "")
    # the rogue build created next.config/components/pages — name them as forbidden
    assert "next.config" in prompt and "components/" in prompt and "pages/" in prompt


def test_non_game_codegen_prompt_has_no_game_stack_directive():
    prompt = _agent()._agentic_prompt("a marketing site", "nextjs", _plan(), "")
    assert "src/sim.js" not in prompt
    assert "Build a GAME" not in prompt


# ---- input contract: the root cause of the NaN + pause failures (sim-correctness) ----
# The gate feeds step() EXACTLY {left,right,up,down,action,pause}. A real build read an
# invented input.paddleDir (undefined -> NaN) and toggled paused inside step() (un-froze
# itself). The directive must pin the input contract + pause semantics.
def test_phaser_codegen_pins_the_input_contract():
    prompt = _agent()._agentic_prompt("a brick breaker", "phaser", _plan(), "")
    low = prompt.lower()
    for field in ("left", "right", "up", "down", "action", "pause"):
        assert field in low, f"input field {field} must be named"
    # codegen must NOT invent custom input fields (the paddleDir->NaN bug)
    assert "never invent" in low or "do not invent" in low
    assert "paddledir" in low, "name the exact anti-example that caused the NaN"


def test_phaser_codegen_pins_pause_as_host_owned_level_flag():
    prompt = _agent()._agentic_prompt("a brick breaker", "phaser", _plan(), "")
    # step() must early-return on paused/over and must NOT toggle paused itself
    assert "if (state.paused || state.over) return state" in prompt
    low = prompt.lower()
    assert "never write state.paused" in low or "must not toggle" in low
