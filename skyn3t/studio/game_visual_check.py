"""Game visual check — the catch that headless gates fundamentally can't be.

The headless invariant gate proves a game's logic is *correct*; it cannot tell whether
there is *anything to play* or whether it *looks right*. Across four discriminators and
two adversarial reviews, no headless heuristic could separate an empty-board / tiny-
sprite game from a good one without false-no_go'ing a real genre — because "is the play
field populated?" and "are entities a readable size?" are VISUAL properties. A human
caught both in seconds by looking; this asks a vision model the same question.

It is never-raises: a screenshot of the running game is judged by a vision model; any
issues feed the fix-loop as guidance (like the #8 input-wiring specialist). Callers
decide policy: the StudioRunner can hard-gate a real non-skipped failure when
``game_quality_gates_verdict`` is enabled, while missing vision / missing screenshot
soft-skips so offline builds are not false-failed.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.studio.visual_check import (
    VisionFn,
    _dom_start_click,
    _extract_json,
    _fit_viewport_to_canvas,
    _vision_locate_with_retry,
)

# Concrete, JSON-only questions a vision model answers reliably (validated: gpt-4o-mini
# flags an empty board and a sparse field). Kept to OBSERVABLE facts ("is it populated",
# "are entities readable-sized"), not taste, so the signal is stable.
GAME_PROMPT = (
    "This is a screenshot of a 2D arcade/action game in mid-play. Judge ONLY what is "
    "visible. Respond with ONLY a JSON object with three keys: "
    '"populated" (true if the play field shows real game entities like enemies, '
    "obstacles, bricks or pickups; false if it is mostly empty space). A BRIEF "
    "between-wave lull in a tower-defense-style game still counts as populated — a "
    "\"Wave cleared\"/score banner, a ready-to-press \"Start Wave\" button, towers "
    "placed on the field, or even just ONE remaining enemy near the goal are all "
    "normal signs of a working, ongoing session, NOT a broken/empty board. Only mark "
    "populated=false if the field is genuinely barren: no towers, no enemies, no "
    "obstacles, and nothing indicating an active session at all. "
    '"entities_readable_size" (true if the main entities are a clear, readable size; '
    "false if they are tiny specks lost in empty space), and "
    '"issues" (a list of short concrete visual problems such as "empty play field", '
    '"sprites too small", or "nothing to interact with"). JSON only.'
)


@dataclass(slots=True)
class GameVisualVerdict:
    populated: bool = True
    entities_readable_size: bool = True
    issues: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        """A clean frame: populated, readable, no issues — and not a soft-skip."""
        return (not self.skipped) and self.populated and self.entities_readable_size and not self.issues

    def gap(self) -> str | None:
        """A fix-loop guidance string when the frame looks empty/tiny, else None.
        Soft-skips (no vision) never produce a gap — they degrade open."""
        if self.skipped or self.ok:
            return None
        parts: list[str] = []
        if not self.populated:
            parts.append("the play field looks EMPTY — spawn the level's real "
                         "obstacles/enemies/pickups so there is something to play")
        if not self.entities_readable_size:
            parts.append("the entities are TINY — render the player and enemies at a "
                         "generous, screen-filling size (not specks in empty space)")
        if self.issues and not parts:
            parts.append("visual issues: " + "; ".join(self.issues[:4]))
        return "the running game does not look right: " + "; ".join(parts) if parts else None


def judge_game_frame(image_path: str, *, vision_fn: VisionFn | None) -> GameVisualVerdict:
    """Ask the vision model whether a game screenshot is populated + readable. Soft-skips
    (no gap) when no vision_fn is wired or the output isn't parseable. Never raises."""
    if vision_fn is None:
        return GameVisualVerdict(skipped=True, reason="no vision provider wired")
    try:
        import json

        raw = vision_fn(image_path, GAME_PROMPT)
        data = json.loads(_extract_json(raw))
        if not isinstance(data, dict):
            return GameVisualVerdict(skipped=True, reason="vision output not an object")
        return GameVisualVerdict(
            populated=bool(data.get("populated", True)),
            entities_readable_size=bool(data.get("entities_readable_size", True)),
            issues=[str(x) for x in (data.get("issues") or [])][:8],
        )
    except Exception as exc:  # noqa: BLE001 - a checker must never break a build
        return GameVisualVerdict(skipped=True, reason=f"vision error: {exc}")


