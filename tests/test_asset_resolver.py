"""Role->sprite resolver (roadmap #6, Phase 1).

The resolver is the source-agnostic heart of the art tier: it decides which roles
a game needs and a brief-themed plan for each, deterministically. The same plan
drives BOTH sources — it becomes the image-generation prompt (Replicate) and the
bundled-sprite variant key (offline). Pure, never raises.
"""

from __future__ import annotations

from skyn3t.studio.asset_resolver import ROLES, RolePlan, plan_roles


def test_plan_roles_covers_all_core_roles():
    plan = plan_roles("a platformer game", seed=1)
    assert set(plan) >= set(ROLES)
    for role, rp in plan.items():
        assert isinstance(rp, RolePlan)
        assert rp.role == role
        assert rp.subject, f"{role} has no subject"
        assert rp.prompt, f"{role} has no prompt"
        assert rp.variant, f"{role} has no variant key"


def test_plan_roles_is_deterministic():
    a = plan_roles("a space shooter", seed=7)
    b = plan_roles("a space shooter", seed=7)
    assert a == b


def test_plan_roles_themes_by_brief_keywords():
    space = plan_roles("a space shooter with aliens", seed=0)
    fantasy = plan_roles("a medieval fantasy dungeon crawler", seed=0)
    # The enemy subject reflects the brief's world, not a fixed default.
    assert "alien" in space["enemy"].subject.lower()
    assert space["enemy"].subject != fantasy["enemy"].subject
    # The player is themed too (knight vs ship, etc.).
    assert space["player"].subject != fantasy["player"].subject


def test_prompt_is_a_game_sprite_prompt():
    rp = plan_roles("a space shooter", seed=0)["player"]
    low = rp.prompt.lower()
    # A game-sprite prompt, not a generic photo: mentions sprite + transparency.
    assert "sprite" in low
    assert "transparent" in low or "transparent background" in low
    assert rp.subject.lower() in low


def test_variant_key_is_filesystem_safe_and_role_scoped():
    for role, rp in plan_roles("a game", seed=3).items():
        assert rp.variant.replace("_", "").replace("-", "").isalnum()
        assert rp.variant.startswith(role)


def test_plan_roles_never_raises_on_odd_input():
    for brief in ["", "   ", "\U0001f3ae", "a" * 5000, "123 456"]:
        plan = plan_roles(brief, seed=0)
        assert set(plan) >= set(ROLES)


def test_default_theme_when_no_keywords():
    # A brief with no world keywords still yields a complete, sensible plan.
    plan = plan_roles("a fun arcade game", seed=0)
    assert plan["coin"].subject  # e.g. "coin"
    assert plan["player"].subject
