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
    DEFAULT_MODEL,
    ReplicateClient,
    asset_model,
    asset_prompt,
    select_asset_style,
)
from skyn3t.agents.art_director import direct_art
from skyn3t.config.settings import Settings

log = structlog.get_logger(__name__)

# Hard cap on images per build — bounded + cost-aware (each is a paid prediction).
MAX_ASSETS = 8  # rich multi-service sites need a hero + one per service (was 4 -> last services fell back to icon tiles)
# Web frameworks that serve static files from public/ (vs ./assets/ for static html).
_WEB_STACKS = {"nextjs", "next", "react", "vite", "remix", "astro", "svelte",
               "sveltekit", "vue", "nuxt", "solid", "node", "phaser"}

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

# Business / marketing / home-services briefs imply real PHOTOS (a hero banner +
# per-service shots), not coloring pages. Without this, a company site shipped with
# zero imagery ("no_subjects"). Map the services a brief names to concrete subjects.
_BUSINESS_SIGNALS = ("service", "services", "company", "business", "marketing",
                     "website", " site", "contractor", "repair", "installation",
                     "commercial", "residential", "agency", "clinic", "shop", "store")
_SERVICE_SUBJECTS = (
    (("air conditioning", "a/c", "hvac", "cooling", " ac "), "air conditioning condenser unit beside a house"),
    (("heating", "furnace", "heat pump", "boiler"), "home furnace heating system"),
    (("plumbing", "plumber", "drain", " pipe"), "plumber repairing a pipe under a sink"),
    (("electrical", "electrician", "wiring", " panel"), "electrician working on a home electrical panel"),
    (("generator",), "home standby backup generator"),
    (("commercial", "office", "business", "retail"), "commercial HVAC rooftop units on an office building"),
    (("roofing", "roof"), "roofer installing shingles on a roof"),
    (("landscaping", "lawn care"), "professionally landscaped residential yard"),
)


def _wants_images(brief: str) -> bool:
    low = (brief or "").lower()
    return any(sig in low for sig in _IMAGE_SIGNALS) or any(w in low for w in _BUSINESS_SIGNALS)


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
    # Business / marketing / home-services site: ship a hero + the per-service
    # photos the brief names, so a company site has real imagery instead of none.
    if any(w in low for w in _BUSINESS_SIGNALS):
        subs = ["uniformed HVAC technician servicing an outdoor air conditioning unit at a home"]
        for keys, subject in _SERVICE_SUBJECTS:
            if any(k in low for k in keys) and subject not in subs:
                subs.append(subject)
        return subs[:limit]
    # Nothing nameable. Do NOT invent coloring-book defaults — a brief that
    # merely *mentions* images (e.g. a tool that takes an image as INPUT) is not
    # an app that ships decorative pictures. Generating cat/dog/tree/flower here
    # only litters unrelated builds. No subject -> generate nothing.
    return []


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
    stack: str = "",
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
    if not subjects:
        # Image-ish brief but nothing concrete to draw — skip before spending any
        # predictions or creating an assets/ dir. Clean degrade, never a crash.
        return {"generated": 0, "skipped": True, "reason": "no_subjects", "assets": []}
    # Route to the model + prompt that fits THIS app (coloring->flux-schnell,
    # logo->recraft SVG, photo->flux-1.1-pro, etc.) instead of line-art for all.
    style = select_asset_style(brief)
    model = asset_model(style)
    # Next.js/Vite/CRA serve static files from public/ — writing to ./assets/ makes
    # the code's `/assets/...` refs 404 (they did on the first agentic build). Detect
    # a JS framework (package.json) and place + reference under public/ so generated
    # images actually load; keep ./assets/ for static/python stacks.
    proj = Path(project_dir)
    # Web frameworks serve static files from public/. Assets run BEFORE codegen, so
    # package.json doesn't exist yet — rely on the known stack first, package.json
    # second. Otherwise images land in ./assets/ and the code's /assets/... refs 404.
    _web = (stack or "").lower() in _WEB_STACKS or (proj / "package.json").is_file()
    assets_dir = (proj / "public" / "assets") if _web else (proj / "assets")
    _url_base = "/assets" if _web else "assets"
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
        written.append({"subject": subject, "file": f"{_url_base}/{fname}"})

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


# The role-sprite model. retro-diffusion/rd-fast is an OFFICIAL Replicate model
# (live-verified is_official=True — works on the official-models endpoint the
# adapter uses, unlike community models that 404 there) that is PIXEL-NATIVE and
# game-styled. The adapter (ReplicateClient._input_for) sends its rd-specific
# params — style='game_asset' + remove_bg=True (transparent PNG in one call) +
# 256x256 (cheap tier, ~$0.017/image). A strict upgrade over flux-schnell, which
# produced generic OPAQUE images. (DEFAULT_MODEL/flux-schnell remains the fallback
# for the subject-image flows above.)
_SPRITE_ROLE_MODEL = "retro-diffusion/rd-fast"


