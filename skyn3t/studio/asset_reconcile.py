"""Reconcile hallucinated asset loads in a generated game (do-no-harm).

THE STORY. A real Phaser tower-defense build no_go'd because codegen wrote
``this.load.image('gold', '/assets/sprites/gold.png')`` while the art tier only
delivered tower/enemy/brute/flyer/boss under ``public/assets/sprites/``. The GDD
mentioned "gold" purely as economy currency; the model hallucinated an asset path
for it. Phaser cannot process the 404 body -> an uncaught runtime error at boot ->
``qa_playtest`` (correctly) fails the build over a real, player-visible crash.

THE FIX. Scan the delivered game's source for Phaser loader calls and, for every
IMAGE url whose file is missing, synthesize a valid placeholder PNG at that exact
path so the game boots and the loader has something real to decode. Missing AUDIO
is NOT synthesized (silent fake audio is worse than a reported gap) — it is
returned under ``missing_audio`` so a later step can feed it to the fix-loop.

DO-NO-HARM CONTRACT. This module only ever CREATES files (never edits code),
confines every write to the project root (an escaping ``../../`` url is skipped,
not written), acts only for game stacks, and NEVER raises — a bad file, an
unreadable source, a mkdir failure all degrade to "did less", never a crash.

WIRING IS SEPARATE. This returns a report shaped to merge into the deterministic
repair dict; hooking ``reconcile_asset_refs`` into ``apply_deterministic_repairs``
(and pairing it with a codegen directive clause that tells the model to only load
assets the art tier actually delivers) happens in a later change, by the owners of
those files.

ON THE PLACEHOLDER GENERATOR. The art tier's "$0 offline primitive" is NOT a
Python routine that draws sprite PNGs — the colored-primitive fallback lives in
the Phaser scaffold and runs in the browser at render time (``assets.py`` skips
generation entirely for ``game_art_source=offline`` and the JS scaffold draws the
rectangles/circles). There is therefore no existing Python function to reuse for
drawing a placeholder file. So, per the fallback path in the spec, we draw a
minimal valid RGBA PNG with Pillow — the SAME imaging library the art tier already
uses (``skyn3t.studio.assets._key_bg_to_alpha``), adding no new dependency — at the
same 256x256 RGBA convention the sprite models use, with a crisp colored primitive
whose color is derived deterministically from the asset key.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Game stacks this reconciler acts on. A frozenset so extending to another game
# stack later is a one-token change; anything else is a clean no-op.
_GAME_STACKS: frozenset[str] = frozenset({"phaser"})

# Phaser loader calls we understand. ``\.load\.`` matches ``this.load.``,
# ``scene.load.``, ``that.load.`` etc.; the two capture groups are the KEY (first
# arg) and the URL (second arg), each single- OR double-quoted with flexible
# whitespace. The quote char is backreferenced (\2 for the key, \4 for the url) so
# a quote of the other kind inside the string can't end it early, and ``\\.``
# tolerates an escaped quote. ``spritesheet``/``image`` load images; ``audio``
# is collected but never synthesized (see module docstring).
_LOADER_RE = re.compile(
    r"\.load\.(?P<method>image|spritesheet|audio)\s*\(\s*"
    r"(['\"])(?P<key>(?:\\.|(?!\2).)*)\2\s*,\s*"
    r"(['\"])(?P<url>(?:\\.|(?!\4).)*)\4",
    re.IGNORECASE,
)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# Source file discovery — bounded so a pathological tree can't stall a build. We
# only scan the game's own source under src/; delivered assets, deps and build
# output are never source.
_SRC_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
_SKIP_DIRS = {"node_modules", "dist", "build", ".next", ".git", "coverage", ".cache"}
_MAX_SRC_FILES = 500
_MAX_FILE_BYTES = 1_000_000

_PLACEHOLDER_SIZE = 256  # matches the sprite models' 256x256 convention

# A small, high-contrast palette for placeholders. The exact hue never matters
# (this is a stand-in until real art lands); what matters is that the file is a
# valid, visible texture rather than a 404. Deterministic per key so rebuilds are
# byte-stable.
_PLACEHOLDER_COLORS: tuple[tuple[int, int, int], ...] = (
    (57, 255, 20), (0, 234, 255), (255, 43, 214), (255, 230, 0), (167, 139, 250),
    (255, 93, 115), (124, 197, 118), (96, 165, 250), (244, 114, 182), (230, 180, 34),
)


def _iter_source_files(src_dir: Path):
    """Yield source files under ``src_dir`` (bounded), skipping vendored/build
    directories. Never raises — a walk error simply ends iteration."""
    count = 0
    try:
        walker = os.walk(src_dir)
    except OSError:
        return
    for dirpath, dirnames, filenames in walker:
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in _SRC_EXTS:
                yield Path(dirpath) / name
                count += 1
                if count >= _MAX_SRC_FILES:
                    return


def _read_text(path: Path) -> str:
    """Read a source file as text, or ``""`` if it can't be read (unreadable,
    too large, gone). Never raises."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _asset_relpath(url: str) -> str | None:
    """Normalize a loader url to a safe project-relative path under ``assets/``,
    or ``None`` if it isn't ours to reconcile.

    Handles both ``/assets/...`` (root-relative) and ``assets/...`` (relative)
    forms, strips any ``?query``/``#hash``, and lexically resolves ``.``/``..``.
    A url that escapes ``assets/`` (e.g. ``/assets/../../evil.png`` -> ``../evil.png``)
    or points elsewhere (a CDN url, a bare ``logo.png``) returns ``None`` — this is
    the primary traversal guard, backed by a commonpath check at write time."""
    path = url.split("?", 1)[0].split("#", 1)[0].strip()
    if not path or "://" in path:  # external/absolute url — not a local asset
        return None
    norm = posixpath.normpath(path.lstrip("/"))
    if norm == "assets" or not norm.startswith("assets/"):
        return None
    return norm


