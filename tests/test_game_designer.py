"""Game-designer (roadmap #7) — the "deep" rung.

Generated games are only as deep as the cheap model improvises unless something
GUARANTEES depth. design_game(brief) produces a genre-aware Game Design Document
(core loop, win/lose, progression/levels, distinct mechanics, POWERUPS, enemy/
obstacle variety, economy) that the codegen directive then demands — so every game
ships with the depth elements, not a thin one-mechanic toy.

Deterministic floor: never raises, reuses the art-director's genre detection so the
two stay consistent. (The optional LLM tailoring lives in test_game_designer_llm.)
"""

from __future__ import annotations

import json

from skyn3t.agents.game_designer import GameDesign, design_game


def test_design_game_guarantees_the_depth_elements():
    for brief in ("a space shooter", "a brick breaker", "a tower defense game",
                  "a platformer", "a snake game"):
        gd = design_game(brief)
        assert gd.core_loop, brief
        assert gd.win and gd.lose, brief
        assert gd.progression, brief
        assert gd.economy, brief
        assert len(gd.powerups) >= 2, f"{brief}: needs >=2 powerups (the 'powerups etc')"
        assert len(gd.mechanics) >= 2, brief
        assert len(gd.variety) >= 2, brief


def test_design_game_is_genre_aware():
    td = design_game("a tower defense game")
    shooter = design_game("a space shooter")
    assert td.genre == "tower_defense"
    assert shooter.genre == "space_shooter"
    # genre-appropriate powerups, not one generic set shared across genres
    assert td.powerups != shooter.powerups
    assert any(("tower" in p.lower() or "upgrade" in p.lower()) for p in td.powerups)


def test_unknown_game_is_general_but_still_guarantees_depth():
    gd = design_game("a fishing game")
    assert gd.open_ended is True
    assert len(gd.powerups) >= 2
    assert gd.win and gd.lose and gd.progression


def test_design_game_is_deterministic():
    assert design_game("a brick breaker") == design_game("a brick breaker")


def test_design_game_never_raises_on_odd_input():
    for brief in ("", "   ", None, 5, ["x"], {"a": 1}, b"hi", "a" * 5000):  # type: ignore[list-item]
        gd = design_game(brief)  # type: ignore[arg-type]
        assert gd.powerups and gd.win and gd.lose


def test_gamedesign_dict_roundtrips_and_is_json_safe():
    gd = design_game("a space shooter")
    d = gd.to_dict()
    json.dumps(d)  # must ride in the codegen payload extra
    assert GameDesign.from_dict(d) == gd
