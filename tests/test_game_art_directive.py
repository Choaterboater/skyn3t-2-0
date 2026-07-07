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


# ---- art-director: the directive is GENRE-AWARE (roadmap #6, "fits") ----
# The directive is built from direct_art(brief), so codegen lists the SAME
# game-aware role keys the sprite generator produces — and geometric games are
# told to render crisp primitives, never to load sprites.
def test_directive_lists_genre_aware_roles_not_generic_set():
    prompt = _agent()._agentic_prompt("a brick breaker game", "phaser", _plan(), "")
    low = prompt.lower()
    assert "paddle" in low and "brick" in low, "must name the genre's actual roles"
    assert "coin" not in low, "must not fall back to the generic role set"


def test_geometric_game_directive_is_primitive_only():
    prompt = _agent()._agentic_prompt("a pong clone", "phaser", _plan(), "")
    low = prompt.lower()
    # a geometric game spends $0: codegen is told to render primitives, not sprites
    assert "primitive" in low
    assert "/assets/sprites/" not in prompt, "geometric games load no sprite files"


def test_sprite_genre_directive_marks_sprites_and_primitives():
    prompt = _agent()._agentic_prompt("a space shooter with aliens", "phaser", _plan(), "")
    low = prompt.lower()
    assert "/assets/sprites/" in prompt, "sprite roles load from the sprites dir"
    assert "textures.exists" in prompt, "with a primitive fallback"
    assert "ship" in low, "the genre's sprite role is named"
    assert "primitive" in low, "primitive roles (laser-likes) are called out"


def test_directive_includes_the_shared_palette():
    prompt = _agent()._agentic_prompt("a space shooter", "phaser", _plan(), "")
    from skyn3t.agents.art_director import direct_art

    palette = direct_art("a space shooter").palette
    # at least the accent colors are handed to codegen so the art is cohesive
    assert sum(c in prompt for c in palette) >= 2, "the palette must reach codegen"


def test_directive_uses_threaded_art_plan_over_recompute():
    # When a pre-computed (LLM-tailored) plan is threaded in, the directive lists
    # ITS roles, not the brief-recomputed generic ones — the alignment guarantee.
    from skyn3t.agents.art_director import ArtPlan, RoleArt

    plan = ArtPlan(
        genre="fishing", theme="llm", palette=("#0a2a3a", "#7ec8e3", "#f4d35e"),
        roles={
            "boat": RoleArt("boat", "sprite", "fishing boat", "p", "boat_llm", "#7ec8e3"),
            "fish": RoleArt("fish", "sprite", "silver fish", "p", "fish_llm", "#f4d35e"),
            "hook": RoleArt("hook", "primitive", "hook", "", "hook_llm", "#7ec8e3"),
        },
    )
    prompt = _agent()._agentic_prompt(
        "a relaxing fishing game", "phaser", _plan(), "", art_plan=plan.to_dict()
    )
    low = prompt.lower()
    assert "boat" in low and "fish" in low, "the tailored roles reach codegen"
    assert "/assets/sprites/" in prompt
    # without threading, the same brief is an open-ended generic plan (no 'boat')
    assert "boat" not in _agent()._agentic_prompt(
        "a relaxing fishing game", "phaser", _plan(), ""
    ).lower()


def test_retry_prompt_preserves_threaded_art_plan():
    # On a retry the directive must still use the THREADED plan — else codegen
    # recomputes a different role set than the sprite generator already produced.
    from skyn3t.agents.art_director import ArtPlan, RoleArt

    plan = ArtPlan(
        genre="fishing", theme="llm", palette=("#0a2a3a", "#7ec8e3", "#f4d35e"),
        roles={
            "boat": RoleArt("boat", "sprite", "fishing boat", "p", "boat_llm", "#7ec8e3"),
            "fish": RoleArt("fish", "sprite", "silver fish", "p", "fish_llm", "#f4d35e"),
        },
    )
    retry = _agent()._agentic_retry_prompt(
        "a fishing game", "phaser", _plan(), "", 50, art_plan=plan.to_dict()
    )
    low = retry.lower()
    assert "boat" in low and "fish" in low, "retry keeps the threaded roles"


