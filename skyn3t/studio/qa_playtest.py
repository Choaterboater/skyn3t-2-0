"""QA-playtest gate (roadmap #9) — the catch that SIM gates and a single snapshot
fundamentally can't be: it automates the human playtester.

Every existing game gate tests the pure SIM (the headless invariant gate drives only the
{left,right,up,down,action,pause} sim contract) or judges ONE mid-play screenshot. That
left a class of bug only PLAYING surfaces:

  1. an uncaught render/scene error (e.g. ``Cannot read properties of undefined
     (reading 'x')`` in ``Graphics.fillPoints``) that FREEZES the loop;
  2. a ``ReferenceError`` thrown only when an OFF-CONTRACT key is pressed — e.g.
     ``BARREL_ROLL_COOLDOWN is not defined`` on the Z/Shift barrel-roll, which the sim
     contract never drives; and
  3. codegen that generated plane sprites into ``public/assets/sprites/`` but whose
     renderer NEVER preloaded them (drew primitives instead).

So this gate SERVES the built game (like ``game_visual_check``), drives EVERY control
with a real browser (movement, fire, barrel-roll, pause), and fails on any uncaught
console/page error; and it statically verifies that generated sprite files are actually
referenced by the delivered source. It is ADVISORY and NEVER-RAISES: its gaps feed the
fix-loop as guidance, it never flips the build verdict, and with no Playwright / a game
that won't serve it soft-skips. The browser driver is INJECTED so the logic is unit-
testable without a browser.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Where the build-time art tier writes generated role sprites (see game-art-tier #6).
_SPRITE_DIR = "public/assets/sprites"
_MAX_CONSOLE_ERRORS = 8

# Strip JS block/line comments + HTML comments before the sprite-reference scan so a role
# name mentioned only in a comment can't false-pass. The ``(?<!:)`` guard keeps ``http://``
# / ``https://`` URLs from being eaten as line comments.
_COMMENT_RE = re.compile(r"/\*.*?\*/|<!--.*?-->|(?<!:)//[^\n]*", re.DOTALL)

# A quoted role only counts as "rendered" when it's the key of a real Phaser loader/texture
# call — NOT any bareword string (role names routinely double as anim keys / logic types).
_LOADER_CALL = (
    r"(?:load\.(?:image|spritesheet|atlas)|textures\.exists"
    r"|add\.(?:image|sprite|tileSprite))"
)

# Benign console messages a fully-playable Phaser build emits anyway: the favicon 404 (the
# scaffold's index.html has no <link rel=icon>), generic resource-load failures, the
# autoplay/AudioContext gesture warning, ResizeObserver noise. These are NOT loop-freezing
# exceptions, so they must never flip the verdict or feed a needless code_improve repair.
_BENIGN_CONSOLE_SUBSTRINGS = (
    "favicon",
    "failed to load resource",
    "audiocontext was not allowed to start",
    "resizeobserver loop",
    "play() request was interrupted",
)


def _is_benign_console_error(text: str) -> bool:
    t = str(text).lower()
    return any(p in t for p in _BENIGN_CONSOLE_SUBSTRINGS)


@dataclass(slots=True)
class QaPlaytestVerdict:
    console_errors: list[str] = field(default_factory=list)
    sprites_rendered: bool = True
    missing_sprite_roles: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        """A clean playthrough: served + driven, no uncaught error, sprites rendered."""
        return (not self.skipped) and (not self.console_errors) and self.sprites_rendered

    def to_dict(self) -> dict[str, Any]:
        return {
            "console_errors": list(self.console_errors),
            "sprites_rendered": self.sprites_rendered,
            "missing_sprite_roles": list(self.missing_sprite_roles),
            "skipped": self.skipped,
            "reason": self.reason,
            "ok": self.ok,
            "gaps": self.gaps(),
        }

    def gaps(self) -> list[str]:
        """Fix-loop guidance strings; ``[]`` when the verdict is ok or a soft-skip (a
        could-not-play run must never false-flag a working game)."""
        if self.skipped or self.ok:
            return []
        out: list[str] = []
        for err in self.console_errors:
            out.append(
                f"the game threw an uncaught error during play: {err} — a runtime "
                "exception in the scene/render loop FREEZES the game; fix it so no "
                "uncaught error occurs while playing (drive movement, fire, AND the "
                "barrel-roll/pause keys)."
            )
        if not self.sprites_rendered:
            roles = ", ".join(self.missing_sprite_roles)
            out.append(
                f"the build generated sprites ({_SPRITE_DIR}/<role>.png: {roles}) but "
                "the game never preloads/renders them — preload each in the scene "
                "preload() and render entities with add.image/sprite (textures.exists "
                "fallback to a primitive)."
            )
        return out


# ── pure: are generated sprites actually referenced? (no browser) ─────────────

def _role_referenced(role: str, text: str) -> bool:
    """True iff the delivered source shows the role sprite is LOADED/USED. Evidence is
    EITHER the sprite asset path/filename (``<role>.png`` / ``assets/sprites/<role>``) OR
    the role used as the QUOTED KEY of a real Phaser loader/texture call (``load.image``/
    ``spritesheet``/``atlas`` / ``textures.exists`` / ``add.image``/``sprite``/
    ``tileSprite``). Comments are stripped first, and a bare quoted ``'role'`` no longer
    counts — so a role mentioned only in a comment or as a non-texture logic/anim string
    (e.g. ``const TYPE = 'explosion'`` / ``anims.create({key:'explosion'})``) is correctly
    flagged as never rendered (the exact bug #3 this gate exists to catch), while a real
    load is never false-flagged."""
    src = _COMMENT_RE.sub(" ", text)
    r = re.escape(role)
    if re.search(rf"\b{r}\.png\b", src) or re.search(rf"assets/sprites/{r}\b", src):
        return True
    return bool(
        re.search(rf"{_LOADER_CALL}\s*\([^)]*?['\"`]{r}['\"`]", src, re.DOTALL)
    )


def check_sprites_rendered(project_dir: str | Path) -> tuple[bool, list[str]]:
    """Scan ``public/assets/sprites/*.png`` for generated role files; if none exist the
    game uses primitives by design -> ``(True, [])``. Otherwise read the delivered game
    source (``src/**/*.js`` + ``index.html``) and return ``(False, [roles that have a
    file but are never referenced])`` when at least one generated sprite is never loaded/
    used; else ``(True, [])``. Never raises."""
    try:
        root = Path(project_dir)
        sprite_dir = root / _SPRITE_DIR
        if not sprite_dir.is_dir():
            return True, []
        roles = sorted(p.stem for p in sprite_dir.glob("*.png") if p.is_file())
        if not roles:
            return True, []

        sources: list[str] = []
        src = root / "src"
        if src.is_dir():
            for p in src.rglob("*.js"):
                try:
                    if p.is_file():
                        sources.append(p.read_text(encoding="utf-8", errors="ignore"))
                except Exception:  # noqa: BLE001
                    pass
        index = root / "index.html"
        if index.is_file():
            try:
                sources.append(index.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                pass
        text = "\n".join(sources)

        missing = [role for role in roles if not _role_referenced(role, text)]
        if missing:
            return False, missing
        return True, []
    except Exception:  # noqa: BLE001 - a checker must never break a build
        return True, []


def _dedup_cap(errors: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for e in (errors or []):
        s = str(e).strip()
        if not s or s in seen or _is_benign_console_error(s):
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= _MAX_CONSOLE_ERRORS:
            break
    return out


def build_verdict(console_errors: Any, project_dir: str | Path) -> QaPlaytestVerdict:
    """Combine the (collected) console/page errors with the sprite-reference scan into a
    verdict. Console errors are deduped + capped. Never raises."""
    errs = _dedup_cap(console_errors)
    rendered, missing = check_sprites_rendered(project_dir)
    return QaPlaytestVerdict(
        console_errors=errs,
        sprites_rendered=rendered,
        missing_sprite_roles=missing,
    )


# ── the real Playwright driver (injected, so the logic is browser-free) ───────

def _canvas_shot(page: Any) -> bytes | None:
    """Best-effort PNG bytes of the game canvas (fallback: the whole page). ``None`` on
    failure. Used to detect play (pixel motion) without a Phaser game handle — the Vite
    ES-module scaffold imports Phaser as a module, so there is NO global ``game`` to probe
    via ``page.evaluate``; a WebGL canvas's ``toDataURL`` is blank without
    ``preserveDrawingBuffer``, so a compositor screenshot is the reliable signal."""
    try:
        return page.locator("canvas").first.screenshot(timeout=2000)
    except Exception:  # noqa: BLE001
        try:
            return page.screenshot()
        except Exception:  # noqa: BLE001
            return None


def _drive_and_collect(url: str, *, timeout_ms: int = 15000) -> dict[str, Any]:
    """Load the running game, register page-error + console-error listeners, START it,
    then exercise EVERY control — arrows + WASD (move), Space (fire), Z and Shift (barrel
    roll), P (pause/unpause) — long enough to spawn a wave, and return
    ``{"errors": [...], "play_confirmed": bool|None}``. Sync Playwright — call via
    ``asyncio.to_thread``. Never raises (returns its partial result on any failure).

    Two reliability fixes over a naive driver:
      * Movement + off-contract keys are HELD across a frame (``down``/wait/``up``) instead
        of ``press``ed, so an ``isDown``-polled barrel-roll actually observes the key in a
        scene ``update()`` and throws its ``BARREL_ROLL_COOLDOWN`` ReferenceError (a
        same-frame keydown+keyup leaves ``isDown`` false by update time).
      * Play state is confirmed by canvas pixel motion before/after driving: a never-
        started game (static menu, wrong start input) yields ``play_confirmed=False`` so
        the caller soft-skips instead of recording a never-played run as a clean pass."""
    errors: list[str] = []
    play_confirmed: bool | None = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 800, "height": 600})

                def _on_console(msg: Any) -> None:
                    try:
                        if msg.type == "error":
                            errors.append(str(msg.text))
                    except Exception:  # noqa: BLE001
                        pass

                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("console", _on_console)

                page.goto(url, timeout=timeout_ms, wait_until="load")
                page.wait_for_timeout(1000)
                before = _canvas_shot(page)
                # START the game (menus respond to click / Space / Enter).
                for act in (lambda: page.mouse.click(400, 300),
                            lambda: page.keyboard.press("Space"),
                            lambda: page.keyboard.press("Enter")):
                    try:
                        act()
                    except Exception:  # noqa: BLE001
                        pass
                page.wait_for_timeout(800)

                # HOLD movement + off-contract keys across an update frame (isDown polling);
                # the sim gate never drives these, so the barrel-roll error only fires here.
                hold_keys = ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
                             "a", "d", "w", "s", "z", "Shift")
                tap_keys = ("Space", "p")  # fire + pause are edge-triggered → press
                for _ in range(3):
                    for k in hold_keys:
                        try:
                            page.keyboard.down(k)
                            page.wait_for_timeout(120)
                            page.keyboard.up(k)
                        except Exception:  # noqa: BLE001
                            pass
                    for k in tap_keys:
                        try:
                            page.keyboard.press(k)
                            page.wait_for_timeout(90)
                        except Exception:  # noqa: BLE001
                            pass

                # Settle so a late async/scene-transition error lands before teardown.
                page.wait_for_timeout(400)
                after = _canvas_shot(page)
                if before is not None and after is not None:
                    play_confirmed = before != after
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 - missing browser, nav error, etc.
        return {"errors": errors, "play_confirmed": play_confirmed}
    return {"errors": errors, "play_confirmed": play_confirmed}


async def qa_playtest(
    project_dir: str | Path,
    *,
    settings: Any,
    app_runner: Any | None = None,
    drive_fn: Callable[[str], list[str]] | None = None,
) -> QaPlaytestVerdict:
    """Serve the built game, DRIVE every control with a browser, and fail on any uncaught
    console/page error; also verify generated sprites are actually rendered. ADVISORY:
    the returned verdict's ``gaps()`` feed the fix-loop; it NEVER blocks a build. Soft-
    skips (``skipped=True``, no gaps) when Playwright is unavailable or the game won't
    serve. The browser driver (``drive_fn(url) -> list[str]``) is injected so the logic
    is testable without a browser; the default is the real Playwright driver. Never
    raises."""
    try:
        if drive_fn is None:
            from skyn3t.studio.visual_check import playwright_available

            if not playwright_available():
                return QaPlaytestVerdict(skipped=True, reason="playwright not available")
            drive_fn = _drive_and_collect

        from skyn3t.studio.app_runner import AppRunner, cleanup_serve

        runner = app_runner or AppRunner()
        app = await runner.start(project_dir, "phaser")
        # Always reap the stopped child + unlink its temp log — even when the game never
        # served (start() can return status="failed" WITH a real log_path, e.g. a vite
        # readiness timeout), or the long-lived dashboard leaks a zombie/logfile per drive.
        try:
            url = getattr(app, "url", "") or ""
            if getattr(app, "status", "running") != "running" or not url:
                return QaPlaytestVerdict(
                    skipped=True, reason="game did not serve a preview")
            try:
                driven = await asyncio.to_thread(drive_fn, url)
            except Exception as exc:  # noqa: BLE001 - a driver failure soft-skips
                return QaPlaytestVerdict(skipped=True, reason=f"drive error: {exc}")
            errors, play_confirmed = _normalize_drive_result(driven)
            # A could-not-play run (static menu / wrong start input) must NOT masquerade as
            # a clean pass: with no errors AND play unconfirmed, soft-skip (no false ok, no
            # false gap). A crash is still reported even when motion couldn't be confirmed.
            if play_confirmed is False and not errors:
                return QaPlaytestVerdict(
                    skipped=True, reason="game did not reach play state")
            return build_verdict(errors, project_dir)
        finally:
            try:
                runner.stop(app)
            except Exception:  # noqa: BLE001
                pass
            try:
                cleanup_serve(app)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - a checker must never break a build
        return QaPlaytestVerdict(skipped=True, reason=f"qa playtest error: {exc}")


def _normalize_drive_result(result: Any) -> tuple[list[str], bool | None]:
    """The injected ``drive_fn`` may return a plain ``list[str]`` of error texts (legacy /
    most tests) or a ``dict`` with ``"errors"`` and optional ``"play_confirmed"``. Returns
    ``(errors, play_confirmed)`` where ``play_confirmed`` is ``None`` when the driver
    couldn't determine it (benefit of the doubt — never skip on unknown)."""
    if isinstance(result, dict):
        pc = result.get("play_confirmed")
        return list(result.get("errors") or []), (pc if pc is None else bool(pc))
    return list(result or []), None
