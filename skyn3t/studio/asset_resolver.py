"""Role->sprite resolver — the source-agnostic heart of the game art tier (#6).

A generated game needs sprites for a small set of game ROLES (player, enemy,
coin, ...). This module decides, deterministically and from the brief alone, a
themed plan for each role. The same plan drives BOTH art sources:

  * online  — the plan's ``prompt`` is the image-generation prompt (Replicate);
  * offline — the plan's ``variant`` keys a bundled CC0 sprite file.

Pure and never raises: a malformed/empty brief still yields a complete plan
(the default "arcade" theme). Brief keywords select a THEME (space, fantasy,
...) which sets each role's subject; the ``seed`` is reserved for palette-variant
selection once the bundled set carries palettes (it never changes role identity,
so two builds of the same brief are identical).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The roles a 2D game render needs. Kept small and stable — the scaffold and the
# bundled set are keyed to exactly these.
ROLES: tuple[str, ...] = ("player", "enemy", "coin", "platform", "projectile", "background")


@dataclass(frozen=True, slots=True)
class RolePlan:
    """A themed plan for one game role. ``subject`` is the themed noun; ``prompt``
    is the ready-to-use game-sprite generation prompt; ``variant`` is the bundled
    sprite key (``role_theme``) and the basename of its file."""

    role: str
    subject: str
    prompt: str
    variant: str


# theme -> {role: themed subject}. The default theme ("arcade") is the floor used
# when no world keyword matches. Subjects are intentionally concrete so both the
# generation prompt and the bundled art read as a real game object.
_THEMES: dict[str, dict[str, str]] = {
    "space": {
        "player": "sleek space fighter ship",
        "enemy": "menacing alien creature",
        "coin": "glowing energy orb",
        "platform": "metal hull panel",
        "projectile": "laser bolt",
        "background": "deep-space starfield",
    },
    "fantasy": {
        "player": "armored knight hero",
        "enemy": "green goblin",
        "coin": "shiny gold coin",
        "platform": "stone brick block",
        "projectile": "glowing magic arrow",
        "background": "castle hall interior",
    },
    "arcade": {
        "player": "cheerful hero character",
        "enemy": "bouncing slime monster",
        "coin": "shiny gold coin",
        "platform": "grassy ground block",
        "projectile": "energy bullet",
        "background": "bright blue sky with clouds",
    },
}

# Theme detection: WORD-bounded keyword match (never substring — the phaser-stack
# lesson: short ambiguous tokens steal unrelated briefs). First theme to match in
# iteration order wins; "arcade" is the default and needs no keywords.
_THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "space": ("space", "spaceship", "spaceships", "galaxy", "galactic", "alien", "aliens",
              "sci-fi", "scifi", "asteroid", "asteroids", "cosmic", "interstellar",
              "starship", "spacecraft", "ufo", "nebula"),
    "fantasy": ("fantasy", "medieval", "knight", "knights", "dragon", "dragons", "dungeon",
                "dungeons", "castle", "wizard", "sorcerer", "goblin", "goblins", "sword",
                "kingdom", "magic", "elf", "elves", "orc", "orcs"),
}


def _detect_theme(brief: str) -> str:
    words = set(re.findall(r"[a-z0-9][a-z0-9'-]*", (brief or "").lower()))
    for theme, keywords in _THEME_KEYWORDS.items():
        if words & set(keywords):
            return theme
    return "arcade"


def _prompt(role: str, subject: str) -> str:
    if role == "background":
        return (
            f"2D game background art of {subject}, wide seamless scene, "
            "flat vector illustration, soft depth, no text, no characters"
        )
    return (
        f"2D game sprite of a {subject}, centered, transparent background, "
        "clean flat shading, crisp edges, vibrant, no text"
    )


def plan_roles(brief: str, *, seed: int = 0) -> dict[str, RolePlan]:
    """Map every core role to a brief-themed :class:`RolePlan`. Deterministic and
    never raises. ``seed`` is accepted for forward-compatible palette selection;
    it does not alter role identity, so same brief -> same plan."""
    theme = _detect_theme(brief)
    subjects = _THEMES[theme]
    plan: dict[str, RolePlan] = {}
    for role in ROLES:
        subject = subjects[role]
        plan[role] = RolePlan(
            role=role,
            subject=subject,
            prompt=_prompt(role, subject),
            variant=f"{role}_{theme}",
        )
    return plan
