"""Codegen must be TOLD to build depth (roadmap #7).

The game-designer's GDD only matters if codegen is required to implement it. The
depth directive injects the GDD (win/lose, progression, mechanics, POWER-UPS,
variety, economy) into the codegen prompt for game stacks — so a generated game
ships with the depth elements, not a thin one-mechanic toy. Threaded like the art
plan (and preserved across the retry prompt — the lesson from the art-director).
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus


def _agent():
    return CodeAgent(event_bus=EventBus())


def _plan():
    return {"summary": "a game", "files": []}


def test_phaser_prompt_demands_depth():
    prompt = _agent()._agentic_prompt("a space shooter", "phaser", _plan(), "")
    low = prompt.lower()
    assert "win" in low and "lose" in low, "win/lose conditions demanded"
    assert "power" in low, "power-ups demanded (the 'powerups etc')"
    assert "level" in low or "progress" in low, "progression/levels demanded"
    # genre-appropriate power-ups are named, not a generic instruction
    assert any(p in low for p in ("rapid fire", "spread", "shield", "bomb"))


def test_phaser_prompt_treats_explicit_counts_as_hard_contracts():
    prompt = _agent()._agentic_prompt(
        "Code Islands: a 120-level journey across 12 islands and 4 phases",
        "phaser",
        _plan(),
        "",
    )
    low = prompt.lower()
    assert "explicit brief counts are hard contracts" in low
    assert "120 levels" in low
    assert "src/sim.js" in low
    assert "do not shrink" in low


def test_non_game_prompt_has_no_depth_directive():
    prompt = _agent()._agentic_prompt("a marketing site", "nextjs", _plan(), "")
    assert "GAME DEPTH" not in prompt


def test_depth_directive_is_genre_aware():
    shooter = _agent()._agentic_prompt("a space shooter", "phaser", _plan(), "").lower()
    td = _agent()._agentic_prompt("a tower defense game", "phaser", _plan(), "").lower()
    assert "wave" in td, "tower-defense depth mentions waves"
    assert "tower" in td
    assert "wave" not in shooter or "tower" not in shooter  # different GDDs


def test_depth_directive_uses_threaded_game_design():
    from skyn3t.agents.game_designer import GameDesign

    gd = GameDesign(
        genre="fishing", core_loop="cast and reel", win="fill the daily quota",
        lose="run out of time", progression="deeper, rarer spots",
        mechanics=("casting", "reeling", "timing"),
        powerups=("bigger net", "fish sonar", "faster reel"),
        variety=("small fish", "big fish", "rare fish"), economy="cash per fish",
        open_ended=True,
    )
    prompt = _agent()._agentic_prompt(
        "a fishing game", "phaser", _plan(), "", game_design=gd.to_dict()
    )
    low = prompt.lower()
    assert "fish sonar" in low or "bigger net" in low, "threaded power-ups reach codegen"
    assert "quota" in low, "threaded win condition reaches codegen"


def test_retry_prompt_preserves_threaded_game_design():
    from skyn3t.agents.game_designer import GameDesign

    gd = GameDesign(
        genre="fishing", core_loop="x", win="fill the daily quota", lose="y",
        progression="z", mechanics=("a", "b"), powerups=("fish sonar", "bigger net"),
        variety=("c", "d"), economy="e", open_ended=True,
    )
    retry = _agent()._agentic_retry_prompt(
        "a fishing game", "phaser", _plan(), "", 50, game_design=gd.to_dict()
    )
    assert "fish sonar" in retry.lower(), "retry keeps the threaded depth spec"
