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
from skyn3t.agents.art_director import ArtPlan, direct_art
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
    (("commercial hvac", "rooftop unit", "rooftop hvac"), "commercial HVAC rooftop units on an office building"),
    (("roofing", "roof"), "roofer installing shingles on a roof"),
    (("landscaping", "lawn care"), "professionally landscaped residential yard"),
)
_DOMAIN_SUBJECTS = (
    (("golf", "golfing", "country club", "clubhouse", "tee time", "tee-time"),
     (
         "sunlit golf course fairway and green",
         "golfer putting on a manicured green",
         "welcoming golf clubhouse exterior",
     )),
    (("restaurant", "cafe", "coffee shop", "bakery"),
     ("welcoming restaurant dining room", "fresh prepared signature dish")),
    (("fitness", "gym", "yoga", "trainer"),
     ("bright fitness studio with modern equipment", "personal trainer coaching a client")),
    (("salon", "spa", "barber"),
     ("modern salon interior", "spa treatment room with soft lighting")),
)
_SUBJECT_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "beside",
    "by",
    "for",
    "home",
    "house",
    "modern",
    "of",
    "on",
    "the",
    "with",
}


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
        subs: list[str] = []
        for keys, subjects in _DOMAIN_SUBJECTS:
            if any(k in low for k in keys):
                subs.extend(subject for subject in subjects if subject not in subs)
        for keys, subject in _SERVICE_SUBJECTS:
            if any(k in low for k in keys) and subject not in subs:
                subs.append(subject)
        return subs[:limit]
    # Nothing nameable. Do NOT invent coloring-book defaults — a brief that
    # merely *mentions* images (e.g. a tool that takes an image as INPUT) is not
    # an app that ships decorative pictures. Generating cat/dog/tree/flower here
    # only litters unrelated builds. No subject -> generate nothing.
    return []


def _subject_tokens(value: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9]+", (value or "").lower())
        if len(tok) > 2 and tok not in _SUBJECT_STOPWORDS
    }


def asset_subject_relevant(brief: str, subject: str) -> bool:
    """Whether an asset subject belongs to this brief's expected image set.

    This is intentionally conservative: generated assets are strong visual
    prompt hints. If the brief implies a domain-specific set (golf, restaurant,
    HVAC, etc.), drop unrelated subjects even when stale files still exist in
    the worktree.
    """
    expected = _extract_subjects(brief, MAX_ASSETS)
    if not expected:
        return not _wants_images(brief)
    subject_tokens = _subject_tokens(subject)
    if not subject_tokens:
        return False
    for candidate in expected:
        candidate_tokens = _subject_tokens(candidate)
        if subject_tokens & candidate_tokens:
            return True
    return False


def filter_assets_for_brief(
    brief: str, assets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep only generated assets whose subject is relevant to this brief."""
    if not isinstance(assets, list):
        return []
    return [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and asset_subject_relevant(brief, str(asset.get("subject") or ""))
    ]


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
# game-styled. The adapter (ReplicateClient._input_for) sends its rd params
# (style='game_asset' + 256x256, cheap tier ~$0.017/image) but NOT remove_bg —
# that flag currently fails server-side; we strip the background ourselves below.
_SPRITE_ROLE_MODEL = "retro-diffusion/rd-fast"

# CASCADE: the preferred pixel model first, then a working OFFICIAL fallback so a
# single-model outage degrades to the next model instead of all the way to
# primitives. flux-schnell is opaque/generic but available + cheap; the bg keyer
# below makes its (and rd-fast's) solid background transparent.
_SPRITE_ROLE_MODELS = (_SPRITE_ROLE_MODEL, "black-forest-labs/flux-schnell")


def _key_bg_to_alpha(png_bytes: bytes) -> bytes:
    """Flood-fill the border-connected background of a sprite to transparent.

    The sprite models return a SOLID background (rd-fast can't use its broken
    remove_bg; flux paints one), which looks wrong dropped onto a scrolling scene.
    This makes only the background touching the image border transparent, via a
    flood-fill from the four corners — so interior light pixels (a white star or
    cockpit *inside* the plane) are PRESERVED, unlike a naive global colour key.

    Only keys when the four corners are a near-uniform colour (a real backdrop); a
    busy/non-uniform frame is left untouched. Returns the input bytes unchanged on
    any problem (not an image, busy bg). Never raises."""
    try:
        import io as _io

        from PIL import Image, ImageDraw

        im = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        w, h = im.size
        if w < 4 or h < 4:
            return png_bytes
        corners = [im.getpixel((0, 0)), im.getpixel((w - 1, 0)),
                   im.getpixel((0, h - 1)), im.getpixel((w - 1, h - 1))]

        def _close(a, b, t=24):
            return all(abs(a[i] - b[i]) <= t for i in range(3))

        if not all(_close(corners[0], c) for c in corners[1:]):
            return png_bytes  # busy backdrop -> don't risk mangling it
        sentinel = (1, 254, 1)  # unlikely to occur naturally; marks filled bg
        for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            ImageDraw.floodfill(im, seed, sentinel, thresh=40)
        rgba = im.convert("RGBA")
        px = rgba.load()
        for y in range(h):
            for x in range(w):
                r, g, b, _a = px[x, y]
                if (r, g, b) == sentinel:
                    px[x, y] = (0, 0, 0, 0)
        out = _io.BytesIO()
        rgba.save(out, "PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - keying is best-effort; never break a build
        return png_bytes


async def _generate_role_image(cli: Any, prompt: str) -> tuple[bytes | None, str | None]:
    """Try each sprite model in order; return (bytes, model) for the first that yields
    an image, with the solid background keyed to transparent, else (None, None).
    Resilient to a single model outage. Never raises."""
    for mdl in _SPRITE_ROLE_MODELS:
        try:
            images = await cli.generate_images(prompt, n=1, model=mdl)
        except Exception:  # noqa: BLE001 - try the next model
            images = []
        if images and images[0]:
            return _key_bg_to_alpha(images[0]), mdl
    return None, None


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
    art_plan: ArtPlan | None = None,
) -> dict[str, Any]:
    """Generate one themed sprite per game ROLE at build time, written to
    ``public/assets/sprites/{role}.png`` with a ``role_map`` manifest.

    ``art_plan`` lets the runner thread a pre-computed plan (e.g. an LLM-tailored
    one) so this generator and the codegen directive use the SAME role set; when
    omitted it derives the plan from the brief (``direct_art``).

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
    # Use the threaded plan when the runner computed one (LLM-tailored), else derive
    # it from the brief. The codegen directive plans from the SAME source, so the two
    # structurally agree on which roles get a sprite file.
    plan = art_plan or direct_art(brief)
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
            data, _mdl = await _generate_role_image(cli, rp.prompt)
            return role, data
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