def _looks_like_a_flash_frame(image_bytes: bytes) -> bool:
    """True if the image is suspiciously close to a single solid color —
    symptomatic of a screenshot landing mid an in-game full-screen FX (a
    damage-flash camera tint, a scene-transition fade), not a genuinely
    empty/broken board. Measured live: the SAME real delivered build judged
    populated=True then populated=False with zero code change between runs — one
    judged screenshot happened to land on a solid-red frame from
    ``cameras.main.flash()``, firing on the Still taking a hit. Best-effort and
    dependency-soft: Pillow backs this (``pyproject [visual]`` extra); without
    it, or on any decode failure, this returns False (don't flag) — it's a
    retake OPTIMIZATION, not a correctness requirement, so absence must never
    raise or change behavior. Downsamples before computing per-channel stdev so
    this stays cheap even on a large screenshot."""
    if not image_bytes:
        return False
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.thumbnail((64, 64))
        pixels = list(img.getdata())
        if not pixels:
            return False
        n = len(pixels)

        def _stdev(channel: int) -> float:
            vals = [p[channel] for p in pixels]
            mean = sum(vals) / n
            return (sum((v - mean) ** 2 for v in vals) / n) ** 0.5

        return _stdev(0) < 8 and _stdev(1) < 8 and _stdev(2) < 8
    except Exception:  # noqa: BLE001 - an optimization must never break a build
        return False


