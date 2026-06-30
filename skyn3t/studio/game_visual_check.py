"""Game visual check — the catch that headless gates fundamentally can't be.

The headless invariant gate proves a game's logic is *correct*; it cannot tell whether
there is *anything to play* or whether it *looks right*. Across four discriminators and
two adversarial reviews, no headless heuristic could separate an empty-board / tiny-
sprite game from a good one without false-no_go'ing a real genre — because "is the play
field populated?" and "are entities a readable size?" are VISUAL properties. A human
caught both in seconds by looking; this asks a vision model the same question.

It is ADVISORY and never-raises: a screenshot of the running game is judged by a vision
model; any issues feed the fix-loop as guidance (like the #8 input-wiring specialist),
never a hard block — a fuzzy vision verdict must not be able to false-no_go a build.
With no vision model / no screenshot it soft-skips.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.studio.visual_check import VisionFn, _extract_json

# Concrete, JSON-only questions a vision model answers reliably (validated: gpt-4o-mini
# flags an empty board and a sparse field). Kept to OBSERVABLE facts ("is it populated",
# "are entities readable-sized"), not taste, so the signal is stable.
GAME_PROMPT = (
    "This is a screenshot of a 2D arcade/action game in mid-play. Judge ONLY what is "
    "visible. Respond with ONLY a JSON object with three keys: "
    '"populated" (true if the play field shows real game entities like enemies, '
    "obstacles, bricks or pickups; false if it is mostly empty space), "
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


def _screenshot_midplay(url: str, out_path: str, *, timeout_ms: int = 15000) -> bool:
    """Load the game, START it (click + Space, so we see MID-PLAY not just a title
    screen), settle, and screenshot. Sync Playwright — call via asyncio.to_thread.
    Returns True on success; never raises."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 800, "height": 600})
                page.goto(url, timeout=timeout_ms, wait_until="load")
                page.wait_for_timeout(1200)
                # Launch/start the game so the judge sees real play, not a menu.
                try:
                    page.mouse.click(400, 300)
                    page.keyboard.press("Space")
                    page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001 - input is best-effort
                    pass
                page.screenshot(path=out_path)
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
    from skyn3t.studio.visual_check import make_vision_fn, playwright_available

    try:
        vision_fn = make_vision_fn(settings)
        if vision_fn is None:
            return GameVisualVerdict(skipped=True, reason="no vision model configured")
        if not playwright_available():
            return GameVisualVerdict(skipped=True, reason="playwright not available")

        from skyn3t.studio.app_runner import AppRunner

        runner = app_runner or AppRunner()
        app = await runner.start(project_dir, "phaser")
        url = getattr(app, "url", "") or ""
        if not url:
            return GameVisualVerdict(skipped=True, reason="game did not serve a preview")
        try:
            fd, shot = tempfile.mkstemp(prefix="skyn3t-gameshot-", suffix=".png")
            os.close(fd)
            try:
                ok = await asyncio.to_thread(_screenshot_midplay, url, shot)
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
                runner.stop(app)
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
