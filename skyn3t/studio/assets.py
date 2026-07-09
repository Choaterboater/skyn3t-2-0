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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from weakref import WeakKeyDictionary

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
# Provider failures should not fan out into a pile of paid/stalled predictions.
ASSET_PROVIDER_BATCH_SIZE = 2
ASSET_PROVIDER_FAILURE_LIMIT = 2
ASSET_PROVIDER_MAX_CONCURRENCY = 1
ASSET_PROVIDER_MAX_ATTEMPTS = 2
ASSET_PROVIDER_MIN_INTERVAL = 1.0
ASSET_PROVIDER_RETRY_DELAY = 1.0
# Web frameworks that serve static files from public/ (vs ./assets/ for static html).
_WEB_STACKS = {"nextjs", "next", "react", "react_vite", "react_ts", "vite",
               "remix", "astro", "svelte", "sveltekit", "vue", "nuxt",
               "solid", "node", "phaser"}

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
_SERVICE_SUBJECTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("air conditioning", "a/c", "hvac", "cooling", " ac "), "air conditioning condenser unit beside a house"),
    (("heating", "furnace", "heat pump", "boiler"), "home furnace heating system"),
    (("plumbing", "plumber", "drain", " pipe"), "plumber repairing a pipe under a sink"),
    (("electrical", "electrician", "wiring", " panel"), "electrician working on a home electrical panel"),
    (("generator",), "home standby backup generator"),
    (("commercial hvac", "rooftop unit", "rooftop hvac"), "commercial HVAC rooftop units on an office building"),
    (("roofing", "roof"), "roofer installing shingles on a roof"),
    (("landscaping", "lawn care"), "professionally landscaped residential yard"),
)
_DOMAIN_SUBJECTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
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


class _ProviderAdmission:
    def __init__(self) -> None:
        self.semaphore = asyncio.Semaphore(ASSET_PROVIDER_MAX_CONCURRENCY)
        self.users = 0


_PROVIDER_ADMISSIONS: dict[asyncio.AbstractEventLoop, _ProviderAdmission] = {}
_PROVIDER_START_TIMES: WeakKeyDictionary[asyncio.AbstractEventLoop, float] = (
    WeakKeyDictionary()
)


@asynccontextmanager
async def _provider_slot() -> AsyncIterator[None]:
    """Share Replicate capacity across builds without binding later event loops."""
    loop = asyncio.get_running_loop()
    admission = _PROVIDER_ADMISSIONS.get(loop)
    if admission is None:
        admission = _ProviderAdmission()
        _PROVIDER_ADMISSIONS[loop] = admission
    admission.users += 1
    try:
        async with admission.semaphore:
            last_started = _PROVIDER_START_TIMES.get(loop, 0.0)
            wait_for = ASSET_PROVIDER_MIN_INTERVAL - (loop.time() - last_started)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            _PROVIDER_START_TIMES[loop] = loop.time()
            yield
    finally:
        admission.users -= 1
        if admission.users == 0 and _PROVIDER_ADMISSIONS.get(loop) is admission:
            _PROVIDER_ADMISSIONS.pop(loop, None)


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

    # Generate in tiny batches. Capacity is shared across simultaneous builds so
    # independent fan-out cannot overload the provider. Only failed subjects retry;
    # successful predictions stay on the single-attempt path.
    async def _gen(subject: str) -> tuple[str, bytes | None]:
        prompt = asset_prompt(style, subject)
        for attempt in range(1, ASSET_PROVIDER_MAX_ATTEMPTS + 1):
            error = "empty result"
            try:
                async with _provider_slot():
                    images = await cli.generate_images(prompt, n=1, model=model)
                data = images[0] if images else None
                if data:
                    return subject, data
            except Exception as exc:  # noqa: BLE001 - never break the build over a subject
                error = str(exc)[:160]
            if attempt < ASSET_PROVIDER_MAX_ATTEMPTS:
                log.warning(
                    "assets.subject_retry",
                    subject=subject,
                    attempt=attempt,
                    error=error,
                )
                await asyncio.sleep(ASSET_PROVIDER_RETRY_DELAY * attempt)
            else:
                log.warning(
                    "assets.subject_failed",
                    subject=subject,
                    attempts=attempt,
                    error=error,
                )
        return subject, None

    results: list[tuple[str, bytes | None]] = []
    failures = 0
    batch_size = max(1, ASSET_PROVIDER_BATCH_SIZE)
    for i in range(0, len(subjects), batch_size):
        batch = subjects[i:i + batch_size]
        batch_results = await asyncio.gather(*(_gen(s) for s in batch))
        results.extend(batch_results)
        batch_failures = sum(1 for _, data in batch_results if not data)
        failures += batch_failures
        if failures >= ASSET_PROVIDER_FAILURE_LIMIT:
            log.warning(
                "assets.provider_circuit_open",
                failures=failures,
                attempted=len(results),
                remaining=max(0, len(subjects) - len(results)),
            )
            break
        if i == 0 and batch_failures == 0:
            rest = subjects[i + batch_size:]
            if rest:
                results.extend(await asyncio.gather(*(_gen(s) for s in rest)))
            break
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