def _screenshot_midplay(
    url: str, out_path: str, *, timeout_ms: int = 15000, vision_fn: VisionFn | None = None,
) -> bool:
    """Load the game, START it, settle, and screenshot. Sync Playwright — call via
    asyncio.to_thread. Returns True on success; never raises.

    Three layers reach an actually-playing frame instead of a title/menu screen:
      1. The generic click(400,300)+Space heuristic (arcade/shooter games).
      2. DOM-first (cheap, zero LLM cost): many codegen UIs render the start/level-
         select screen as REAL HTML (a `<button>` overlay on the canvas, not Phaser-
         drawn) — invisible to a canvas screenshot. Tried before any vision call, the
         same DOM-first/vision-fallback split browser-use and Stagehand use.
      3. When ``vision_fn`` is supplied, up to 2 rounds of vision-grounded "are we
         actually playing, and if not, where do I click" — ALWAYS run, not gated on
         "did the canvas already change" (a stray DOM click/CSS transition can satisfy
         that without the game being in play — the false no_go this replaced twice:
         once on a pure-canvas menu, once on an HTML-button overlay that layer 1's
         blind click happened to hit, hiding that layer 3 never ran at all). Mirrors
         Anthropic's own computer-use loop: verify an action via a fresh observation,
         don't infer success from a single before/after diff.
    No vision_fn -> layers 1-2 only (free, deterministic)."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 800, "height": 600})
                page.goto(url, timeout=timeout_ms, wait_until="load")
                page.wait_for_timeout(1200)
                _fit_viewport_to_canvas(page)
                # Launch/start the game so the judge sees real play, not a menu. A
                # settle wait after EACH action, not one grouped wait at the end — a
                # Phaser scene transition/tween triggered by the FIRST action needs
                # its own moment before the next action lands meaningfully (measured:
                # this class of under-settling is the likeliest source of the
                # same-build, same-code "ok=True then ok=False" flakiness found this
                # session).
                for act in (lambda: page.mouse.click(400, 300),
                            lambda: page.keyboard.press("Space")):
                    try:
                        act()
                    except Exception:  # noqa: BLE001 - input is best-effort
                        pass
                    page.wait_for_timeout(750)
                if _dom_start_click(page):
                    page.wait_for_timeout(700)
                canvas = page.locator("canvas").first
                clicked_anything = False

                def _shot_fn():
                    try:
                        s = canvas.screenshot(timeout=2000)
                        b = canvas.bounding_box()
                    except Exception:  # noqa: BLE001
                        return None, None
                    return s, b

                for _ in range(3):
                    # DOM-first EVERY round, not just once: a level-select screen and
                    # an in-game "Start Wave" button are routinely TWO SEPARATE DOM
                    # elements that only exist at different points in the flow (one
                    # before entering the game scene, one after) — checking once up
                    # front misses whichever hasn't rendered yet.
                    if _dom_start_click(page):
                        clicked_anything = True
                        page.wait_for_timeout(700)
                        continue
                    if vision_fn is None:
                        break
                    # Retries a transient parse failure (a CLI backend wrapping JSON
                    # in prose, a frame caught mid-transition) ONCE within this round
                    # before falling through — a single bad response shouldn't burn a
                    # whole round of this loop's own outer budget.
                    in_play, click, box = _vision_locate_with_retry(
                        vision_fn, _shot_fn,
                        wait_fn=lambda: page.wait_for_timeout(300))
                    if not box:
                        break
                    if in_play or click is None:
                        break
                    try:
                        # RAW page coordinates (not Locator.click(position=...)): the
                        # vision-located target is sometimes a REAL DOM element
                        # layered over the canvas (an in-game "Start Wave" button
                        # that only appears once play begins) — canvas.click()
                        # demands the CANVAS be the actionable target and times out
                        # fighting the DOM element actually on top. A raw click at
                        # the same physical point hits whatever's really there,
                        # canvas-internal Phaser object or DOM element alike. Safe
                        # now that _fit_viewport_to_canvas guarantees box.x/y >= 0
                        # (the reason this used to be a Locator click in the first
                        # place — a too-small viewport could clip the canvas to a
                        # negative bounding-box origin).
                        page.mouse.click(box["x"] + click[0], box["y"] + click[1])
                        clicked_anything = True
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(700)
                if clicked_anything:
                    # A freshly-started wave needs a moment for its first entities to
                    # actually spawn and become visible — judging immediately after the
                    # click that triggered it is a coin flip (measured: the SAME build
                    # scored ok=True and ok=False back to back with no code change,
                    # purely from how much had spawned by judgement time).
                    page.wait_for_timeout(1500)
                final_shot = page.screenshot()
                if _looks_like_a_flash_frame(final_shot):
                    # Confirmed live: a judged frame can land mid an in-game
                    # full-screen FX (this build's red damage-flash camera tint on
                    # the Still taking a hit) — for that ONE frame the canvas is a
                    # solid color, genuinely showing nothing, even though the
                    # underlying game state is fine. Retake once after the FX has
                    # almost certainly finished (measured: a 200ms flash) rather
                    # than judge a frame that's accidentally unrepresentative.
                    page.wait_for_timeout(400)
                    retake = page.screenshot()
                    if not _looks_like_a_flash_frame(retake):
                        final_shot = retake
                with open(out_path, "wb") as f:
                    f.write(final_shot)
            finally:
                browser.close()
        return True
    except Exception:  # noqa: BLE001 - missing browser, nav error, etc.
        return False


async def check_game_visual(project_dir: str | Path, *, settings: Any,
                            app_runner: Any | None = None) -> GameVisualVerdict:
    """Serve the built game, screenshot it MID-PLAY, and vision-judge whether the play
    field is populated and the entities are readable-sized. ADVISORY: returns a verdict
    whose ``gap()`` feeds the fix-loop; it NEVER blocks a build. Soft-skips (no gap)
    when no vision model is configured, Playwright is unavailable, or the game won't
    serve. Never raises."""
    from skyn3t.studio.visual_check import (
        make_click_vision_fn,
        make_vision_fn,
        playwright_available,
    )

    try:
        vision_fn = make_vision_fn(settings)
        if vision_fn is None:
            return GameVisualVerdict(skipped=True, reason="no vision model configured")
        if not playwright_available():
            return GameVisualVerdict(skipped=True, reason="playwright not available")

        from skyn3t.studio.preview_supervisor import PreviewSupervisor

        runner = app_runner or PreviewSupervisor()
        app = await runner.start(project_dir, "phaser")
        url = getattr(app, "url", "") or ""
        if not url:
            return GameVisualVerdict(skipped=True, reason="game did not serve a preview")
        try:
            fd, shot = tempfile.mkstemp(prefix="skyn3t-gameshot-", suffix=".png")
            os.close(fd)
            try:
                click_vision_fn = make_click_vision_fn(settings)
                ok = await asyncio.to_thread(_screenshot_midplay, url, shot,
                                              vision_fn=click_vision_fn)
                if not ok:
                    return GameVisualVerdict(skipped=True, reason="screenshot failed")
                return judge_game_frame(shot, vision_fn=vision_fn)
            finally:
                try:
                    os.unlink(shot)
                except OSError:
                    pass
        finally:
            try:
                stopped = runner.stop(app)
                if inspect.isawaitable(stopped):
                    await stopped
            except Exception:  # noqa: BLE001
                pass
            # Reap the stopped child + unlink its temp log; in the long-lived dashboard
            # process stop() alone leaves a zombie + a leaked logfile per judge.
            try:
                from skyn3t.studio.app_runner import cleanup_serve
                cleanup_serve(app)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - a checker must never break a build
        return GameVisualVerdict(skipped=True, reason=f"visual check error: {exc}")