def _role_art_source(settings: Settings) -> str:
    """Resolve where role sprites come from: ``replicate``, ``offline``, or
    ``disabled`` — from ``game_art_enabled`` + ``game_art_source``. ``auto`` uses
    replicate when a token is configured, else the free offline floor."""
    if not bool(getattr(settings, "game_art_enabled", True)):
        return "disabled"
    src = str(getattr(settings, "game_art_source", "auto")).lower()
    if src in ("replicate", "offline"):
        return src
    return "replicate" if bool(getattr(settings, "replicate_available", False)) else "offline"


async def generate_role_sprites(
    project_dir: str,
    brief: str,
    *,
    settings: Settings,
    client: ReplicateClient | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    """Generate one themed sprite per game ROLE at build time, written to
    ``public/assets/sprites/{role}.png`` with a ``role_map`` manifest.

    Gated by ``game_art_enabled`` + ``game_art_source`` (``offline``/``disabled``
    skip cleanly — the scaffold renders colored primitives). Never raises: a
    per-role generation failure OMITS that role (primitive fallback), so the result
    is never a gap and never crashes the build.
    """
    decision = _role_art_source(settings)
    if decision != "replicate":
        return {"generated": 0, "skipped": True, "reason": decision,
                "source": "offline", "role_map": {}}

    cli = client or ReplicateClient(settings)
    if not cli.available:
        return {"generated": 0, "skipped": True, "reason": "no_token",
                "source": "offline", "role_map": {}}

    # The art director decides, per genre, which roles are SPRITES vs styled
    # PRIMITIVES — we generate ONLY the sprite roles, so a geometric game (all
    # primitives) spends nothing and a themed game gets exactly its game-aware
    # sprites. The codegen directive reads the SAME deterministic plan, so the two
    # agree on role keys with no threading.
    # Deterministic on the brief alone (no seed) — the codegen directive plans from
    # the SAME call, so the two structurally agree on which roles get a sprite file.
    plan = direct_art(brief)
    sprite_roles = plan.sprite_roles()
    sprites_dir = Path(project_dir) / "public" / "assets" / "sprites"
    try:
        sprites_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("role_sprites.mkdir_failed", error=str(exc)[:160])
        return {"generated": 0, "skipped": True, "reason": "mkdir_failed",
                "source": "offline", "role_map": {},
                "genre": plan.genre, "palette": list(plan.palette)}

    # One independent prediction per SPRITE role, run CONCURRENTLY (the build hot
    # path). Primitive roles are never generated — they render from the palette.
    async def _gen(role: str, rp) -> tuple[str, bytes | None]:
        try:
            images = await cli.generate_images(rp.prompt, n=1, model=_SPRITE_ROLE_MODEL)
            return role, (images[0] if images else None)
        except Exception as exc:  # noqa: BLE001 - never break the build over one role
            log.warning("role_sprites.role_failed", role=role, error=str(exc)[:160])
            return role, None

    results = await asyncio.gather(*(_gen(role, rp) for role, rp in sprite_roles.items()))
    role_map: dict[str, str] = {}
    for role, data in results:
        if not data:
            continue  # omitted -> the scaffold renders a colored primitive for this role
        # Force .png: the scaffold's preload() loads {role}.png, and the sprite
        # model returns PNG. A failed/odd format simply 404s -> primitive fallback.
        fname = f"{role}.png"
        try:
            (sprites_dir / fname).write_bytes(data)
        except OSError as exc:
            log.warning("role_sprites.write_failed", role=role, error=str(exc)[:160])
            continue
        role_map[role] = f"/assets/sprites/{fname}"

    # Accurate reason — a geometric genre with NO sprite roles is "all_primitive"
    # (intended, $0), distinct from a real generation miss ("no_images").
    if not sprite_roles:
        reason = "all_primitive"
    elif not role_map:
        reason = "no_images"
    else:
        reason = ""
    manifest = {
        "generated": len(role_map),
        "skipped": len(role_map) == 0,
        "reason": reason,
        "source": "replicate",
        "model": _SPRITE_ROLE_MODEL,
        "genre": plan.genre,
        "palette": list(plan.palette),
        "role_map": role_map,
    }
    if role_map:
        try:
            (sprites_dir / "assets.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("role_sprites.manifest_failed", error=str(exc)[:160])
    log.info("role_sprites.generated", count=len(role_map), roles=list(role_map))
    return manifest
