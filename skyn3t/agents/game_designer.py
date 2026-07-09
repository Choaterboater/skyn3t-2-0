"""Game-designer (roadmap #7) — the "deep" rung of the game stack.

A cheap codegen model left to itself builds a thin, one-mechanic toy. This module
GUARANTEES depth: ``design_game(brief)`` produces a genre-aware Game Design Document
(GDD) — core loop, explicit win/lose, level/difficulty progression, several distinct
mechanics, POWERUPS/upgrades, enemy/obstacle variety, and an economy — which the
codegen directive (``code_agent._game_depth_directive``) then DEMANDS. So every
generated game ships with the depth elements instead of improvising them away.

The deterministic floor here is always-on and $0; it reuses the art-director's genre
detection (via ``direct_art``) so art and design agree on the genre. The optional LLM
layer (``design_game_llm``, gated by ``game_designer_enabled``) tailors the GDD to the
specific brief and is threaded by the runner exactly like the art plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass
class GameDesign:
    """A game's depth contract. ``mechanics``/``powerups``/``variety`` are short,
    concrete lists the directive turns into hard requirements. ``open_ended`` marks a
    game the genre table didn't recognize (a general but still depth-complete GDD)."""

    genre: str
    core_loop: str
    win: str
    lose: str
    progression: str
    mechanics: tuple[str, ...]
    powerups: tuple[str, ...]
    variety: tuple[str, ...]
    economy: str
    open_ended: bool = False

    def to_dict(self) -> dict:
        return {
            "genre": self.genre, "core_loop": self.core_loop, "win": self.win,
            "lose": self.lose, "progression": self.progression,
            "mechanics": list(self.mechanics), "powerups": list(self.powerups),
            "variety": list(self.variety), "economy": self.economy,
            "open_ended": self.open_ended,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GameDesign:
        return cls(
            genre=d.get("genre", "general"),
            core_loop=d.get("core_loop", ""), win=d.get("win", ""),
            lose=d.get("lose", ""), progression=d.get("progression", ""),
            mechanics=tuple(d.get("mechanics") or ()),
            powerups=tuple(d.get("powerups") or ()),
            variety=tuple(d.get("variety") or ()),
            economy=d.get("economy", ""), open_ended=bool(d.get("open_ended", False)),
        )


# Genre -> depth spec. Keyed to the art-director's genres so art and design agree.
# Every spec carries the full depth contract; the directive turns it into demands.
_DEPTH: dict[str, dict] = {
    "space_shooter": {
        "core_loop": "fly, dodge incoming fire, and shoot escalating waves of enemies",
        "win": "survive every wave and defeat the end boss",
        "lose": "lose all lives (3) to enemy fire or collisions",
        "progression": "multiple waves of rising difficulty, new enemy patterns each wave, a boss finale",
        "mechanics": ("8-way movement", "shooting", "dodging", "power-up pickups"),
        "powerups": ("rapid fire", "spread/triple shot", "shield", "screen-clear bomb", "extra life"),
        "variety": ("straight-diver enemy", "zig-zag enemy", "tanky shooter enemy", "boss with phases"),
        "economy": "score from kills with a combo multiplier for chained hits",
    },
    "breakout": {
        "core_loop": "angle the paddle to bounce the ball and break every brick",
        "win": "clear all bricks across all level layouts",
        "lose": "drop the ball and lose all lives (3)",
        "progression": "several hand-built level layouts, faster ball and tougher bricks each level",
        "mechanics": ("paddle control", "ball physics/angles", "brick destruction", "power-up drops"),
        "powerups": ("multi-ball", "wide paddle", "laser paddle", "slow ball", "extra life"),
        "variety": ("1-hit brick", "multi-hit brick", "unbreakable brick", "power-up brick"),
        "economy": "score per brick with a streak bonus for fast clears",
    },
    "platformer": {
        "core_loop": "run and jump across platforms, avoid hazards, collect items, reach the goal",
        "win": "reach the exit flag of the final level",
        "lose": "lose all lives (3) to enemies, hazards, or falling",
        "progression": "multiple hand-built levels, new hazards and enemy types as you go",
        "mechanics": ("run", "variable-height jump", "enemy stomp", "item collection"),
        "powerups": ("double jump", "speed boost", "temporary invincibility", "extra life"),
        "variety": ("patrolling walker", "flying enemy", "spike/pit hazard", "moving platform"),
        "economy": "coins plus score; coins can extend lives",
    },
    "tower_defense": {
        "core_loop": "place and upgrade towers along the path to stop waves of creeps",
        "win": "survive all waves without the base falling",
        "lose": "enemies that reach the base drain your lives to 0",
        "progression": "escalating waves, new enemy types and a boss, more gold to spend each wave",
        "mechanics": ("tower placement", "tower upgrading", "gold economy", "targeting priorities"),
        "powerups": ("tower damage/range upgrades", "freeze ability", "airstrike ability", "sell/refund"),
        "variety": ("fast creep", "armored creep", "flying creep", "boss creep"),
        "economy": "gold earned per kill, spent on building and upgrading towers",
    },
    "snake": {
        "core_loop": "steer the snake to eat food and grow without crashing",
        "win": "reach a target length / score",
        "lose": "run into a wall, an obstacle, or your own tail",
        "progression": "speed ramps up as you grow; obstacles appear at higher scores",
        "mechanics": ("grid movement", "growth", "self/wall collision", "pickups"),
        "powerups": ("slow-motion", "shrink", "double-points food", "phase-through-wall"),
        "variety": ("normal food", "bonus timed food", "obstacle block"),
        "economy": "score per food with bonus food worth more",
    },
    "pong": {
        "core_loop": "rally the ball past the opponent's paddle",
        "win": "be first to the target score (11)",
        "lose": "the opponent reaches the target score first",
        "progression": "ball speeds up each rally; the AI opponent gets sharper",
        "mechanics": ("paddle control", "ball physics/spin", "AI opponent"),
        "powerups": ("speed paddle", "ball curve/spin", "shrink-opponent"),
        "variety": ("easy/normal/hard AI", "optional obstacle in the arena"),
        "economy": "match score to the win target",
    },
    "tetris": {
        "core_loop": "rotate and drop falling pieces to complete and clear lines",
        "win": "endless survival with a target score / level to reach",
        "lose": "the stack reaches the top of the well",
        "progression": "drop speed increases each level as lines are cleared",
        "mechanics": ("piece movement", "rotation", "soft/hard drop", "line clears"),
        "powerups": ("hold piece", "ghost-piece preview", "next-queue", "occasional clear-bomb"),
        "variety": ("the 7 standard tetrominoes", "an optional special piece"),
        "economy": "score per line (more for multi-line clears) with level-up",
    },
    "top_down": {
        "core_loop": "explore rooms, fight enemies, collect items, find the exit",
        "win": "clear the final room / defeat the boss",
        "lose": "lose all health",
        "progression": "multiple rooms/levels with tougher enemies and locked doors + keys",
        "mechanics": ("8-way movement", "melee/ranged combat", "item pickups", "doors and keys"),
        "powerups": ("health pack", "weapon upgrade", "speed boost", "temporary shield"),
        "variety": ("melee enemy", "ranged enemy", "boss", "locked door + key"),
        "economy": "gold/score plus keys that unlock progress",
    },
    "general": {
        "core_loop": "the brief's main repeated action, tight and immediately readable",
        "win": "a clear, reachable win condition appropriate to this game",
        "lose": "a clear lose/fail condition (lives, health, or a timer)",
        "progression": "escalating difficulty across multiple levels or stages",
        "mechanics": ("at least three distinct, interacting mechanics", "responsive controls", "clear feedback"),
        "powerups": ("at least two earnable power-ups or upgrades", "a meaningful third if it fits"),
        "variety": ("two or more distinct enemy/obstacle/challenge types", "a stand-out level or boss moment"),
        "economy": "a score and/or currency the player earns and can spend",
    },
}


def design_game(brief) -> GameDesign:
    """Produce a genre-aware, depth-complete GDD for ``brief``. Deterministic and
    never raises; reuses ``direct_art``'s genre so art and design agree. A game the
    genre table doesn't recognize gets the (still depth-complete) ``general`` spec."""
    from skyn3t.agents.art_director import direct_art

    plan = direct_art(brief)  # already coerces/never-raises
    genre = "general" if plan.open_ended else plan.genre
    spec = _DEPTH.get(genre, _DEPTH["general"])
    return GameDesign(
        genre=genre,
        core_loop=spec["core_loop"], win=spec["win"], lose=spec["lose"],
        progression=spec["progression"], mechanics=tuple(spec["mechanics"]),
        powerups=tuple(spec["powerups"]), variety=tuple(spec["variety"]),
        economy=spec["economy"], open_ended=plan.open_ended,
    )


# ---- LLM game-designer (per-game tailored depth) --------------------------
# Optional cheap LLM call that tailors the GDD to the brief; gated by
# ``game_designer_enabled`` (default off), NEVER raises, ALWAYS falls back to the
# deterministic floor, and GUARANTEES the depth contract (>=2 power-ups, win+lose)
# by backfilling from the floor. Threaded by the runner like the art plan.
_GD_SYSTEM = (
    "You are a 2D game designer. Given a brief, design a tight, DEEP game: a clear "
    "core loop, reachable win and lose conditions, level/difficulty progression, "
    "several interacting mechanics, at least 2-3 POWER-UPS/upgrades with real "
    "effects, distinct enemy/obstacle types, and a scoring/economy. JSON only."
)
_GD_PROMPT = (
    "Game brief: {brief}\n\n"
    'Return JSON exactly like: {{"genre": "<short>", "core_loop": "<one sentence>", '
    '"win": "<win condition>", "lose": "<lose condition>", "progression": "<how it '
    'escalates / levels>", "mechanics": ["<m1>", "<m2>", "<m3>"], "powerups": '
    '["<p1>", "<p2>", "<p3>"], "variety": ["<enemy/obstacle types>"], "economy": '
    '"<scoring/currency>"}}. Make it specific to THIS game. JSON only, no prose.'
)


_CTRL = re.compile(r"[\x00-\x1f\x7f]+")  # control chars (incl. non-whitespace ones)


def _scrub(s: str, cap: int) -> str:
    # Strip ALL control chars (not just \n/\t) then collapse whitespace — these
    # strings flow into the codegen directive, so nothing may corrupt the prompt.
    return re.sub(r"\s+", " ", _CTRL.sub(" ", s)).strip()[:cap]


def _clean_str(v, fallback: str, cap: int = 240) -> str:
    if not isinstance(v, str):
        return fallback
    return _scrub(v, cap) or fallback


def _clean_list(v, fallback: tuple[str, ...], cap_items: int = 8, cap_len: int = 60) -> tuple[str, ...]:
    if not isinstance(v, list):
        return fallback
    out = [s for item in v[:cap_items] if isinstance(item, str) and (s := _scrub(item, cap_len))]
    return tuple(out) if out else fallback


def _design_from_llm(data, floor: GameDesign) -> GameDesign | None:
    if not isinstance(data, dict) or data.get("stub") is True:
        return None
    # If the model contributed NOTHING usable, fall back to the floor entirely (don't
    # synthesize a floor-valued design that merely differs by open_ended).
    _has_str = False
    for key in ("core_loop", "win", "lose", "progression", "economy"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            _has_str = True
            break
    _has_list = False
    for key in ("mechanics", "powerups", "variety"):
        value = data.get(key)
        if isinstance(value, list) and any(
            isinstance(item, str) and item.strip() for item in value
        ):
            _has_list = True
            break
    if not (_has_str or _has_list):
        return None
    # Genre flows into the codegen directive text -> whitelist like role keys.
    genre = re.sub(
        r"[^a-z0-9]+", "_", str(data.get("genre") or "").strip().lower()
    ).strip("_")[:40] or floor.genre
    powerups = _clean_list(data.get("powerups"), floor.powerups)
    if len(powerups) < 2:  # GUARANTEE the depth contract regardless of the model
        powerups = floor.powerups
    return GameDesign(
        genre=genre,
        core_loop=_clean_str(data.get("core_loop"), floor.core_loop),
        win=_clean_str(data.get("win"), floor.win),
        lose=_clean_str(data.get("lose"), floor.lose),
        progression=_clean_str(data.get("progression"), floor.progression),
        mechanics=_clean_list(data.get("mechanics"), floor.mechanics),
        powerups=powerups,
        variety=_clean_list(data.get("variety"), floor.variety),
        economy=_clean_str(data.get("economy"), floor.economy),
        open_ended=False,
    )


async def design_game_llm(brief, *, settings, llm=None) -> GameDesign:
    """Tailor the GDD to the brief with one cheap LLM call, gated by
    ``settings.game_designer_enabled`` (default off). Returns the deterministic
    ``design_game`` floor when the flag is off or on ANY failure/stub/garbage — never
    raises, costs nothing when disabled, and always satisfies the depth contract."""
    floor = design_game(brief)
    if not bool(getattr(settings, "game_designer_enabled", False)):
        return floor
    try:
        from skyn3t.adapters.llm import LLMClient
        from skyn3t.agents._common import parse_json
        from skyn3t.core.model_router import Tier

        client = llm or LLMClient(settings)
        result = await client.complete(
            _GD_PROMPT.format(brief=str(brief or "").strip()[:2000]),
            tier=Tier.CHEAP, system=_GD_SYSTEM, json_mode=True,
            task_type="game_designer",
        )
        if getattr(result, "backend", "") == "stub":
            return floor
        gd = _design_from_llm(parse_json(getattr(result, "text", "") or ""), floor)
        if gd is None:
            return floor
        log.info("game_designer.llm_gdd", genre=gd.genre, powerups=len(gd.powerups))
        return gd
    except Exception as exc:  # noqa: BLE001 - design must never break a build
        log.warning("game_designer.llm_failed", error=str(exc)[:160])
        return floor
