"""Asset Foundry v1 — normalize packs, resolve requirements, write manifests.

The foundry is the full-game asset layer above role sprites: it turns a game
brief, art plan, and GDD into concrete asset requirements, matches them against a
catalog of local packs or existing project assets, and writes the selected files
plus manifests and credits into the generated project.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from skyn3t.agents.art_director import ArtPlan, direct_art
from skyn3t.agents.game_designer import GameDesign, design_game

log = structlog.get_logger(__name__)

_IMAGE_KINDS = {"sprite", "spritesheet", "atlas", "tile", "ui"}
_AUDIO_KINDS = {"audio", "music", "sfx"}
_ROOT_ASSET_DIR = "public/assets"
_CC0_LICENSES = {"cc0", "cc0-1.0", "public domain", "unlicense"}


@dataclass(frozen=True, slots=True)
class AssetItem:
    asset_id: str
    kind: str
    path: str
    tags: tuple[str, ...] = ()
    license: str = ""
    credit: str = ""
    source_path: str = ""
    fps: int = 0
    loop: bool = True
    frame_width: int = 0
    frame_height: int = 0


@dataclass(frozen=True, slots=True)
class AssetRequirement:
    asset_id: str
    kind: str
    priority: int
    tags: tuple[str, ...] = ()
    optional: bool = False


@dataclass(slots=True)
class AssetPlan:
    selected: dict[str, AssetItem] = field(default_factory=dict)
    missing: list[AssetRequirement] = field(default_factory=list)
    requirements: list[AssetRequirement] = field(default_factory=list)
    catalog_size: int = 0
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": {
                key: {
                    "asset_id": item.asset_id,
                    "kind": item.kind,
                    "path": item.path,
                    "tags": list(item.tags),
                    "license": item.license,
                    "credit": item.credit,
                    "source_path": item.source_path,
                    "fps": item.fps,
                    "loop": item.loop,
                    "frame_width": item.frame_width,
                    "frame_height": item.frame_height,
                }
                for key, item in self.selected.items()
            },
            "missing": [
                {
                    "asset_id": req.asset_id,
                    "kind": req.kind,
                    "priority": req.priority,
                    "tags": list(req.tags),
                    "optional": req.optional,
                }
                for req in self.missing
            ],
            "requirements": [
                {
                    "asset_id": req.asset_id,
                    "kind": req.kind,
                    "priority": req.priority,
                    "tags": list(req.tags),
                    "optional": req.optional,
                }
                for req in self.requirements
            ],
            "catalog_size": self.catalog_size,
            "sources": list(self.sources),
        }


def _as_tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return tuple(out)
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _safe_pack_path(pack_root: Path, rel_path: str) -> Path:
    target = (pack_root / rel_path).resolve()
    root = pack_root.resolve()
    if root not in target.parents and target != root:
        raise ValueError("asset path escapes outside the pack directory")
    return target


def load_local_asset_pack(path: str | Path) -> list[AssetItem]:
    """Load one local pack directory with a pack.json manifest."""
    pack_root = Path(path)
    manifest_path = pack_root / "pack.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = data.get("assets") or []
    out: list[AssetItem] = []
    for entry in assets:
        if not isinstance(entry, dict):
            continue
        asset_id = str(entry.get("id") or "").strip()
        kind = str(entry.get("kind") or "").strip().lower()
        rel_path = str(entry.get("path") or "").strip()
        if not asset_id or not kind or not rel_path:
            continue
        source_path = _safe_pack_path(pack_root, rel_path)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        out.append(
            AssetItem(
                asset_id=asset_id,
                kind=kind,
                path=rel_path,
                tags=_as_tags(entry.get("tags")),
                license=str(entry.get("license") or data.get("license") or ""),
                credit=str(entry.get("credit") or data.get("credit") or ""),
                source_path=str(source_path),
                fps=int(entry.get("fps") or 0),
                loop=bool(entry.get("loop", True)),
                frame_width=int(entry.get("frame_width") or 0),
                frame_height=int(entry.get("frame_height") or 0),
            )
        )
    return out


def discover_local_asset_packs(project_dir: str | Path) -> tuple[list[AssetItem], list[str]]:
    """Discover local pack directories under `asset_packs/`."""
    root = Path(project_dir)
    base = root / "asset_packs"
    if not base.is_dir():
        return [], []
    items: list[AssetItem] = []
    sources: list[str] = []
    for pack_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        pack_json = pack_dir / "pack.json"
        if not pack_json.is_file():
            continue
        try:
            items.extend(load_local_asset_pack(pack_dir))
            sources.append(str(pack_dir))
        except Exception as exc:  # noqa: BLE001 - a bad pack should not stop the build
            log.warning("asset_foundry.pack_failed", pack=str(pack_dir), error=str(exc)[:180])
    return items, sources


def collect_project_catalog(project_dir: str | Path) -> tuple[list[AssetItem], list[str]]:
    """Collect local packs plus any existing project assets as a catalog."""
    root = Path(project_dir)
    items, sources = discover_local_asset_packs(root)
    sprite_dir = root / "public" / "assets" / "sprites"
    if sprite_dir.is_dir():
        for p in sorted(sprite_dir.glob("**/*")):
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                rel = p.relative_to(root / "public" / "assets").as_posix()
                items.append(
                    AssetItem(
                        asset_id=f"sprite/{p.stem}",
                        kind="sprite",
                        path=rel,
                        tags=(p.stem,),
                        license="CC0-1.0",
                        credit="",
                        source_path=str(p),
                    )
                )
    audio_root = root / "public" / "assets" / "audio"
    if audio_root.is_dir():
        for p in sorted(audio_root.glob("**/*")):
            if p.is_file() and p.suffix.lower() in {".wav", ".ogg", ".mp3", ".m4a"}:
                rel = p.relative_to(root / "public" / "assets").as_posix()
                items.append(
                    AssetItem(
                        asset_id=f"audio/{p.stem}",
                        kind="audio",
                        path=rel,
                        tags=(p.stem,),
                        license="unknown",
                        credit="",
                        source_path=str(p),
                    )
                )
    return items, sources


def _theme_from_art_plan(art_plan: ArtPlan | None, game_design: GameDesign | None) -> str:
    if isinstance(game_design, GameDesign) and game_design.genre:
        return game_design.genre
    if isinstance(art_plan, ArtPlan) and art_plan.genre:
        return art_plan.genre
    return "general"


def derive_asset_requirements(
    brief: str,
    art_plan: ArtPlan | None,
    game_design: GameDesign | None,
) -> list[AssetRequirement]:
    """Turn the brief/design into concrete role, animation, tile, and audio needs."""
    genre = _theme_from_art_plan(art_plan, game_design)
    reqs: list[AssetRequirement] = []

    def add(asset_id: str, kind: str, priority: int, *tags: str, optional: bool = False) -> None:
        reqs.append(
            AssetRequirement(
                asset_id=asset_id,
                kind=kind,
                priority=priority,
                tags=tuple(t for t in tags if t),
                optional=optional,
            )
        )

    if genre == "platformer":
        add("sprite/player/idle/down", "sprite", 100, "player", "idle", "down")
        add("sprite/player/walk/down", "sprite", 95, "player", "walk", "down")
        add("sprite/player/jump/down", "sprite", 90, "player", "jump", "down")
        add("sprite/enemy/walk/down", "sprite", 85, "enemy", "walk", "down")
        add("sprite/coin/idle", "sprite", 80, "coin")
        add("tile/forest/ground", "tile", 70, "forest", "ground")
        add("audio/player/jump", "audio", 65, "jump")
        add("audio/coin/collect", "audio", 60, "coin", "collect")
        add("music/level/loop", "music", 50, "loop")
    elif genre == "top_down":
        for direction in ("up", "down", "left", "right"):
            add(f"sprite/player/walk/{direction}", "sprite", 100 - len(reqs), "player", direction)
        add("sprite/enemy/walk/down", "sprite", 85, "enemy", "walk", "down")
        add("sprite/item/idle", "sprite", 80, "item")
        add("audio/player/hit", "audio", 65, "hit")
        add("music/level/loop", "music", 50, "loop")
    elif genre == "space_shooter":
        add("sprite/ship/idle/up", "sprite", 100, "ship", "up")
        add("sprite/enemy/idle/down", "sprite", 95, "enemy", "down")
        add("sprite/boss/idle/down", "sprite", 90, "boss", "down")
        add("sprite/powerup_rapid/idle", "sprite", 85, "powerup", "rapid")
        add("audio/player/shoot", "audio", 70, "shoot")
        add("audio/enemy/explosion", "audio", 65, "explosion")
        add("music/level/loop", "music", 50, "loop")
    else:
        add("sprite/player/idle/down", "sprite", 100, "player", "idle", "down")
        add("sprite/enemy/idle/down", "sprite", 95, "enemy", "down")
        add("sprite/coin/idle", "sprite", 90, "coin")
        add("audio/ui/click", "audio", 60, "ui", "click", optional=True)
        add("music/level/loop", "music", 50, "loop", optional=True)

    # Generic directional and animation fallbacks across genres.
    add("sprite/player/walk/right", "sprite", 45, "player", "walk", "right", optional=True)
    add("sprite/player/walk/left", "sprite", 44, "player", "walk", "left", optional=True)
    add("sprite/player/idle", "sprite", 43, "player", "idle", optional=True)
    add("audio/ui/click", "audio", 42, "ui", "click", optional=True)

    return reqs


def _score_match(req: AssetRequirement, item: AssetItem) -> tuple[int, int, int]:
    if req.asset_id == item.asset_id:
        return (1000, item.fps, len(item.tags))
    if req.kind != item.kind:
        return (-1, 0, 0)
    req_tags = set(req.tags)
    item_tags = set(item.tags)
    overlap = len(req_tags & item_tags)
    if overlap == 0 and req.kind in _IMAGE_KINDS | _AUDIO_KINDS:
        # Generic fallback matching by same kind, e.g. any coin sprite.
        if req.asset_id.split("/")[0] == item.asset_id.split("/")[0]:
            overlap = 1
    if overlap == 0 and req.kind in {"sprite", "tile", "ui"}:
        return (-1, 0, 0)
    return (overlap * 100 + item.fps, len(item.tags), -len(item.path))


def resolve_assets(
    requirements: list[AssetRequirement],
    catalog: list[AssetItem],
) -> AssetPlan:
    """Resolve requirements against a catalog with deterministic precedence."""
    selected: dict[str, AssetItem] = {}
    missing: list[AssetRequirement] = []
    for req in sorted(requirements, key=lambda r: (-r.priority, r.asset_id)):
        best: AssetItem | None = None
        best_score: tuple[int, int, int] | None = None
        for item in catalog:
            score = _score_match(req, item)
            if score[0] < 0:
                continue
            if best_score is None or score > best_score:
                best = item
                best_score = score
        if best is None:
            missing.append(req)
            continue
        selected[req.asset_id] = best
    return AssetPlan(selected=selected, missing=missing, requirements=list(requirements), catalog_size=len(catalog))


def _dest_root_for_kind(kind: str) -> str:
    if kind in _AUDIO_KINDS:
        return f"{_ROOT_ASSET_DIR}/audio"
    if kind == "tile":
        return f"{_ROOT_ASSET_DIR}/tiles"
    if kind == "ui":
        return f"{_ROOT_ASSET_DIR}/ui"
    return f"{_ROOT_ASSET_DIR}/sprites"


def _served_rel_path(item: AssetItem) -> str:
    rel = Path(item.path)
    parts = rel.parts
    if item.kind in _AUDIO_KINDS and parts and parts[0] == "audio":
        rel = Path(*parts[1:]) if len(parts) > 1 else Path(rel.name)
    elif item.kind == "tile" and parts and parts[0] == "tiles":
        rel = Path(*parts[1:]) if len(parts) > 1 else Path(rel.name)
    elif item.kind == "ui" and parts and parts[0] == "ui":
        rel = Path(*parts[1:]) if len(parts) > 1 else Path(rel.name)
    elif item.kind in {"sprite", "spritesheet", "atlas"} and parts and parts[0] == "sprites":
        rel = Path(*parts[1:]) if len(parts) > 1 else Path(rel.name)
    return rel.as_posix()


def write_asset_plan(project_dir: str | Path, plan: AssetPlan) -> dict[str, Any]:
    """Copy selected files and emit manifests + credits."""
    root = Path(project_dir)
    assets_json: list[dict[str, Any]] = []
    audio_json: list[dict[str, Any]] = []
    credits: list[str] = ["# Credits", ""]

    for req_id, item in plan.selected.items():
        source = Path(item.source_path)
        if not source.is_file():
            continue
        served_rel = _served_rel_path(item)
        dest = root / _dest_root_for_kind(item.kind) / served_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source.resolve() != dest.resolve():
                shutil.copy2(source, dest)
        except FileNotFoundError:
            shutil.copy2(source, dest)

        record = {
            "asset_id": req_id,
            "kind": item.kind,
            "path": f"/assets/{_dest_root_for_kind(item.kind).removeprefix(_ROOT_ASSET_DIR + '/')}/{served_rel}",
            "tags": list(item.tags),
            "license": item.license,
            "credit": item.credit,
            "source_path": item.source_path,
            "fps": item.fps,
            "loop": item.loop,
            "frame_width": item.frame_width,
            "frame_height": item.frame_height,
        }
        if item.kind in _AUDIO_KINDS:
            audio_json.append(record)
        else:
            assets_json.append(record)
        line = f"- `{req_id}`: {item.license or 'unknown'}"
        if item.credit:
            line += f" - {item.credit}"
        credits.append(line)

    for req in plan.missing:
        credits.append(f"- Missing `{req.asset_id}` (`{req.kind}`)")

    assets_path = root / _ROOT_ASSET_DIR / "assets.json"
    audio_path = root / _ROOT_ASSET_DIR / "audio" / "audio.json"
    credits_path = root / "CREDITS.md"
    assets_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    assets_path.write_text(json.dumps(assets_json, indent=2) + "\n", encoding="utf-8")
    audio_path.write_text(json.dumps(audio_json, indent=2) + "\n", encoding="utf-8")
    credits_path.write_text("\n".join(credits).rstrip() + "\n", encoding="utf-8")

    return {
        "requirements": [
            {
                "asset_id": req.asset_id,
                "kind": req.kind,
                "priority": req.priority,
                "tags": list(req.tags),
                "optional": req.optional,
            }
            for req in plan.requirements
        ],
        "selected": {
            req_id: {
                "asset_id": item.asset_id,
                "kind": item.kind,
                "path": _served_rel_path(item),
                "tags": list(item.tags),
                "license": item.license,
                "credit": item.credit,
                "source_path": item.source_path,
                "fps": item.fps,
                "loop": item.loop,
                "frame_width": item.frame_width,
                "frame_height": item.frame_height,
            }
            for req_id, item in plan.selected.items()
        },
        "missing": [
            {
                "asset_id": req.asset_id,
                "kind": req.kind,
                "priority": req.priority,
                "tags": list(req.tags),
                "optional": req.optional,
            }
            for req in plan.missing
        ],
        "assets_path": str(assets_path),
        "audio_path": str(audio_path),
        "credits_path": str(credits_path),
    }


def build_asset_foundry(
    project_dir: str | Path,
    brief: str,
    *,
    art_plan: ArtPlan | None = None,
    game_design: GameDesign | None = None,
) -> dict[str, Any]:
    """Convenience wrapper for runner integration."""
    effective_art = art_plan or direct_art(brief)
    effective_design = game_design or design_game(brief)
    catalog, sources = collect_project_catalog(project_dir)
    requirements = derive_asset_requirements(brief, effective_art, effective_design)
    plan = resolve_assets(requirements, catalog)
    written = write_asset_plan(project_dir, plan)
    return {
        "catalog_size": plan.catalog_size,
        "sources": sources,
        "requirements": written["requirements"],
        "selected": written["selected"],
        "missing": written["missing"],
        "assets_path": written["assets_path"],
        "audio_path": written["audio_path"],
        "credits_path": written["credits_path"],
        "art_plan": effective_art.to_dict(),
        "game_design": effective_design.to_dict(),
    }


def check_asset_outputs(project_dir: str | Path) -> dict[str, list[str]]:
    """Verify emitted asset/audio manifests reference real files and credits."""
    root = Path(project_dir)
    missing_paths: list[str] = []
    missing_credits: list[str] = []

    def _check_records(records: list[dict[str, Any]]) -> None:
        for rec in records:
            path = str(rec.get("path") or "").strip()
            if not path:
                continue
            rel = path.lstrip("/")
            target = root / rel
            if not target.exists():
                missing_paths.append(path)
            license_name = str(rec.get("license") or "").strip().lower()
            if license_name and license_name not in _CC0_LICENSES and not str(rec.get("credit") or "").strip():
                missing_credits.append(str(rec.get("asset_id") or path))

    assets_path = root / _ROOT_ASSET_DIR / "assets.json"
    audio_path = root / _ROOT_ASSET_DIR / "audio" / "audio.json"
    try:
        if assets_path.is_file():
            _check_records(json.loads(assets_path.read_text(encoding="utf-8") or "[]"))
        if audio_path.is_file():
            _check_records(json.loads(audio_path.read_text(encoding="utf-8") or "[]"))
    except Exception as exc:  # noqa: BLE001 - QA never breaks a build
        log.warning("asset_foundry.check_failed", error=str(exc)[:160])
    return {"missing_paths": missing_paths, "missing_credits": missing_credits}
