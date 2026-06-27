"""Art-director (roadmap #6) — the genre-aware planner that makes a game's art FIT.

The role->sprite resolver ([[asset_resolver]]) maps a fixed GENERIC set of roles
(player/enemy/coin/...) to themed sprites. That is the floor, but it has two gaps
the art-director closes:

  1. **Game-aware roles.** A breakout game needs paddle/ball/brick, a tower-defense
     game needs tower/enemy/gold — not a generic player/coin. The art-director picks
     the roles a GENRE actually uses, so generated assets fit what's on screen.

  2. **Sprite vs primitive, per role.** Not everything should be a generated sprite.
     Geometric/arcade games (pong, breakout, snake, tetris) look BETTER as clean
     styled PRIMITIVES (neon + glow + a shared palette) and cost $0 — a fuzzy AI
     sprite of a "ball" is worse than a crisp circle. Sprites are for characters,
     creatures, and objects (a knight, an alien, a coin, a tower). Each role is
     tagged ``render`` = ``"sprite"`` or ``"primitive"``; only sprite roles cost a
     generation, so a whole geometric game is free.

``direct_art`` is **pure and deterministic** on the brief. That is load-bearing:
the sprite generator (``assets.generate_role_sprites``) and the codegen directive
(``code_agent._game_art_directive``) BOTH call it independently and must agree on
the role keys with no fragile threading — only a deterministic plan guarantees that
``tower.png`` gets generated exactly when codegen is told to load ``tower``.

An LLM refinement layer (richer per-role art direction) is a separate, flag-gated
step (``art_director_enabled``, default off) that the runner threads to both
consumers — it must NOT live inside ``direct_art``, or the two callers would get
divergent non-deterministic plans. This module is the always-on, $0 floor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RoleArt:
    """Art direction for one game role. ``render`` decides sprite-vs-primitive;
    ``prompt`` is the generation prompt (sprite roles only, else ``""``); ``color``
    is the palette hex used for a primitive role AND as the colored-primitive
    fallback when a sprite is missing; ``variant`` is the filesystem-safe key."""

    role: str
    render: str  # "sprite" | "primitive"
    subject: str
    prompt: str
    variant: str
    color: str


@dataclass
class ArtPlan:
    """A game's complete, deterministic art plan: the roles it uses, each tagged
    sprite/primitive, over one shared palette.

    ``open_ended`` marks a plan for a game the known-genre table did NOT recognize.
    Its roles are a sensible THEMED BASELINE, not a tailored genre set — so the
    codegen directive tells the model to render whatever entities THIS brief
    implies (following the sprite-vs-primitive rule + palette), rather than a fixed
    role list. The built-in genres are a fast-path optimization on top of this
    open-ended floor; they are examples, not the catalog of games."""

    genre: str
    theme: str
    palette: tuple[str, ...]
    roles: dict[str, RoleArt] = field(default_factory=dict)
    open_ended: bool = False

    def sprite_roles(self) -> dict[str, RoleArt]:
        """Roles to GENERATE — the only ones that cost money."""
        return {k: r for k, r in self.roles.items() if r.render == "sprite"}

    def primitive_roles(self) -> dict[str, RoleArt]:
        """Roles drawn as clean styled shapes from the palette ($0)."""
        return {k: r for k, r in self.roles.items() if r.render == "primitive"}


# Shared palettes (background first, then accents). Hex is validated by tests. The
# "arcade" background + green + gold deliberately match the _phaser scaffold's
# existing colors so the floor and the planned art read as one piece.
_PALETTES: dict[str, tuple[str, ...]] = {
    "neon": ("#0a0a14", "#39ff14", "#00eaff", "#ff2bd6", "#ffe600"),
    "space": ("#070b1a", "#7df9ff", "#ff5d73", "#ffd166", "#a78bfa"),
    "fantasy": ("#241a33", "#ffd700", "#7bb661", "#c98a3a", "#e0d6f5"),
    "arcade": ("#1d2330", "#4ade80", "#fbbf24", "#60a5fa", "#f472b6"),
    "earth": ("#1b2a1f", "#9acd32", "#c2a878", "#e6b422", "#7ec8e3"),
}


@dataclass(frozen=True, slots=True)
class _GenreSpec:
    palette: str
    # (role, render, subject), in render/draw order.
    roles: tuple[tuple[str, str, str], ...]


# Geometric genres: ALL primitive, $0 — crisp shapes beat fuzzy sprites here.
# Sprite genres: characters/objects are sprites; bolts/beams/paths/platforms and
# the backdrop stay primitives (cheap + crisp). Backgrounds are always a palette
# fill (primitive) — a flat themed backdrop reads better than a generated one and
# costs nothing.
_GENRES: dict[str, _GenreSpec] = {
    "pong": _GenreSpec("neon", (
        ("paddle", "primitive", "paddle bar"),
        ("ball", "primitive", "ball"),
    )),
    "breakout": _GenreSpec("neon", (
        ("paddle", "primitive", "paddle bar"),
        ("ball", "primitive", "ball"),
        ("brick", "primitive", "brick"),
    )),
    "snake": _GenreSpec("neon", (
        ("snake", "primitive", "snake segment"),
        ("food", "primitive", "food pellet"),
    )),
    "tetris": _GenreSpec("neon", (
        ("block", "primitive", "tetromino block"),
    )),
    "space_shooter": _GenreSpec("space", (
        ("ship", "sprite", "sleek space fighter ship"),
        ("alien", "sprite", "menacing alien creature"),
        ("laser", "primitive", "laser bolt"),
        ("powerup", "sprite", "glowing power-up capsule"),
        ("background", "primitive", "deep-space starfield"),
    )),
    "platformer": _GenreSpec("arcade", (
        ("player", "sprite", "cheerful platformer hero"),
        ("enemy", "sprite", "grumpy slime monster"),
        ("coin", "sprite", "shiny gold coin"),
        ("platform", "primitive", "solid ground block"),
        ("background", "primitive", "bright sky with rolling hills"),
    )),
    "tower_defense": _GenreSpec("earth", (
        ("tower", "sprite", "stone defense turret"),
        ("enemy", "sprite", "marching creep monster"),
        ("projectile", "primitive", "cannon shot"),
        ("gold", "sprite", "stack of gold coins"),
        ("path", "primitive", "dirt path tile"),
        ("background", "primitive", "grassy battlefield map"),
    )),
    "top_down": _GenreSpec("arcade", (
        ("player", "sprite", "top-down adventurer hero"),
        ("enemy", "sprite", "prowling monster"),
        ("item", "sprite", "treasure item"),
        ("background", "primitive", "top-down dungeon floor"),
    )),
}

# Genre detection. Two hard lessons are baked in:
#   * Tokenize on word boundaries AND split hyphens, so "brick-breaker" and
#     "tower-defense" read as their component words (not one opaque token that
#     matches nothing) and "study"/"touchdown" never yield the token "td".
#   * A token can be a whole word yet still too BROAD: "shooter" is not a SPACE
#     signal, bare "brick" is a building material, bare "adventure" is anything. So
#     ambiguous tokens require a corroborating word, and an explicit gameplay NOUN
#     ("platformer", "snake") outranks a mere theme word ("space").

# Unambiguous gameplay nouns — each names a genre directly and wins over themes.
# Ordered; first match wins.
_GENRE_NOUNS: tuple[tuple[str, frozenset[str]], ...] = (
    ("pong", frozenset({"pong"})),
    ("snake", frozenset({"snake"})),
    ("tetris", frozenset({"tetris", "tetromino", "tetrominoes"})),
    ("breakout", frozenset({"breakout", "arkanoid"})),
    ("platformer", frozenset({"platformer", "sidescroller"})),
    ("top_down", frozenset({"rpg", "zelda", "roguelike", "topdown", "dungeon"})),
)
# Specific space NOUNS (each alone implies a space game); plain "space" is only a
# theme and needs a shooting word to become space_shooter.
_SPACE_NOUNS = frozenset({
    "spaceship", "spaceships", "galaxy", "galactic", "alien", "aliens", "asteroid",
    "asteroids", "starship", "spacecraft", "ufo", "shmup", "invader", "invaders",
    "cosmic", "interstellar", "nebula", "scifi",
})
_SHOOT = frozenset({"shooter", "shoot", "shooting", "blaster", "blast", "laser", "lasers"})
_JUMP = frozenset({"jump", "jumping", "jumper"})
_DEFEND = frozenset({"defense", "defence", "defend", "defending", "defender"})


def _words(brief: str | None) -> set[str]:
    # Split on every non-alphanumeric char (hyphens included) so multiword genre
    # phrases survive as their component words, and short tokens stay standalone.
    return set(re.findall(r"[a-z0-9]+", (brief or "").lower()))


def _detect_genre(brief: str | None) -> str:
    w = _words(brief)
    # Breakout: "brick(s)" alone is just a material, so require a paddle/ball/breaker
    # corroborator before claiming the paddle-and-ball game.
    if {"brick", "bricks"} & w and ({"breaker", "ball", "paddle"} & w):
        return "breakout"
    # Explicit gameplay nouns outrank theme words ("space platformer" -> platformer).
    for genre, nouns in _GENRE_NOUNS:
        if w & nouns:
            return genre
    # Multiword genre phrases the tokenizer split on a hyphen/space.
    if "top" in w and "down" in w:
        return "top_down"
    if "side" in w and "scroller" in w:
        return "platformer"
    # Tower defense: a real tower + a defend-family word (or the explicit "td" token
    # beside "tower"); checked before space so a space-themed TD stays a TD.
    if "tower" in w and ((w & _DEFEND) or "td" in w):
        return "tower_defense"
    # Platformer via "platform" + a jump word (plain "platform" is too generic).
    if "platform" in w and (w & _JUMP):
        return "platformer"
    # Space shooter: a specific space noun, or the theme "space" + a shooting word.
    if (w & _SPACE_NOUNS) or ("space" in w and (w & _SHOOT)):
        return "space_shooter"
    return "arcade"


def _sprite_prompt(role: str, subject: str) -> str:
    if role == "background":
        return (
            f"2D game background art of {subject}, wide seamless scene, "
            "flat vector illustration, soft depth, no text, no characters"
        )
    return (
        f"2D game sprite of a {subject}, centered, transparent background, "
        "clean flat shading, crisp edges, vibrant, no text"
    )


def _colors_for(order: list[tuple[str, str]], palette: tuple[str, ...]) -> dict[str, str]:
    """Assign each role a palette color: the backdrop takes the background hex,
    every other role cycles the accents — deterministic and palette-locked."""
    accents = palette[1:] or palette
    out: dict[str, str] = {}
    ai = 0
    for role, _render in order:
        if role == "background":
            out[role] = palette[0]
        else:
            out[role] = accents[ai % len(accents)]
            ai += 1
    return out


def _general_plan(brief: str | None) -> ArtPlan:
    """The OPEN-ENDED fallback for any game the genre table doesn't recognize: a
    sensible THEMED BASELINE (characters/coin = sprite; platform/projectile/
    background = a palette primitive) over the brief's detected-theme palette. It is
    flagged ``open_ended`` so the codegen directive invites game-appropriate roles
    rather than pinning this generic set — the known genres are a fast-path, not the
    whole catalog of games."""
    from skyn3t.studio.asset_resolver import _detect_theme, plan_roles

    theme = _detect_theme(brief or "")
    palette = _PALETTES.get(theme, _PALETTES["arcade"])
    plan = plan_roles(brief or "")
    render = {
        "player": "sprite", "enemy": "sprite", "coin": "sprite",
        "platform": "primitive", "projectile": "primitive", "background": "primitive",
    }
    order = [(role, render.get(role, "sprite")) for role in plan]
    colors = _colors_for(order, palette)
    roles: dict[str, RoleArt] = {}
    for role, rp in plan.items():
        rmode = render.get(role, "sprite")
        roles[role] = RoleArt(
            role=role,
            render=rmode,
            subject=rp.subject,
            prompt=rp.prompt if rmode == "sprite" else "",
            variant=f"{role}_arcade",
            color=colors[role],
        )
    return ArtPlan(
        genre="arcade", theme=theme, palette=palette, roles=roles, open_ended=True
    )


def direct_art(brief: str | None, *, settings=None) -> ArtPlan:
    """Plan a game's art from its brief — game-aware roles, each tagged sprite or
    primitive, over one shared palette. A recognized genre gets a tailored role set;
    ANY other game gets an open-ended themed baseline (``_general_plan``) so the
    built-in genres stay a fast-path, never a closed catalog. Deterministic on the
    brief ALONE (no seed): the sprite generator and the codegen directive both call
    this independently and must agree on role keys, so divergent inputs would break
    that alignment. Never raises; ``settings`` is accepted for forward-compatible LLM
    refinement and is unused by this deterministic floor."""
    genre = _detect_genre(brief)
    if genre == "arcade":
        return _general_plan(brief)

    spec = _GENRES[genre]
    palette = _PALETTES.get(spec.palette, _PALETTES["arcade"])
    order = [(role, render) for role, render, _subject in spec.roles]
    colors = _colors_for(order, palette)
    roles: dict[str, RoleArt] = {}
    for role, render, subject in spec.roles:
        roles[role] = RoleArt(
            role=role,
            render=render,
            subject=subject,
            prompt=_sprite_prompt(role, subject) if render == "sprite" else "",
            variant=f"{role}_{genre}",
            color=colors[role],
        )
    return ArtPlan(genre=genre, theme=spec.palette, palette=palette, roles=roles)
