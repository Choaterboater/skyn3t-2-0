"""Asset generation — turn an image-implying brief into real generated images.

When a Replicate token is configured AND ``asset_gen`` is on, this step inspects
the brief for image subjects (e.g. a kids coloring app -> animals), generates a
small, capped set of coloring-page line-art images, writes them into the
delivered project under ``assets/`` with an ``assets.json`` manifest (subject ->
file), and returns a manifest dict the runner records + the code/agentic prompt
references so the generated app uses real art.

Everything degrades: no token / asset_gen off / non-image brief / generation
failure -> a no-op that writes nothing and never raises. It NEVER blocks or
fails a build (design rule #6).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import structlog

from skyn3t.adapters.replicate import (
    ReplicateClient,
    asset_model,
    asset_prompt,
    select_asset_style,
)
from skyn3t.config.settings import Settings

log = structlog.get_logger(__name__)

# Hard cap on images per build — bounded + cost-aware (each is a paid prediction).
MAX_ASSETS = 4

# Words that imply the app SHOWS pictures / art, so generating real assets pays
# off. Absent these, we skip (no point spending predictions on a calculator).
_IMAGE_SIGNALS = (
    "coloring", "colouring", "color-in", "line-art", "line art", "drawing",
    "drawings", "picture", "pictures", "image", "images", "illustration",
    "illustrated", "art", "artwork", "icon", "icons", "sticker", "stickers",
    "gallery", "sprite", "sprites", "cartoon", "clip art", "clipart",
    # photo + brand signals so photo/logo apps trigger asset-gen and route to
    # their proper model (flux-1.1-pro / recraft SVG) instead of being skipped.
    "photo", "photos", "photograph", "photography", "logo", "logos", "brand",
    "branding", "avatar", "avatars", "banner", "thumbnail",
)

# A pragmatic subject vocabulary. We pull subjects the brief actually names; this
# fallback list is used for kid/coloring briefs that imply "animals" without
# naming specific ones, so the app ships with usable pages out of the box.
_ANIMALS = ("cat", "dog", "elephant", "lion", "rabbit", "fox", "owl", "fish",
            "bear", "frog", "horse", "penguin", "turtle", "butterfly")
_NATURE = ("tree", "flower", "sun", "star", "cloud", "rainbow", "house", "car")


def _wants_images(brief: str) -> bool:
    low = (brief or "").lower()
    return any(sig in low for sig in _IMAGE_SIGNALS)


def _extract_subjects(brief: str, limit: int) -> list[str]:
    """Pull image subjects from the brief; fall back to a sensible set.

    Names the brief mentions (animals/nature nouns) win; for a coloring/kids
    brief that only implies "animals", we seed a friendly default set so the app
    ships with real pages instead of nothing.
    """
    low = (brief or "").lower()
    found: list[str] = []
    for word in (*_ANIMALS, *_NATURE):
        if re.search(rf"\b{re.escape(word)}s?\b", low) and word not in found:
            found.append(word)
    if found:
        return found[:limit]
    # Implied subjects: kids/animal/coloring brief with no explicit nouns.
    if any(w in low for w in ("animal", "zoo", "kid", "child", "toddler",
                              "coloring", "colouring")):
        return list(_ANIMALS[:limit])
    # Generic image brief: a small mixed default so something is generated.
    return list((*_ANIMALS[:2], *_NATURE[:2]))[:limit]


def _ext_for(data: bytes) -> str:
    """Guess an image extension from magic bytes (png/jpg/webp); default png."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:256].lstrip().lower()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return "svg"  # recraft vector model returns SVG markup
    return "png"


def asset_gen_enabled(settings: Settings) -> bool:
    """The step runs only when a token is present AND asset_gen is opted in."""
    return bool(getattr(settings, "replicate_available", False)) and bool(
        getattr(settings, "asset_gen", False)
    )


async def generate_assets(
    project_dir: str,
    brief: str,
    *,
    settings: Settings,
    client: ReplicateClient | None = None,
    max_assets: int = MAX_ASSETS,
) -> dict[str, Any]:
    """Generate + write coloring-page assets for ``brief`` into ``project_dir``.

    Returns a manifest dict: ``{"generated": int, "assets": [{subject, file}],
    "skipped": bool, "reason": str}``. A no-op (skipped) when the token/flag is
    off, the brief doesn't imply images, or generation yields nothing. Never
    raises — a failure logs + returns ``generated=0``.
    """
    if not asset_gen_enabled(settings):
        return {"generated": 0, "skipped": True, "reason": "disabled", "assets": []}
    if not _wants_images(brief):
        return {"generated": 0, "skipped": True, "reason": "no_image_brief", "assets": []}

    cli = client or ReplicateClient(settings)
    if not cli.available:
        return {"generated": 0, "skipped": True, "reason": "no_token", "assets": []}

    cap = max(1, min(int(max_assets), MAX_ASSETS))
    subjects = _extract_subjects(brief, cap)
    # Route to the model + prompt that fits THIS app (coloring->flux-schnell,
    # logo->recraft SVG, photo->flux-1.1-pro, etc.) instead of line-art for all.
    style = select_asset_style(brief)
    model = asset_model(style)
    assets_dir = Path(project_dir) / "assets"
    written: list[dict[str, str]] = []
    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("assets.mkdir_failed", error=str(exc)[:160])
        return {"generated": 0, "skipped": True, "reason": "mkdir_failed", "assets": []}

    # Generate all subjects CONCURRENTLY (each is an independent prediction) so a
    # multi-subject set isn't serialized at ~90s/subject on the build hot path.
    async def _gen(subject: str) -> tuple[str, bytes | None]:
        try:
            images = await cli.generate_images(
                asset_prompt(style, subject), n=1, model=model)
            return subject, (images[0] if images else None)
        except Exception as exc:  # noqa: BLE001 - never break the build over a subject
            log.warning("assets.subject_failed", subject=subject, error=str(exc)[:160])
            return subject, None

    results = await asyncio.gather(*(_gen(s) for s in subjects))
    for subject, data in results:
        if not data:
            continue
        fname = f"{re.sub(r'[^a-z0-9]+', '-', subject.lower()).strip('-') or 'asset'}.{_ext_for(data)}"
        try:
            (assets_dir / fname).write_bytes(data)
        except OSError as exc:
            log.warning("assets.write_failed", file=fname, error=str(exc)[:160])
            continue
        written.append({"subject": subject, "file": f"assets/{fname}"})

    manifest = {
        "generated": len(written),
        "skipped": len(written) == 0,
        "reason": "" if written else "no_images",
        "style": style,
        "model": model,
        "assets": written,
    }
    if written:
        try:
            (assets_dir / "assets.json").write_text(
                json.dumps(manifest["assets"], indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            log.warning("assets.manifest_write_failed", error=str(exc)[:160])
    log.info("assets.generated", count=len(written), subjects=[a["subject"] for a in written])
    return manifest
