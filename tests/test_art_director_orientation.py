"""Genre-aware sprite ORIENTATION (retry-games 2026-06-29 finding).

A top-down vertical shooter needs orientation-correct sprites: the player faces
UP, enemies face DOWN. The art-director's generic "a fighter plane" prompt yielded
a 3/4-view plane facing diagonally — wrong for a top-down shmup. The art-director
now appends a top-down + nose-direction phrase for shooter genres.
"""

from __future__ import annotations

from skyn3t.agents.art_director import _orientation_for, _sprite_prompt, direct_art


def test_player_role_faces_up_in_a_shooter():
    o = _orientation_for("space_shooter", "ship").lower()
    assert "top-down" in o and "up" in o


def test_enemy_role_faces_down_in_a_shooter():
    o = _orientation_for("space_shooter", "enemy").lower()
    assert "top-down" in o and "down" in o


def test_llm_vertical_shooter_genre_is_recognized():
    # the LLM art-director labels the 1942 brief "vertical_scroll_shooter"
    assert "up" in _orientation_for("vertical_scroll_shooter", "player_plane").lower()
    down = _orientation_for("vertical_scroll_shooter", "enemy_plane").lower()
    assert "down" in down
    assert "down" in _orientation_for("vertical_scroll_shooter", "boss_plane").lower()


def test_enemy_token_wins_over_player_token():
    # 'enemy_plane' contains 'plane' but must read as an ENEMY (faces down)
    assert "down" in _orientation_for("vertical_scroll_shooter", "enemy_plane").lower()


def test_neutral_role_gets_top_down_without_a_nose_direction():
    o = _orientation_for("space_shooter", "powerup").lower()
    assert "top-down" in o
    assert "nose pointing" not in o  # a non-vehicle pickup has no nose direction


def test_non_shooter_genres_get_no_orientation():
    assert _orientation_for("breakout", "paddle") == ""
    assert _orientation_for("snake", "snake") == ""
    assert _orientation_for("platformer", "hero") == ""


def test_side_scrolling_shooter_is_skipped():
    # a horizontal shooter's player faces RIGHT, not up — don't impose up/down
    assert _orientation_for("side_scroller_shooter", "ship") == ""


def test_sprite_prompt_appends_orientation_for_a_shooter():
    p = _sprite_prompt("ship", "space fighter", "space_shooter").lower()
    assert "top-down" in p and "up" in p


def test_sprite_prompt_is_unchanged_for_non_shooters():
    p = _sprite_prompt("coin", "gold coin", "platformer").lower()
    assert "top-down" not in p


def test_sprite_prompt_default_genre_adds_nothing():
    # backward-compatible 2-arg call
    assert "top-down" not in _sprite_prompt("hero", "a knight").lower()


def test_direct_art_space_shooter_sprites_are_oriented_end_to_end():
    plan = direct_art("a space shooter with alien ships")
    assert plan.genre == "space_shooter"
    sr = plan.sprite_roles()
    assert "top-down" in sr["ship"].prompt.lower() and "up" in sr["ship"].prompt.lower()
    assert "down" in sr["enemy"].prompt.lower()
