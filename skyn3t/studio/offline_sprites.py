"""Deterministic offline role sprites for game builds.

This is the $0 floor for `game_art_source=offline`: small transparent PNGs
derived from the role art plan, written into the same public/assets/sprites
contract that Replicate sprites use.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import structlog

from skyn3t.agents.art_director import ArtPlan, RoleArt

log = structlog.get_logger(__name__)

SPRITE_SIZE = 96


def _safe_role(role: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", role.lower()).strip("_") or "sprite"


def _rgb(hex_color: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    raw = str(hex_color or "").strip().lstrip("#")
    if len(raw) != 6:
        return fallback
    try:
        return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return fallback


def _role_index(role: str, genre: str) -> int:
    return sum(ord(ch) for ch in f"{genre}:{role}")


def _draw_polygon(
    draw: Any,
    center: tuple[int, int],
    radius: int,
    sides: int,
    fill: Any,
    outline: Any,
    angle: float,
) -> None:
    cx, cy = center
    pts = []
    for i in range(sides):
        theta = angle + (math.tau * i / sides)
        pts.append((cx + math.cos(theta) * radius, cy + math.sin(theta) * radius))
    draw.polygon(pts, fill=fill, outline=outline)


def _draw_sprite_png(role: RoleArt, plan: ArtPlan) -> bytes:
    import io

    from PIL import Image, ImageDraw, ImageFont

    idx = _role_index(role.role, plan.genre)
    palette = list(plan.palette or ())
    base = _rgb(role.color, (96, 165, 250))
    accent = (
        _rgb(palette[(idx % max(1, len(palette)))], (251, 191, 36))
        if palette
        else (251, 191, 36)
    )
    outline = tuple(max(0, c - 64) for c in base)

    im = Image.new("RGBA", (SPRITE_SIZE, SPRITE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    fill = (*base, 235)
    accent_fill = (*accent, 230)
    outline_fill = (*outline, 255)
    shape = idx % 5

    if shape == 0:
        draw.ellipse((18, 16, 78, 76), fill=fill, outline=outline_fill, width=3)
        draw.ellipse((38, 34, 58, 54), fill=accent_fill)
    elif shape == 1:
        _draw_polygon(draw, (48, 45), 34, 3, fill, outline_fill, -math.pi / 2)
        draw.rectangle((42, 46, 54, 78), fill=accent_fill)
    elif shape == 2:
        draw.rounded_rectangle(
            (18, 20, 78, 70),
            radius=12,
            fill=fill,
            outline=outline_fill,
            width=3,
        )
        draw.rectangle((28, 32, 68, 44), fill=accent_fill)
    elif shape == 3:
        _draw_polygon(draw, (48, 45), 33, 5, fill, outline_fill, -math.pi / 2)
        draw.ellipse((36, 33, 60, 57), fill=accent_fill)
    else:
        draw.rectangle((22, 22, 74, 72), fill=fill, outline=outline_fill, width=3)
        draw.polygon(
            ((48, 12), (70, 45), (48, 82), (26, 45)),
            fill=accent_fill,
            outline=outline_fill,
        )

    label = "".join(part[:1] for part in _safe_role(role.role).split("_"))[:3].upper()
    font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.rounded_rectangle(
        (48 - tw / 2 - 4, 76, 48 + tw / 2 + 4, 91),
        radius=3,
        fill=(0, 0, 0, 150),
    )
    draw.text((48 - tw / 2, 78 + (11 - th) / 2), label, font=font, fill=(255, 255, 255, 245))

    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def write_offline_role_sprites(project_dir: str | Path, art_plan: ArtPlan) -> dict[str, Any]:
    """Write deterministic local role sprites and return a role-sprite manifest."""
    sprite_roles = art_plan.sprite_roles()
    base_manifest: dict[str, Any] = {
        "generated": 0,
        "skipped": True,
        "reason": "no_sprite_roles",
        "source": "offline",
        "genre": art_plan.genre,
        "palette": list(art_plan.palette),
        "role_map": {},
    }
    if not sprite_roles:
        return base_manifest

    sprites_dir = Path(project_dir) / "public" / "assets" / "sprites"
    try:
        sprites_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("offline_sprites.mkdir_failed", error=str(exc)[:160])
        return {**base_manifest, "reason": "mkdir_failed"}

    role_map: dict[str, str] = {}
    for role_name, role in sprite_roles.items():
        safe = _safe_role(role_name)
        try:
            (sprites_dir / f"{safe}.png").write_bytes(_draw_sprite_png(role, art_plan))
        except Exception as exc:  # noqa: BLE001 - one sprite must not fail a build
            log.warning("offline_sprites.role_failed", role=role_name, error=str(exc)[:160])
            continue
        role_map[role_name] = f"/assets/sprites/{safe}.png"

    manifest = {
        "generated": len(role_map),
        "skipped": len(role_map) == 0,
        "reason": "" if role_map else "write_failed",
        "source": "offline",
        "genre": art_plan.genre,
        "palette": list(art_plan.palette),
        "role_map": role_map,
    }
    if role_map:
        try:
            (sprites_dir / "assets.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            log.warning("offline_sprites.manifest_failed", error=str(exc)[:160])
    return manifest
