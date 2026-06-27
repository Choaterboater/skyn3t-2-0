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

import structlog

log = structlog.get_logger(__name__)


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

    def to_dict(self) -> dict:
        """JSON-safe form so the plan can ride in the codegen payload's ``extra``
        and be rebuilt by the directive (a non-deterministic LLM plan must be
        threaded, not recomputed)."""
        return {
            "genre": self.genre,
            "theme": self.theme,
            "palette": list(self.palette),
            "open_ended": self.open_ended,
            "roles": {
                k: {
                    "role": r.role, "render": r.render, "subject": r.subject,
                    "prompt": r.prompt, "variant": r.variant, "color": r.color,
                }
                for k, r in self.roles.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> ArtPlan:
        roles = {
            k: RoleArt(
                role=v["role"], render=v["render"], subject=v["subject"],
                prompt=v.get("prompt", ""), variant=v["variant"], color=v["color"],
            )
            for k, v in (d.get("roles") or {}).items()
        }
        return cls(
            genre=d.get("genre", "custom"),
            theme=d.get("theme", ""),
            palette=tuple(d.get("palette") or ()),
            roles=roles,
            open_ended=bool(d.get("open_ended", False)),
        )


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


def _words(brief) -> set[str]:
    # Split on every non-alphanumeric char (hyphens included) so multiword genre
    # phrases survive as their component words, and short tokens stay standalone.
    # ``str(brief or "")`` tolerates any type (the never-raises contract).
    return set(re.findall(r"[a-z0-9]+", str(brief or "").lower()))


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
    # Coerce once at the contract surface so EVERY downstream helper (incl. the
    # asset_resolver theme/role functions) sees a str — the never-raises guarantee
    # must not depend on callers passing the right type.
    if not isinstance(brief, str):
        brief = "" if brief is None else str(brief)
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


# ---- LLM art-director (the long tail) -------------------------------------
# An OPTIONAL cheap LLM call that tailors roles + palette to ANY brief — the genre
# table and the open-ended floor handle the rest. Gated by ``art_director_enabled``
# (default off); NEVER raises; ALWAYS falls back to the deterministic ``direct_art``
# floor, so the default behavior is unchanged and $0. Because its output is
# non-deterministic, the RUNNER computes the plan ONCE and threads it to both
# consumers (sprite generator + codegen directive) — it must not be recomputed.
_ART_SYSTEM = (
    "You are a 2D game art director. Given a game brief, decide the small set of "
    "on-screen ROLES the game needs and, per role, whether it is a generated pixel "
    "SPRITE (characters, creatures, vehicles, items, themed objects) or a clean "
    "styled PRIMITIVE (geometric shapes, projectiles, platforms, walls, HUD, "
    "abstract). Pick ONE cohesive color palette. Respond with JSON only."
)
_ART_PROMPT = (
    "Game brief: {brief}\n\n"
    'Return JSON exactly like: {{"genre": "<short genre>", '
    '"palette": ["#RRGGBB", "#RRGGBB", "#RRGGBB", "#RRGGBB"], '
    '"roles": [{{"role": "<short_snake_key>", "render": "sprite|primitive", '
    '"subject": "<concrete visual noun>"}}]}}. '
    "Use 3-7 game-aware roles that FIT this specific game (a fishing game -> boat, "
    "fish, hook, water; a racing game -> car, track, obstacle). 4-6 palette hex "
    "colors, background first. JSON only, no prose."
)


def _valid_palette(p) -> tuple[str, ...] | None:
    if not isinstance(p, list):
        return None
    hexes = [
        c.strip() for c in p
        if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c.strip())
    ]
    return tuple(hexes) if len(hexes) >= 2 else None


def _plan_from_llm(data, floor: ArtPlan) -> ArtPlan | None:
    """Build a tailored :class:`ArtPlan` from the model's JSON, or ``None`` if it is
    unusable (caller then keeps the deterministic floor). Sanitizes every field —
    fs-safe role keys, render mode in {sprite, primitive}, valid palette — so a
    sloppy model response can never produce a broken plan."""
    if not isinstance(data, dict) or data.get("stub") is True:
        return None
    raw = data.get("roles")
    if not isinstance(raw, list) or not raw:
        return None
    palette = _valid_palette(data.get("palette")) or floor.palette
    deduped: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in raw[:24]:  # bound the role count (a runaway response can't bloat)
        if not isinstance(item, dict):
            continue
        # Role keys become sprite FILENAMES and a JS identifier in the directive —
        # whitelist to fs/JS-safe snake and cap the length (no traversal / bloat).
        key = re.sub(r"[^a-z0-9]+", "_",
                     str(item.get("role") or item.get("name") or "").strip().lower())
        key = key.strip("_")[:40]
        if not key or key in seen:
            continue
        render = str(item.get("render") or "sprite").strip().lower()
        if render not in ("sprite", "primitive"):
            render = "sprite"
        # Subject feeds the image-generation prompt — collapse control chars so a
        # newline/tab/backtick can't corrupt the prompt (or a directive, if reused).
        subject = re.sub(
            r"\s+", " ", str(item.get("subject") or key.replace("_", " ")).strip()
        )[:80] or key
        seen.add(key)
        deduped.append((key, render, subject))
    if not deduped:
        return None
    colors = _colors_for([(k, r) for k, r, _ in deduped], palette)
    roles = {
        key: RoleArt(
            role=key, render=render, subject=subject,
            prompt=_sprite_prompt(key, subject) if render == "sprite" else "",
            variant=f"{key}_llm", color=colors[key],
        )
        for key, render, subject in deduped
    }
    # Genre flows into the codegen directive text — whitelist it the same way so a
    # malicious value can't inject quotes/newlines/instructions to the coder model.
    genre = re.sub(
        r"[^a-z0-9]+", "_", str(data.get("genre") or "").strip().lower()
    ).strip("_")[:40] or "custom"
    return ArtPlan(genre=genre, theme="llm", palette=palette, roles=roles)


async def plan_art_llm(brief: str | None, *, settings, llm=None) -> ArtPlan:
    """Tailor a game's art to the brief with one cheap LLM call, gated by
    ``settings.art_director_enabled`` (default off). Returns the deterministic
    ``direct_art`` floor when the flag is off or on ANY failure/stub/garbage —
    never raises, never blocks a build, costs nothing when disabled."""
    floor = direct_art(brief)
    if not bool(getattr(settings, "art_director_enabled", False)):
        return floor
    try:
        from skyn3t.agents._common import parse_json
        from skyn3t.adapters.llm import LLMClient
        from skyn3t.core.model_router import Tier

        client = llm or LLMClient(settings)
        result = await client.complete(
            _ART_PROMPT.format(brief=(brief or "").strip()[:2000]),
            tier=Tier.CHEAP, system=_ART_SYSTEM, json_mode=True,
            task_type="art_director",
        )
        if getattr(result, "backend", "") == "stub":
            return floor
        plan = _plan_from_llm(parse_json(getattr(result, "text", "") or ""), floor)
        if plan is None:
            return floor
        log.info("art_director.llm_plan", genre=plan.genre, roles=list(plan.roles))
        return plan
    except Exception as exc:  # noqa: BLE001 - art direction must never break a build
        log.warning("art_director.llm_failed", error=str(exc)[:160])
        return floor