def test_unknown_game_directive_is_open_ended():
    # A game the table doesn't recognize must be told to render the entities THIS
    # brief implies (game-appropriate roles + the sprite-vs-primitive rule), not a
    # fixed genre's role list.
    prompt = _agent()._agentic_prompt("a fishing game", "phaser", _plan(), "")
    low = prompt.lower()
    assert "/assets/sprites/" in prompt, "baseline sprites still load"
    assert "textures.exists" in prompt, "with a primitive fallback"
    assert "primitive" in low, "the sprite-vs-primitive rule is stated"
    assert "appropriate" in low, "codegen is told to use roles appropriate to THIS game"


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


# ---- FIX B: determinism must seed from the createState(seed) ARGUMENT only ----
# The tower-defense probe seeded its RNG with Date.now() despite the directive, so
# state.rng held a Unix timestamp (~1.78e12) -> the headless gate flagged value
# explosion + non-determinism. The generic "never Math.random or Date.now" wasn't
# enough; reinforce that ALL randomness derives from the seed ARGUMENT, never the
# clock — not even to pick the initial seed.
def test_phaser_codegen_pins_seed_argument_as_only_randomness_source():
    prompt = _agent()._agentic_prompt("a tower defense game", "phaser", _plan(), "")
    low = prompt.lower()
    assert "date.now" in low and "math.random" in low
    # tie determinism to the seed ARGUMENT, not just a generic clock ban
    assert "seed argument" in low, "must derive randomness from the seed argument"
    assert "initial seed" in low, "must forbid clock-seeding even the initial seed"


# ---- sprite WIRING: generated textures must actually render (verify-by-playing) ----
# Regression: the model loaded enemy.png/bullet.png in preload() but then drew enemies and
# bullets with this.add.graphics() primitives, so the generated sprites NEVER appeared on
# screen (the player saw shapes). The directive must MANDATE rendering each textured role as
# a Phaser sprite (groups for swarms) and FORBID drawing a textured role as a primitive.
def test_sprite_directive_mandates_sprites_and_forbids_primitive_for_textured_roles():
    prompt = _agent()._agentic_prompt("a space shooter with aliens", "phaser", _plan(), "")
    low = prompt.lower()
    assert "as a phaser sprite" in low, "textured roles must render as sprites, not shapes"
    assert "group" in low, "pooled bullets/enemies must use a sprite group keyed to texture"
    assert "this.add.graphics()" in prompt, "the forbidden primitive path is named"
    assert "bug" in low and "do not" in low, "drawing a textured role as a shape is a BUG"
    assert "setdisplaysize" in low, "sprites are scaled to gameplay size"
    # the model must LOAD the real generated PNG art, not fabricate textures in code
    assert "this.load.image" in prompt, "must load the generated /assets/sprites PNGs"
    assert "generatetexture" in low, "fabricating textures in code is named + FORBIDDEN"


def test_sprite_directive_demands_a_layered_background_not_flat_fill():
    # 'better backgrounds': the backdrop must be layered (parallax + gradient), not a fill.
    prompt = _agent()._agentic_prompt("a space shooter with aliens", "phaser", _plan(), "")
    low = prompt.lower()
    assert "parallax" in low or "layered" in low
    assert "not a single flat fill" in low


def test_sprite_directive_demands_distinct_powerup_sprites():
    # 'different types of power ups': the directive must tell codegen each power-up TYPE
    # uses its OWN matching sprite, never one texture reused for all.
    prompt = _agent()._agentic_prompt("a space shooter with aliens", "phaser", _plan(), "")
    low = prompt.lower()
    assert "power-up variety" in low, "the power-up variety clause is present"
    assert "powerup_shield" in low and "powerup_rapid" in low, "distinct power-up roles named"
    assert "never draw every power-up with the same texture" in low


def test_agentic_prompt_mentions_asset_foundry_paths():
    prompt = _agent()._agentic_prompt(
        "a platformer",
        "phaser",
        _plan(),
        "",
        asset_foundry={"selected": {"sprite/player/idle/down": {"path": "player/idle/down.png"}}},
    )
    assert "player/idle/down.png" in prompt


def test_non_game_agentic_prompt_mentions_web_asset_foundry_paths():
    prompt = _agent()._agentic_prompt(
        "a golf course website",
        "nextjs",
        _plan(),
        "",
        asset_foundry={
            "type": "web",
            "selected": {
                "web/hero": {"path": "/assets/hero.png"},
                "web/og": {"path": "/assets/og.png"},
                "web/favicon": {"path": "/assets/favicon.png"},
            },
        },
    )
    assert "/assets/hero.png" in prompt
    assert "Open Graph" in prompt
    assert "favicon" in prompt
