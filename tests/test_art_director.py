"""Art-director (roadmap #6, the "fits" layer).

The art-director is the genre-aware planner above the role->sprite resolver. It
decides, deterministically from the brief, (1) the GAME-AWARE roles a genre needs
(paddle/ball/brick for breakout, tower/enemy/gold for tower-defense — not a fixed
generic set) and (2) PER ROLE whether it renders as a generated SPRITE or a clean
styled PRIMITIVE. Geometric/arcade games (pong/breakout/snake/tetris) look BETTER
as crisp primitives and cost $0; sprites are for characters/creatures/objects.

Pure and never raises — the same ArtPlan drives both the sprite generator and the
codegen directive, so they agree on role keys with no fragile threading.
"""

from __future__ import annotations

import re

from skyn3t.agents.art_director import ArtPlan, RoleArt, direct_art

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


# ---- genre detection: game-aware roles, not a fixed generic set ----
def test_breakout_roles_are_game_aware():
    plan = direct_art("a brick breaker game")
    assert plan.genre == "breakout"
    assert set(plan.roles) >= {"paddle", "ball", "brick"}
    # NOT the generic resolver set
    assert "coin" not in plan.roles


def test_tower_defense_roles_are_game_aware():
    plan = direct_art("a tower defense game with waves of enemies")
    assert plan.genre == "tower_defense"
    assert {"tower", "enemy"} <= set(plan.roles)
    assert "gold" in plan.roles or "coin" in plan.roles


def test_space_shooter_is_a_sprite_genre():
    plan = direct_art("a space shooter with alien invaders")
    assert plan.genre == "space_shooter"
    assert "ship" in plan.roles


# ---- the $0 win: geometric games render as ALL primitives, no sprites ----
def test_geometric_game_is_all_primitive_zero_sprites():
    for brief in ("a pong clone", "a brick breaker", "a snake game", "a tetris game"):
        plan = direct_art(brief)
        assert plan.roles, brief
        assert all(r.render == "primitive" for r in plan.roles.values()), brief
        assert plan.sprite_roles() == {}, f"{brief}: geometric games spend $0 on sprites"


def test_sprite_genre_mixes_sprite_and_primitive_roles():
    plan = direct_art("a space shooter with aliens")
    renders = {r.role: r.render for r in plan.roles.values()}
    # characters/objects are sprites; bolts/beams are crisp primitives
    assert renders["ship"] == "sprite"
    assert any(v == "primitive" for v in renders.values()), "laser-likes stay primitive"


# ---- palette: one shared, valid-hex palette across the game ----
def test_palette_is_shared_and_valid_hex():
    plan = direct_art("a space shooter")
    assert len(plan.palette) >= 3
    assert all(_HEX.match(c) for c in plan.palette), plan.palette


def test_primitive_roles_carry_a_palette_color():
    plan = direct_art("a brick breaker")
    for r in plan.roles.values():
        if r.render == "primitive":
            assert _HEX.match(r.color), r
            assert r.color in plan.palette


# ---- sprite roles carry a real game-sprite generation prompt ----
def test_sprite_roles_have_a_game_sprite_prompt():
    plan = direct_art("a fantasy platformer")
    sprites = plan.sprite_roles()
    assert sprites, "a platformer has sprite roles"
    for r in sprites.values():
        low = r.prompt.lower()
        assert "sprite" in low
        assert "transparent" in low
        assert r.subject.lower() in low


def test_primitive_roles_have_no_generation_prompt():
    plan = direct_art("a pong clone")
    assert all(r.prompt == "" for r in plan.roles.values())


# ---- determinism + robustness (the alignment guarantee) ----
def test_direct_art_is_deterministic():
    a = direct_art("a tower defense game")
    b = direct_art("a tower defense game")
    assert a == b


def test_direct_art_never_raises_on_odd_input():
    for brief in ["", "   ", "\U0001f3ae", "a" * 5000, "123 456", None]:  # type: ignore[list-item]
        plan = direct_art(brief)  # type: ignore[arg-type]
        assert isinstance(plan, ArtPlan)
        assert plan.roles  # always a complete, usable plan


def test_direct_art_never_raises_on_non_string_brief():
    # The "never raises" contract must also hold for non-string briefs (the floor
    # is computed before any caller's guard) — coerce at the entry, not by luck.
    for brief in (5, 3.14, ["a"], {"x": 1}, b"hi", True):
        plan = direct_art(brief)  # type: ignore[arg-type]
        assert plan.roles