def _confined_target(root: Path, relpath: str) -> Path | None:
    """Resolve ``base/relpath`` (base = ``root/public`` if present, else ``root``)
    and return it ONLY if it stays within ``root``. Mirrors the codebase's
    ``_confine`` guard. ``None`` on escape or any resolution error."""
    base = root / "public" if (root / "public").is_dir() else root
    try:
        target = (base / relpath).resolve()
        root_resolved = root.resolve()
        if os.path.commonpath([str(root_resolved), str(target)]) != str(root_resolved):
            return None
        return target
    except (OSError, ValueError):
        return None


def _color_for(key: str) -> tuple[int, int, int]:
    """Deterministic palette color for an asset key — a stable hash (NOT Python's
    salted ``hash()``) so two builds of the same key produce identical bytes."""
    digest = hashlib.sha1(key.encode("utf-8", "ignore")).digest()
    return _PLACEHOLDER_COLORS[digest[0] % len(_PLACEHOLDER_COLORS)]


def _write_placeholder_png(target: Path, key: str) -> bool:
    """Draw a minimal valid 256x256 RGBA placeholder (a crisp colored primitive on
    a transparent field) at ``target``. Returns True on success. Never raises."""
    try:
        from PIL import Image, ImageDraw

        color = _color_for(key)
        im = Image.new("RGBA", (_PLACEHOLDER_SIZE, _PLACEHOLDER_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(im)
        pad = _PLACEHOLDER_SIZE // 8
        # An inset filled disc reads as a deliberate primitive, not a broken image;
        # ImageDraw.ellipse is available on every Pillow version.
        draw.ellipse(
            [pad, pad, _PLACEHOLDER_SIZE - pad, _PLACEHOLDER_SIZE - pad],
            fill=(*color, 255),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        im.save(target, "PNG")
        return True
    except Exception as exc:  # noqa: BLE001 - reconciliation must never break a build
        log.warning("asset_reconcile.write_failed", target=str(target)[:200], error=str(exc)[:160])
        return False


def reconcile_asset_refs(project_dir: str | Path, stack: str = "") -> dict:
    """Synthesize placeholder PNGs for hallucinated IMAGE loads and report missing
    AUDIO loads in a generated game.

    For a game stack, scans ``src/**`` for Phaser ``load.image``/``load.spritesheet``/
    ``load.audio`` calls whose target url points under ``assets/``. Every missing
    image gets a valid placeholder PNG written at its exact path (confined to the
    project root); every missing audio ref is reported, never faked.

    Returns ``{"images_created": [<root-relative paths>], "missing_audio": [<urls>],
    "refs_scanned": <int>}``. A non-game stack (or any error) returns the zeroed
    form without touching disk. Never raises. Wiring into
    ``apply_deterministic_repairs`` is a separate change (see module docstring)."""
    empty = {"images_created": [], "missing_audio": [], "refs_scanned": 0}
    if str(stack or "").lower() not in _GAME_STACKS:
        return empty

    try:
        root = Path(project_dir)
        src_dir = root / "src"
        images_created: list[str] = []
        missing_audio: list[str] = []
        refs_scanned = 0
        handled_targets: set[str] = set()   # dedupe image writes by resolved path
        seen_audio: set[str] = set()         # dedupe audio urls

        for src_file in _iter_source_files(src_dir):
            text = _read_text(src_file)
            if not text or ".load." not in text:
                continue
            for m in _LOADER_RE.finditer(text):
                method = m.group("method").lower()
                url = m.group("url")
                relpath = _asset_relpath(url)
                if relpath is None:
                    continue  # not a local asset under assets/ — not ours

                if method == "audio":
                    refs_scanned += 1
                    target = _confined_target(root, relpath)
                    if target is not None and not target.exists() and url not in seen_audio:
                        seen_audio.add(url)
                        missing_audio.append(url)  # reported, never synthesized
                    continue

                # image / spritesheet — require a real image extension.
                if os.path.splitext(relpath)[1].lower() not in _IMAGE_EXTS:
                    continue
                refs_scanned += 1
                target = _confined_target(root, relpath)
                if target is None:
                    continue  # escapes the root — skip (never write outside)
                key = str(target)
                if target.exists() or key in handled_targets:
                    continue
                if _write_placeholder_png(target, m.group("key") or relpath):
                    handled_targets.add(key)
                    try:
                        rel = target.relative_to(root.resolve()).as_posix()
                    except ValueError:
                        rel = target.name
                    images_created.append(rel)

        log.info(
            "asset_reconcile.done",
            refs_scanned=refs_scanned,
            images_created=len(images_created),
            missing_audio=len(missing_audio),
        )
        return {
            "images_created": images_created,
            "missing_audio": missing_audio,
            "refs_scanned": refs_scanned,
        }
    except Exception as exc:  # noqa: BLE001 - never break a build over reconciliation
        log.warning("asset_reconcile.failed", error=str(exc)[:200])
        return empty
