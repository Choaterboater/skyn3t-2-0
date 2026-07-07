"""Kenney all-in-one asset pack adapter.

The downloaded Kenney bundle is large and intentionally lives under ignored
``data/asset_packs``. This module indexes it just enough for SkyN3t's asset
pipeline to copy useful CC0 files into generated projects.
"""

from __future__ import annotations

import json
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

from skyn3t.agents.art_director import ArtPlan, RoleArt
from skyn3t.studio.asset_foundry import AssetItem

log = structlog.get_logger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_AUDIO_EXTS = {".wav", ".ogg", ".mp3", ".m4a"}
_NOISY_TERMS = {
    "archive",
    "license",
    "preview",
    "sample",
    "samples",
    "sheet",
    "sheets",
    "spritesheet",
    "spritesheets",
}
_PLAYER_TERMS = {"player", "character", "characters", "hero", "ship", "fighter", "adventurer"}
_ENEMY_TERMS = {
    "enemy",
    "enemies",
    "monster",
    "monsters",
    "alien",
    "aliens",
    "slime",
    "boss",
    "spider",
    "bat",
    "fly",
    "bee",
}
_ITEM_TERMS = {"coin", "coins", "gem", "gems", "star", "stars", "key", "keys", "item", "items"}
_TILE_TERMS = {"tile", "tiles", "terrain", "ground", "grass", "dirt", "stone", "block", "platform"}
_UI_TERMS = {"ui", "interface", "button", "buttons", "panel", "cursor", "hud", "icon", "icons"}
_AUDIO_TERMS = {"audio", "sound", "sounds", "sfx", "music", "jingle", "jingles", "click", "hit"}


def _tokens(value: str) -> tuple[str, ...]:
    parts = re.findall(r"[a-z0-9]+", value.lower().replace("_", " ").replace("-", " "))
    out: list[str] = []
    for part in parts:
        out.append(part)
        if part.endswith("s") and len(part) > 3:
            out.append(part[:-1])
    return tuple(dict.fromkeys(out))


def _default_pack_roots(data_dir: str | Path) -> list[Path]:
    base = Path(data_dir) / "asset_packs"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and "kenney" in p.name.lower())


def has_kenney_pack(data_dir: str | Path) -> bool:
    return bool(_default_pack_roots(data_dir))


def _kind_for(path: Path, terms: set[str]) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _AUDIO_EXTS:
        return "music" if "music" in terms or "jingle" in terms or "loop" in terms else "audio"
    if suffix not in _IMAGE_EXTS:
        return None
    if terms & _UI_TERMS:
        return "ui"
    if terms & _TILE_TERMS:
        return "tile"
    if "sheet" in terms or "spritesheet" in terms:
        return "spritesheet"
    return "sprite"


def _tags_for(path: Path, root: Path) -> tuple[str, ...]:
    rel = path.relative_to(root).as_posix()
    terms = set(_tokens(rel))
    tags = set(terms)
    if terms & _PLAYER_TERMS:
        tags.add("player")
    if terms & _ENEMY_TERMS:
        tags.add("enemy")
    if terms & _ITEM_TERMS:
        tags.add("item")
    if {"coin", "coins", "gold"} & terms:
        tags.add("coin")
    if terms & _TILE_TERMS:
        tags.add("tile")
    if terms & _UI_TERMS:
        tags.add("ui")
    if "click" in terms:
        tags.add("click")
    if "jump" in terms:
        tags.add("jump")
    if "collect" in terms or "coin" in terms:
        tags.add("collect")
    if "shoot" in terms or "laser" in terms:
        tags.add("shoot")
    if "explosion" in terms or "explode" in terms:
        tags.add("explosion")
    if "loop" in terms or "music" in terms:
        tags.add("loop")
    return tuple(sorted(tags))


