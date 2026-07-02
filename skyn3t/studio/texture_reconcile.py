"""Reconcile used-but-never-loaded texture keys in a generated Phaser game.

THE STORY. The honest "games still render blank/green boxes" root cause, and it
happens with EVERY model (cheap AND frontier — confirmed on Opus): codegen writes
a render call against a texture key that ``preload()`` never loaded. E.g. it does
``this.load.image('enemy', 'assets/sprites/enemy.png')`` but then draws the entity
with ``this.add.sprite(x, y, 'monster')`` — an invented key. Phaser can't find
``'monster'`` so it renders its built-in green ``__MISSING`` box. No crash, just a
visibly broken game that ``qa_playtest``/``game_visual_check`` then (correctly)
fail. ``qa_playtest.check_sprites_rendered`` already DETECTS this and ADVISES the
fix-loop to remap the key — but that advice rides on the model actually following
it, which is exactly what fails across the board. So the fix must be DETERMINISTIC
and model-independent: guarantee, after generation, that every texture key used in
a render call is really loaded.

THE FIX. Scan the delivered game's source for (a) the keys LOADED via
``load.image``/``load.spritesheet``/``load.atlas`` and (b) the keys USED as the
texture arg of a render call (``add.image``/``add.sprite``/``add.tileSprite``,
``physics.add.*``, ``setTexture``). For every USED key that is never LOADED,
synthesize a valid placeholder PNG under ``public/assets/sprites/`` and inject the
matching ``load.image('<key>', 'assets/sprites/<key>.png')`` into ``preload()`` (by
anchoring on an existing loader call so we always land in a real preload context).
The game now loads a real texture for every key it draws — the green box is gone.

This is the complement of ``asset_reconcile`` (which handles the reverse: a
``load`` call whose FILE is missing). Together they close the load↔use gap in both
directions.

DO-NO-HARM CONTRACT. Acts only for game stacks. It CREATES placeholder files and
INSERTS loader lines — it never rewrites, reorders, or deletes existing code, and
every write is confined to the project root. It BAILS entirely (touches nothing)
when the game loads textures via computed/variable keys (``for (k of list)
this.load.image(k, ...)`` / ``sprite.setTexture(tex)``) — there a "used literal"
might be loaded dynamically, so we cannot safely conclude it's unloaded. Injection
requires an existing loader call to anchor on; with none, the game loads nothing by
design and we leave it untouched. NEVER raises — every failure degrades to "did
less", never a crash.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

# Reuse the do-no-harm primitives + game-stack gate from the sibling reconciler
# (one placeholder generator, one confinement guard, one source walker), and the
# dynamic-key guard from the detector that already reasons about this exact idiom.
from skyn3t.studio.asset_reconcile import (
    _GAME_STACKS,
    _iter_source_files,
    _read_text,
    _write_placeholder_png,
)
from skyn3t.studio.qa_playtest import _VAR_KEYED_TEXTURE

log = structlog.get_logger(__name__)

# A texture KEY as the first quoted literal arg of a real Phaser LOADER call.
_LOAD_KEY_RE = re.compile(
    r"\.load\.(?:image|spritesheet|atlas)\s*\(\s*(['\"])([\w$-]+)\1",
    re.IGNORECASE,
)

# A texture KEY as the texture arg of a real RENDER call. Coordinates (numbers,
# commas, whitespace, simple vars) precede the key, so the FIRST quoted literal
# inside the arg list is the texture key. ``.create`` (group.create) and
# ``make.sprite({key})`` are deliberately NOT matched — ``anims.create({key:...})``
# and config-object forms would false-match a non-texture string.
_USE_KEY_RE = re.compile(
    r"(?:\.add|physics\.add)\.(?:sprite|image|tileSprite)\s*\(\s*"
    r"[^)'\"`;]*?(['\"])([\w$-]+)\1",
    re.IGNORECASE,
)
_SETTEXTURE_RE = re.compile(r"\.setTexture\s*\(\s*(['\"])([\w$-]+)\1", re.IGNORECASE)

# An existing loader call we can anchor an injection on, found ANYWHERE in a line
# (``preload() { this.load.image(...) }`` puts it mid-line). Captures the receiver
# (``this.load`` / ``scene.load`` / ``that.load``) so the injected call matches; the
# line's own leading indentation is used for the inserted lines.
_LOADER_ANCHOR_RE = re.compile(
    r"(?P<recv>[A-Za-z_$][\w$.]*\.load)\.(?:image|spritesheet|atlas)\s*\(",
)

# Phaser's built-in/internal texture keys are always present — never "unloaded".
_BUILTIN_KEYS = {"__DEFAULT", "__MISSING", "__WHITE", "__NORMAL"}


def _sprite_target(root: Path, key: str) -> Path | None:
    """Resolve the placeholder path for a texture key under ``public/assets/sprites``
    (where Vite serves ``/assets/...`` from and where the art tier writes delivered
    sprites), confined to the project root. ``None`` on escape/resolution error.
    Keys are ``[\\w$-]+`` (no separators) so the join can't traverse, but we verify
    anyway. The injected loader URL ``assets/sprites/<key>.png`` resolves to exactly
    this file at runtime."""
    try:
        root_resolved = root.resolve()
        target = (root_resolved / "public" / "assets" / "sprites" / f"{key}.png").resolve()
        if not target.is_relative_to(root_resolved):
            return None
        return target
    except (OSError, ValueError):
        return None


def _collect_keys(sources: list[tuple[Path, str]]) -> tuple[set[str], set[str]]:
    """Return (loaded_keys, used_keys) across all source texts."""
    loaded: set[str] = set()
    used: set[str] = set()
    for _, text in sources:
        for m in _LOAD_KEY_RE.finditer(text):
            loaded.add(m.group(2))
        for m in _USE_KEY_RE.finditer(text):
            used.add(m.group(2))
        for m in _SETTEXTURE_RE.finditer(text):
            used.add(m.group(2))
    return loaded, used


def _inject_loads(sources: list[tuple[Path, str]], keys: list[str]) -> Path | None:
    """Insert ``<recv>.image('<key>', 'assets/sprites/<key>.png')`` for each key,
    immediately before the first existing loader call found (so we land inside a
    real ``preload``). Writes the file and returns its path, or None if there was
    no loader call to anchor on (do-no-harm: never guess a preload location)."""
    for path, text in sources:
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            am = _LOADER_ANCHOR_RE.search(line)
            if not am:
                continue
            recv = am.group("recv")
            indent = re.match(r"[ \t]*", line).group()
            injected = [
                f"{indent}{recv}.image('{k}', 'assets/sprites/{k}.png');\n"
                for k in keys
            ]
            lines[i:i] = injected
            try:
                path.write_text("".join(lines), encoding="utf-8")
                return path
            except OSError as exc:  # noqa: BLE001 - never break a build over a write
                log.warning("texture_reconcile.inject_failed", path=str(path)[:200],
                            error=str(exc)[:160])
                return None
    return None


def reconcile_texture_keys(project_dir: str | Path, stack: str = "") -> dict:
    """Guarantee every USED texture key in a generated game is actually LOADED.

    For a game stack, scans ``src/**`` for loaded vs. used texture keys; for each
    key drawn but never loaded, writes a placeholder PNG under
    ``public/assets/sprites/`` and injects the matching ``load.image`` into
    ``preload()``. Returns ``{"textures_loaded_added": [<keys>],
    "placeholders_created": [<root-relative paths>], "skipped_dynamic": <bool>}``.
    A non-game stack, a dynamic-key game, or any error returns the zeroed form
    without touching disk. Never raises."""
    empty = {"textures_loaded_added": [], "placeholders_created": [], "skipped_dynamic": False}
    if str(stack or "").lower() not in _GAME_STACKS:
        return empty

    try:
        root = Path(project_dir)
        src_dir = root / "src"
        if not src_dir.is_dir():
            return empty

        sources: list[tuple[Path, str]] = []
        for src_file in _iter_source_files(src_dir):
            text = _read_text(src_file)
            if text:
                sources.append((src_file, text))
        if not sources:
            return empty

        # Bail if the game loads/renders via computed keys — a "used literal" may be
        # loaded dynamically, so we can't safely call it unloaded (do-no-harm).
        if any(_VAR_KEYED_TEXTURE.search(text) for _, text in sources):
            return {**empty, "skipped_dynamic": True}

        loaded, used = _collect_keys(sources)
        unloaded = sorted(
            k for k in used
            if k not in loaded and k not in _BUILTIN_KEYS
        )
        if not unloaded:
            return empty

        # Synthesize a placeholder for each unloaded key first; only inject loads for
        # keys whose file we actually created (or that already exist on disk).
        placeholders_created: list[str] = []
        resolvable: list[str] = []
        for key in unloaded:
            target = _sprite_target(root, key)
            if target is None:
                continue  # escapes root — skip
            if target.exists():
                resolvable.append(key)
                continue
            if _write_placeholder_png(target, key):
                try:
                    rel = target.relative_to(root.resolve()).as_posix()
                except ValueError:
                    rel = target.name
                placeholders_created.append(rel)
                resolvable.append(key)

        if not resolvable:
            return empty

        injected_path = _inject_loads(sources, resolvable)
        if injected_path is None:
            # No loader call to anchor on — leave the tree untouched. The placeholder
            # files we wrote are harmless (unreferenced), so report nothing added.
            return {**empty, "placeholders_created": placeholders_created}

        log.info("texture_reconcile.done", keys=len(resolvable),
                 placeholders=len(placeholders_created))
        return {
            "textures_loaded_added": resolvable,
            "placeholders_created": placeholders_created,
            "skipped_dynamic": False,
        }
    except Exception as exc:  # noqa: BLE001 - never break a build over reconciliation
        log.warning("texture_reconcile.failed", error=str(exc)[:200])
        return empty