def _brief_theme(brief: str) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    low = (brief or "").lower()
    if any(w in low for w in ("golf", "course", "clubhouse", "tee")):
        return "golf", (29, 107, 77), (240, 200, 84)
    if any(w in low for w in ("restaurant", "cafe", "bakery", "coffee")):
        return "hospitality", (132, 61, 42), (244, 164, 96)
    if any(w in low for w in ("fitness", "gym", "trainer", "yoga")):
        return "fitness", (36, 72, 110), (96, 190, 174)
    if any(w in low for w in ("portfolio", "studio", "photography", "gallery")):
        return "creative", (83, 55, 109), (236, 138, 92)
    return "web", (45, 74, 102), (95, 179, 168)


def _draw_web_asset_png(
    brief: str,
    *,
    size: tuple[int, int],
    label: str,
    compact: bool = False,
) -> bytes:
    import io

    from PIL import Image, ImageDraw, ImageFont

    theme, base, accent = _brief_theme(brief)
    width, height = size
    im = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(im)
    for y in range(height):
        t = y / max(1, height - 1)
        shade = tuple(int(base[i] * (1 - t) + max(0, base[i] - 34) * t) for i in range(3))
        draw.line((0, y, width, y), fill=shade)

    if compact:
        margin = max(4, width // 8)
        draw.rounded_rectangle(
            (margin, margin, width - margin, height - margin),
            radius=max(4, width // 7),
            fill=accent,
        )
    else:
        horizon = int(height * 0.63)
        draw.rectangle((0, horizon, width, height), fill=tuple(max(0, c - 38) for c in base))
        for i in range(7):
            x = int(width * (0.1 + i * 0.14))
            r = int(width * (0.05 + (i % 3) * 0.012))
            draw.ellipse((x - r, horizon - r, x + r, horizon + r), fill=accent)
        draw.rounded_rectangle(
            (int(width * 0.08), int(height * 0.16), int(width * 0.58), int(height * 0.32)),
            radius=max(8, height // 24),
            fill=(255, 255, 255),
        )

    font = ImageFont.load_default()
    text = label if compact else f"{theme.upper()} / {label}"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_x = (width - (bbox[2] - bbox[0])) / 2 if compact else int(width * 0.11)
    text_y = (height - (bbox[3] - bbox[1])) / 2 if compact else int(height * 0.21)
    draw.text((text_x, text_y), text, font=font, fill=(16, 24, 39))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def generate_offline_web_assets(project_dir: str | Path, brief: str, *, stack: str = "") -> dict[str, Any]:
    """Write a $0 web asset floor: hero, Open Graph image, and favicon.

    These are deterministic local PNGs, not stock art. They give web codegen real
    served paths to use even when paid image generation is disabled.
    """
    root = Path(project_dir)
    # Framework apps serve public/assets at /assets. Plain static HTML is served
    # from the project root, so its /assets path must be root/assets instead.
    web_framework = (stack or "").lower() in _WEB_STACKS or (root / "package.json").is_file()
    assets_dir = root / "public" / "assets" if web_framework else root / "assets"
    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("web_assets.mkdir_failed", error=str(exc)[:160])
        return {"type": "web", "source": "offline", "generated": 0, "skipped": True,
                "reason": "mkdir_failed", "selected": {}, "requirements": []}

    specs = {
        "web/hero": ("hero.png", (1440, 720), "HERO", False),
        "web/og": ("og.png", (1200, 630), "OPEN GRAPH", False),
        "web/favicon": ("favicon.png", (96, 96), "SKY", True),
    }
    selected: dict[str, dict[str, Any]] = {}
    for asset_id, (filename, size, label, compact) in specs.items():
        target = assets_dir / filename
        try:
            target.write_bytes(
                _draw_web_asset_png(brief, size=size, label=label, compact=compact)
            )
        except Exception as exc:  # noqa: BLE001 - web assets must never block delivery
            log.warning("web_assets.write_failed", file=filename, error=str(exc)[:160])
            continue
        selected[asset_id] = {
            "asset_id": asset_id,
            "kind": "image",
            "path": f"/assets/{filename}",
            "tags": ["web", filename.rsplit(".", 1)[0]],
            "license": "CC0-1.0",
            "credit": "Generated locally by SkyN3t offline web asset floor",
            "source": "offline",
        }

    requirements = [
        {"asset_id": asset_id, "kind": "image", "priority": priority,
         "tags": ["web"], "optional": False}
        for priority, asset_id in enumerate(specs, start=1)
    ]
    manifest = {
        "type": "web",
        "source": "offline",
        "stack": stack,
        "generated": len(selected),
        "skipped": len(selected) == 0,
        "reason": "" if selected else "write_failed",
        "requirements": requirements,
        "selected": selected,
        "assets_path": str(assets_dir / "web-assets.json"),
        "credits_path": str(root / "CREDITS.md"),
    }
    try:
        (assets_dir / "web-assets.json").write_text(
            json.dumps(list(selected.values()), indent=2) + "\n", encoding="utf-8"
        )
        credits = root / "CREDITS.md"
        existing = credits.read_text(encoding="utf-8") if credits.exists() else "# Credits\n"
        if "SkyN3t offline web asset floor" not in existing:
            credits.write_text(
                existing.rstrip()
                + "\n- `web/*`: CC0-1.0 - SkyN3t offline web asset floor\n",
                encoding="utf-8",
            )
    except OSError as exc:
        log.warning("web_assets.manifest_failed", error=str(exc)[:160])
    return manifest


def apply_web_asset_foundry(project_dir: str | Path, foundry: dict[str, Any]) -> dict[str, Any]:
    """Wire generated web assets into plain HTML entrypoints.

    Codegen receives an instruction to use the assets, but models can ignore it.
    This deterministic pass is deliberately narrow: it only edits existing HTML
    files, adds favicon/Open Graph metadata to <head>, and injects a visible hero
    image after <body> when the page has no reference to the generated hero.
    """
    if not isinstance(foundry, dict) or foundry.get("type") != "web":
        return {"changed": False, "reason": "not_web_foundry", "files": []}
    selected = foundry.get("selected")
    if not isinstance(selected, dict):
        return {"changed": False, "reason": "no_selected_assets", "files": []}
    paths = {
        key: str(value.get("path") or "")
        for key, value in selected.items()
        if isinstance(value, dict) and value.get("path")
    }
    hero = paths.get("web/hero", "")
    og = paths.get("web/og", "")
    favicon = paths.get("web/favicon", "")
    if not any((hero, og, favicon)):
        return {"changed": False, "reason": "no_paths", "files": []}

    root = Path(project_dir)
    candidates = []
    for rel in ("index.html", "public/index.html"):
        p = root / rel
        if p.is_file():
            candidates.append(p)
    changed_files: list[str] = []
    hero_applied = favicon_applied = og_applied = False

    style = (
        '<style id="skyn3t-web-assets">'
        ".skyn3t-asset-hero{margin:0;overflow:hidden;background:#111827}"
        ".skyn3t-asset-hero img{display:block;width:100%;height:min(34vh,420px);"
        "object-fit:cover}"
        "</style>"
    )
    for html_path in candidates:
        try:
            html = html_path.read_text(encoding="utf-8")
        except OSError:
            continue
        original = html
        if favicon and favicon not in html and re.search(r"</head\s*>", html, flags=re.I):
            html = re.sub(
                r"</head\s*>",
                f'  <link rel="icon" href="{favicon}">\n</head>',
                html,
                count=1,
                flags=re.I,
            )
            favicon_applied = True
        if og and og not in html and re.search(r"</head\s*>", html, flags=re.I):
            html = re.sub(
                r"</head\s*>",
                f'  <meta property="og:image" content="{og}">\n</head>',
                html,
                count=1,
                flags=re.I,
            )
            og_applied = True
        if hero and hero not in html and re.search(r"<body(?:\\s[^>]*)?>", html, flags=re.I):
            if "skyn3t-web-assets" not in html and re.search(r"</head\s*>", html, flags=re.I):
                html = re.sub(r"</head\s*>", f"  {style}\n</head>", html, count=1, flags=re.I)
            hero_markup = (
                f'\n<section class="skyn3t-asset-hero" aria-label="Generated visual">'
                f'<img src="{hero}" alt="" loading="eager"></section>'
            )
            html = re.sub(
                r"(<body(?:\s[^>]*)?>)",
                r"\1" + hero_markup,
                html,
                count=1,
                flags=re.I,
            )
            hero_applied = True
        if html != original:
            try:
                html_path.write_text(html, encoding="utf-8")
            except OSError:
                continue
            changed_files.append(str(html_path.relative_to(root)))

    return {
        "changed": bool(changed_files),
        "files": changed_files,
        "hero_applied": hero_applied,
        "favicon_applied": favicon_applied,
        "og_applied": og_applied,
    }


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
        if px is None:
            return png_bytes
        for y in range(h):
            for x in range(w):
                r, g, b, _a = cast(tuple[int, int, int, int], px[x, y])
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
    """Resolve where role sprites come from: ``kenney``, ``replicate``,
    ``offline``, or ``disabled`` — from ``game_art_enabled`` + ``game_art_source``.
    ``auto`` uses an installed Kenney pack first, then replicate when a token is
    configured, else the free offline floor."""
    if not bool(getattr(settings, "game_art_enabled", True)):
        return "disabled"
    src = str(getattr(settings, "game_art_source", "auto")).lower()
    if src in ("kenney", "replicate", "offline"):
        return src
    if src == "auto":
        try:
            from skyn3t.studio.kenney_assets import has_kenney_pack

            if has_kenney_pack(getattr(settings, "data_dir", "data")):
                return "kenney"
        except Exception:  # noqa: BLE001 - optional local packs must never block art fallback
            pass
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
    # The art director decides, per genre, which roles are SPRITES vs styled
    # PRIMITIVES — we generate ONLY the sprite roles, so a geometric game (all
    # primitives) spends nothing and a themed game gets exactly its game-aware
    # sprites. The codegen directive reads the SAME deterministic plan, so the two
    # agree on role keys with no threading.
    # Use the threaded plan when the runner computed one (LLM-tailored), else derive
    # it from the brief. The codegen directive plans from the SAME source, so the two
    # structurally agree on which roles get a sprite file.
    plan = art_plan or direct_art(brief)

    decision = _role_art_source(settings)
    if decision == "disabled":
        return {"generated": 0, "skipped": True, "reason": "disabled",
                "source": "offline", "role_map": {}}
    if decision == "offline":
        from skyn3t.studio.offline_sprites import write_offline_role_sprites

        return write_offline_role_sprites(project_dir, plan)
    if decision == "kenney":
        from skyn3t.studio.kenney_assets import write_kenney_role_sprites
        from skyn3t.studio.offline_sprites import write_offline_role_sprites

        res = write_kenney_role_sprites(project_dir, plan, data_dir=getattr(settings, "data_dir", "data"))
        if res.get("generated") or not plan.sprite_roles():
            return res
        fallback = write_offline_role_sprites(project_dir, plan)
        fallback["requested_source"] = "kenney"
        fallback["fallback_reason"] = res.get("reason") or "no_matches"
        return fallback

    cli = client or ReplicateClient(settings)
    if not cli.available:
        from skyn3t.studio.offline_sprites import write_offline_role_sprites

        return write_offline_role_sprites(project_dir, plan)
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