def _asset_id_for(kind: str, path: Path, root: Path, tags: tuple[str, ...]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_") or "asset"
    if kind == "tile":
        prefix = "tile"
    elif kind == "ui":
        prefix = "ui"
    elif kind in {"audio", "music"}:
        prefix = kind
    else:
        prefix = "sprite"
    role = "player" if "player" in tags else "enemy" if "enemy" in tags else "coin" if "coin" in tags else stem
    rel_bits = "_".join(_tokens(path.relative_to(root).parent.as_posix())[-3:])
    suffix = f"{rel_bits}_{stem}" if rel_bits else stem
    return f"{prefix}/{role}/{suffix}" if prefix == "sprite" else f"{prefix}/{suffix}"


def _classify_kenney_asset(path: Path, root: Path) -> AssetItem | None:
    rel = path.relative_to(root).as_posix()
    terms = set(_tokens(rel))
    if path.suffix.lower() not in _IMAGE_EXTS | _AUDIO_EXTS:
        return None
    if "3d" in terms or "vector" in terms or "svg" in terms or "license" in terms:
        return None
    kind = _kind_for(path, terms)
    if kind is None:
        return None
    tags = _tags_for(path, root)
    asset_id = _asset_id_for(kind, path, root, tags)
    return AssetItem(
        asset_id=asset_id,
        kind=kind,
        path=rel,
        tags=tags,
        license="CC0-1.0",
        credit="Kenney",
        source_path=str(path),
    )


@lru_cache(maxsize=16)
def _load_kenney_assets_cached(root: str, limit: int) -> tuple[AssetItem, ...]:
    pack_root = Path(root)
    items: list[AssetItem] = []
    for path in sorted(pack_root.rglob("*")):
        if not path.is_file():
            continue
        item = _classify_kenney_asset(path, pack_root)
        if item is None:
            continue
        items.append(item)
        if limit > 0 and len(items) >= limit:
            break
    return tuple(items)


def load_kenney_assets(root: str | Path, *, limit: int | None = None) -> list[AssetItem]:
    """Return catalog items for a Kenney bundle root."""
    return list(_load_kenney_assets_cached(str(Path(root).resolve()), int(limit or 0)))


def discover_kenney_assets(
    data_dir: str | Path,
    *,
    pack_roots: list[str | Path] | tuple[str | Path, ...] = (),
    limit_per_pack: int | None = None,
) -> tuple[list[AssetItem], list[str]]:
    roots = [Path(p) for p in pack_roots] if pack_roots else _default_pack_roots(data_dir)
    items: list[AssetItem] = []
    sources: list[str] = []
    for root in sorted({p.resolve() for p in roots if p.is_dir()}):
        try:
            found = load_kenney_assets(root, limit=limit_per_pack)
        except Exception as exc:  # noqa: BLE001 - a bad local pack should not stop builds
            log.warning("kenney_assets.pack_failed", pack=str(root), error=str(exc)[:180])
            continue
        if found:
            items.extend(found)
            sources.append(str(root))
    return items, sources


def _role_terms(role: str, art: RoleArt) -> set[str]:
    terms = set(_tokens(f"{role} {art.subject} {art.variant}"))
    if role in {"ship", "boat", "tower"}:
        terms.add("player")
    if role in {"enemy", "interceptor", "gunship", "brute", "flyer", "boss"}:
        terms.add("enemy")
    if role.startswith("powerup") or role in {"coin", "gold", "item", "key"}:
        terms.update({"coin", "item"})
    return terms


def _role_score(item: AssetItem, role: str, art: RoleArt) -> tuple[int, int, int]:
    if item.kind not in {"sprite", "tile", "ui"}:
        return (-1, 0, 0)
    tags = set(item.tags)
    role_terms = _role_terms(role, art)
    overlap = len(tags & role_terms)
    if overlap == 0:
        return (-1, 0, 0)
    noisy = len(tags & _NOISY_TERMS)
    exact = 5 if role in tags or role in item.path.lower() else 0
    preferred_kind = 2 if item.kind == "sprite" else 0
    return (overlap * 10 + exact + preferred_kind - noisy * 8, -len(item.path), -len(tags))


def _best_for_role(
    catalog: list[AssetItem],
    role: str,
    art: RoleArt,
    *,
    used_sources: set[str] | None = None,
) -> AssetItem | None:
    best: AssetItem | None = None
    best_score: tuple[int, int, int] | None = None
    used = used_sources or set()
    for item in catalog:
        if item.source_path in used:
            continue
        score = _role_score(item, role, art)
        if score[0] < 0:
            continue
        if best_score is None or score > best_score:
            best = item
            best_score = score
    if best is not None or not used:
        return best
    # If a tiny pack has fewer matching files than roles, reuse the best match
    # rather than dropping a sprite role entirely.
    return _best_for_role(catalog, role, art, used_sources=None)


def write_kenney_role_sprites(
    project_dir: str | Path,
    art_plan: ArtPlan,
    *,
    data_dir: str | Path,
    pack_roots: list[str | Path] | tuple[str | Path, ...] = (),
) -> dict[str, Any]:
    """Copy one matching Kenney PNG per sprite role into the generated project."""
    catalog, sources = discover_kenney_assets(data_dir, pack_roots=pack_roots)
    sprites = [item for item in catalog if Path(item.source_path).suffix.lower() in _IMAGE_EXTS]
    sprite_roles = art_plan.sprite_roles()
    sprites_dir = Path(project_dir) / "public" / "assets" / "sprites"
    role_map: dict[str, str] = {}
    selected: dict[str, dict[str, Any]] = {}
    try:
        sprites_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("kenney_assets.mkdir_failed", error=str(exc)[:160])
        return {
            "generated": 0,
            "skipped": True,
            "reason": "mkdir_failed",
            "source": "kenney",
            "role_map": {},
            "sources": sources,
            "genre": art_plan.genre,
            "palette": list(art_plan.palette),
        }

    used_sources: set[str] = set()
    for role, art in sprite_roles.items():
        item = _best_for_role(sprites, role, art, used_sources=used_sources)
        if item is None:
            continue
        dest = sprites_dir / f"{role}.png"
        try:
            shutil.copy2(item.source_path, dest)
        except OSError as exc:
            log.warning("kenney_assets.copy_failed", role=role, error=str(exc)[:160])
            continue
        role_map[role] = f"/assets/sprites/{role}.png"
        used_sources.add(item.source_path)
        selected[role] = {
            "asset_id": item.asset_id,
            "source_path": item.source_path,
            "tags": list(item.tags),
            "license": item.license,
            "credit": item.credit,
        }

    if not sprite_roles:
        reason = "all_primitive"
    elif not sources:
        reason = "no_kenney_pack"
    elif not role_map:
        reason = "no_matches"
    else:
        reason = ""
    manifest = {
        "generated": len(role_map),
        "skipped": len(role_map) == 0,
        "reason": reason,
        "source": "kenney",
        "genre": art_plan.genre,
        "palette": list(art_plan.palette),
        "role_map": role_map,
        "selected": selected,
        "sources": sources,
    }
    try:
        (sprites_dir / "assets.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("kenney_assets.manifest_failed", error=str(exc)[:160])
    return manifest