def test_default_genre_is_arcade_with_generic_roles():
    plan = direct_art("a fun little game")
    assert plan.genre == "arcade"
    assert {"player", "enemy", "coin"} <= set(plan.roles)


def test_arcade_player_is_sprite_projectile_is_primitive():
    plan = direct_art("a fun little game")
    renders = {r.role: r.render for r in plan.roles.values()}
    assert renders["player"] == "sprite"
    assert renders.get("projectile") == "primitive"  # cheap + crisp


# ---- word-bounded genre detection (the phaser-stack lesson) ----
def test_genre_detection_is_word_bounded():
    # "study" contains the substring "td" — must NOT be read as tower_defense.
    plan = direct_art("a study habits tracker game")
    assert plan.genre != "tower_defense"
    # "sneak" contains "snake"? no — but a brief with no genre word is arcade.
    assert direct_art("a calm relaxing game").genre == "arcade"


# ---- genre detection hardening (adversarial review of the "fits" layer) ----
def test_hyphenated_genre_names_are_detected():
    # The tokenizer must not swallow a hyphen joining two genre words.
    assert direct_art("a brick-breaker game").genre == "breakout"
    assert direct_art("a tower-defense game").genre == "tower_defense"
    assert direct_art("a top-down dungeon crawler").genre == "top_down"


def test_generic_shooter_is_not_space_shooter():
    # A non-space shooter must NOT inherit space-ship art — "shooter" alone is
    # not a space signal.
    assert direct_art("a zombie shooter").genre != "space_shooter"


def test_genre_noun_outranks_theme_word():
    # A platformer set in space is a PLATFORMER, not a space shooter — the
    # gameplay noun beats the mere theme word.
    assert direct_art("a space platformer").genre == "platformer"


def test_brick_as_theme_does_not_force_breakout():
    # Bricks as building material, not the breakout paddle/ball game.
    assert direct_art("a game about building brick houses").genre != "breakout"


def test_adventure_alone_is_not_top_down():
    # Bare "adventure" is too broad to route to dungeon/monster art.
    assert direct_art("a fun adventure game").genre != "top_down"


def test_defend_family_triggers_tower_defense():
    assert direct_art("a tower game where you defend against waves").genre == "tower_defense"


def test_td_token_needs_tower_and_is_not_touchdown():
    # "touchdown" tokenizes whole — it is not the standalone token "td".
    assert direct_art("a touchdown football game").genre != "tower_defense"


def test_sprite_roles_helper_filters_render_mode():
    plan = direct_art("a space shooter")
    assert all(r.render == "sprite" for r in plan.sprite_roles().values())
    assert all(r.render == "primitive" for r in plan.primitive_roles().values())
    assert len(plan.sprite_roles()) + len(plan.primitive_roles()) == len(plan.roles)


def test_roleart_fields_are_complete():
    plan = direct_art("a platformer")
    for key, r in plan.roles.items():
        assert isinstance(r, RoleArt)
        assert r.role == key
        assert r.render in ("sprite", "primitive")
        assert r.subject
        assert r.variant
        assert _HEX.match(r.color)


# ---- generalization: the built-in genres are a fast-path, NOT the catalog ----
# Any game the keyword table doesn't recognize must still get a sensible, themed,
# game-aware plan (an OPEN-ENDED one) — never silently forced into a rigid generic
# set. The known genres are an optimization on top of this floor.
def test_unknown_game_gets_an_open_ended_plan():
    for brief in ("a fishing game", "a farming sim", "a cooking game", "a typing game"):
        plan = direct_art(brief)
        assert plan.open_ended is True, brief
        assert plan.roles, brief  # still a usable plan
        # the plan still carries a real palette + at least one generatable sprite
        assert plan.palette
        assert plan.sprite_roles(), brief


def test_recognized_genres_are_not_open_ended():
    # A genre the table DOES know is treated specifically, not as the open floor.
    for brief in ("a brick breaker", "a space shooter", "a tower defense game"):
        assert direct_art(brief).open_ended is False, brief


def test_open_ended_plan_is_themed_by_brief():
    # A space-flavored unknown game still gets the space palette, not a flat default.
    space = direct_art("a space fishing game")
    fantasy = direct_art("a medieval blacksmith game")
    assert space.open_ended and fantasy.open_ended
    assert space.palette != fantasy.palette
