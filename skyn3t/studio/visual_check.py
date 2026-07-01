# skyn3t/studio/visual_check.py
"""Visual self-inspection (Spec 3, Slice 3): screenshot a running app and ask a
vision model whether it matches the goal. EVERY dependency is optional — with no
Playwright or no vision_fn, check() returns a `skipped` verdict and never blocks.
Never raises."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from skyn3t.core.events import EventType

VisionFn = Callable[[str, str], str]  # (image_path, prompt) -> raw model text

# Common "begin/continue play" affordance text — used by _dom_start_click for
# accessibility-tree/DOM matching BEFORE any vision call is attempted.
_START_TEXT_PATTERN = re.compile(r"\b(start|play|begin|continue|run|go|enter)\b", re.IGNORECASE)

# A small, widely-available, cheap vision-capable model on OpenRouter; override
# with settings.vision_model.
_DEFAULT_VISION_MODEL = "openai/gpt-4o-mini"


@dataclass(slots=True)
class VisualVerdict:
    matches: bool = False
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)
    fix_hint: str = ""
    skipped: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def screenshot(url: str, out_path: str, *, timeout_ms: int = 8000) -> str | None:
    """Capture a full-page PNG of `url`. Returns the path, or None on any failure
    (incl. Playwright not installed). Never raises."""
    if not playwright_available():
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout_ms, wait_until="load")
                page.screenshot(path=out_path, full_page=True)
            finally:
                browser.close()
        return out_path
    except Exception:  # noqa: BLE001 - missing browser binary, nav error, etc.
        return None


def _extract_json(text: str) -> str:
    t = (text or "").strip()
    if "```" in t:
        parts = t.split("```")
        if len(parts) > 1:
            t = parts[1].removeprefix("json").strip()
    a, b = t.find("{"), t.rfind("}")
    # no '{' -> returns raw text; json.loads then fails and inspect() soft-skips (safe).
    return t[a:b + 1] if a >= 0 and b > a else t


def inspect(image_path: str, goal: str, *, vision_fn: VisionFn | None = None,
            prior: str | None = None) -> VisualVerdict:
    """Ask `vision_fn` whether the screenshot fulfills `goal`. Soft-skips when no
    vision_fn is supplied or the output isn't parseable. Never raises."""
    if vision_fn is None:
        return VisualVerdict(skipped=True, reason="no vision provider wired")
    prompt = (
        "You are reviewing a screenshot of a running web app. "
        f"The user asked for: '{goal}'."
        + (f" Prior attempt note: {prior}." if prior else "")
        + " Does the screenshot fulfill that request AND look visually correct and"
        " polished (layout, contrast, no obvious breakage)? Respond ONLY as JSON: "
        '{"matches": <bool>, "confidence": <0..1>, "issues": ["..."], '
        '"fix_hint": "<one concrete change if it does not match, else empty>"}'
    )
    try:
        raw = vision_fn(image_path, prompt)
        data = json.loads(_extract_json(raw))
        return VisualVerdict(
            matches=bool(data.get("matches", False)),
            confidence=float(data.get("confidence", 0.5)),
            issues=[str(x) for x in (data.get("issues") or [])],
            fix_hint=str(data.get("fix_hint", "")),
        )
    except Exception as exc:  # noqa: BLE001
        return VisualVerdict(skipped=True, reason=f"vision error: {exc}")


def _image_data_url(image_path: str) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _vision_messages(data_url: str, prompt: str) -> list[dict[str, Any]]:
    """OpenAI/OpenRouter-style multimodal message: a text part + an image part."""
    return [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]


def _make_openrouter_vision_fn(settings: Any) -> VisionFn | None:
    """Build a vision_fn that judges a screenshot via OpenRouter, or None when no key
    is configured."""
    key = str(getattr(settings, "openrouter_api_key", "") or "")
    if not key:
        return None
    model = str(getattr(settings, "vision_model", "") or "") or _DEFAULT_VISION_MODEL

    def _vision_fn(image_path: str, prompt: str) -> str:
        import httpx

        from skyn3t.adapters.llm import OPENROUTER_URL
        body = {"model": model,
                "messages": _vision_messages(_image_data_url(image_path), prompt),
                "max_tokens": 700}
        headers = {"Authorization": f"Bearer {key}",
                   "HTTP-Referer": "https://github.com/skyn3t", "X-Title": "SkyN3t"}
        with httpx.Client(timeout=60) as client:
            resp = client.post(OPENROUTER_URL, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]

    return _vision_fn


def _make_cli_vision_fn(settings: Any) -> VisionFn | None:
    """Build a vision_fn that judges a screenshot via a vision-capable CLI on PATH
    (claude/kimi), or None when none is found. The CLI reads the image FILE (not
    base64 over HTTP), so it judges the screenshot natively — no OpenRouter dependency
    anywhere."""
    from skyn3t.adapters.llm import _no_mcp_args  # local import avoids any cycle

    provider = str(getattr(settings, "cli_llm_provider", "") or "claude").lower()
    for prov in (provider, "claude", "kimi"):
        if shutil.which(prov):
            def _cli_vision_fn(image_path: str, prompt: str, _prov: str = prov) -> str:
                full = (f"View the image file at {image_path}. {prompt} "
                        "Respond with ONLY the JSON object, no prose.")
                # cwd = the image's OWN directory. A `--setting-sources project` CLI
                # session scopes its file-read permission to its cwd ("the project");
                # screenshots live in a system temp dir, nowhere near wherever this
                # Python process happens to be running from. Without this, the CLI
                # refuses the read ("outside the project directory") and silently
                # degrades to an unparseable refusal string instead of a verdict.
                cwd = os.path.dirname(image_path) or None
                try:
                    out = subprocess.run([_prov, "-p", full, *_no_mcp_args(settings, _prov)],
                                         capture_output=True, text=True, timeout=120,
                                         cwd=cwd)
                    return out.stdout or ""
                except Exception:  # noqa: BLE001 - CLI missing/slow -> empty -> soft-skip
                    return ""
            return _cli_vision_fn
    return None


def make_vision_fn(settings: Any) -> VisionFn | None:
    """Build a vision_fn that judges a screenshot (true/false + free-text issues), or
    None when no backend is configured (the loop then soft-skips). This auto-activates
    the visual loop's judgement step wherever an OpenRouter key + vision model are
    available — closing the 'text-only LLM' gap without changing the loop. OpenRouter is
    preferred when a key is present (cheap, fast — plenty accurate for "does this look
    right" judgments); falls back to a CLI (claude/kimi) only without a key."""
    return _make_openrouter_vision_fn(settings) or _make_cli_vision_fn(settings)


def make_click_vision_fn(settings: Any) -> VisionFn | None:
    """Build a vision_fn for PIXEL-GROUNDING tasks (locating a click target on a
    screenshot) — same backends as ``make_vision_fn``, but CLI-preferred. Measured
    live on the same menu screenshot: the local claude CLI located a card's click
    target within 1px (179,276 vs the true ~180,275); the default cheap OpenRouter
    vision model (gpt-4o-mini) was off by ~230px and landed on the wrong, locked card.
    "Does this look right" (matches/populated/issues) doesn't need that precision, so
    only the click-locator path pays for the stronger backend."""
    return _make_cli_vision_fn(settings) or _make_openrouter_vision_fn(settings)


def _dom_start_click(page: Any) -> bool:
    """Click a visible DOM element (button/link/any element) whose text suggests it
    begins or continues play (Start/Play/Begin/Continue/Run/Go/Enter) — BEFORE falling
    back to vision-grounded canvas clicking. Cheap, deterministic, zero LLM cost, and
    catches what vision-on-the-canvas fundamentally can't: many codegen UIs render the
    start/level-select screen as REAL HTML (a `<button>` or `<div>` overlay on top of
    the canvas, not Phaser-drawn) — invisible to a canvas-only screenshot. Mirrors the
    DOM-first / vision-fallback pattern used by browser-use, Stagehand, and similar
    browser-agent frameworks: try the cheap, reliable signal (the accessibility tree /
    DOM text) before paying for a vision call, which is reserved for genuinely opaque
    canvas content. Returns True iff a click was attempted (not whether it changed
    anything — the caller verifies that separately, the same way Anthropic's own
    computer-use loop verifies an action instead of trusting it blindly). Never
    raises."""
    for locator_fn in (
        lambda: page.get_by_role("button", name=_START_TEXT_PATTERN),
        lambda: page.get_by_role("link", name=_START_TEXT_PATTERN),
        lambda: page.get_by_text(_START_TEXT_PATTERN),
    ):
        try:
            candidates = locator_fn()
            count = candidates.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(min(count, 10)):
            try:
                el = candidates.nth(i)
                if el.is_visible():
                    el.click(timeout=2000)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _fit_viewport_to_canvas(
    page: Any, *, min_w: int = 800, min_h: int = 600,
    margin_w: int = 40, margin_h: int = 220,
) -> None:
    """Grow the page viewport to comfortably fit the game's canvas BEFORE any click or
    screenshot. A fixed-resolution canvas (e.g. 1280x720) routinely exceeds a driver's
    default 800x600 viewport; the scaffold's CSS commonly centers it in an
    ``overflow:hidden`` container with NO scrollbar, so "scroll into view" has nothing
    to scroll — the clipped portion (often the whole left/top of the canvas, at a
    NEGATIVE bounding-box x/y) is genuinely unreachable by click, not just off-screen.
    Reads the canvas's actual RENDERED size via ``getBoundingClientRect()`` (correct
    even for a CSS-scaled canvas) and never shrinks below ``min_w``/``min_h``. Never
    raises; on any failure (no canvas yet, no ``set_viewport_size`` support) the
    viewport is simply left as-is."""
    try:
        rect = page.locator("canvas").first.evaluate(
            "el => { const r = el.getBoundingClientRect(); "
            "return {w: r.width, h: r.height}; }"
        )
        w = int(rect.get("w") or 0)
        h = int(rect.get("h") or 0)
        if w > 0 and h > 0:
            page.set_viewport_size({
                "width": max(min_w, w + margin_w),
                "height": max(min_h, h + margin_h),
            })
    except Exception:  # noqa: BLE001 - a checker must never break a build
        pass


def _vision_locate_start_click(
    vision_fn: VisionFn | None, image_bytes: bytes | None, canvas_w: int, canvas_h: int,
) -> tuple[bool, tuple[float, float] | None]:
    """Ask a vision model whether a game canvas already shows active gameplay, or — for
    a menu/level-select-driven genre (tower defense: pick a level card, then press a
    "Start Wave" button) a blind click(400,300)+Space+Enter heuristic never reaches —
    where to click next. Returns ``(in_play, click)`` where ``click`` is an (x, y) pair
    in the SAME pixel space as the supplied screenshot (the canvas itself, so the caller
    only has to add the canvas's page offset). Never raises: any missing input or
    unparseable/exploding response is treated as "no instruction" (``(False, None)``),
    same soft-skip posture as every other vision call in this module. Shared by
    ``qa_playtest`` and ``game_visual_check`` — both serve a game mid-play and both hit
    the same "blind start heuristic can't reach a menu-driven game" gap."""
    if vision_fn is None or not image_bytes:
        return False, None
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="skyn3t-qa-shot-", suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
        prompt = (
            "This is a screenshot of a video game's canvas "
            f"({canvas_w}x{canvas_h} px). The goal is to reach a state where the play "
            "field shows REAL GAME ENTITIES (enemies, characters, obstacles, "
            "projectiles, or pickups) actually present on it. This is NOT satisfied by "
            "a title/menu/level-select screen, and NOT satisfied by a loaded game "
            "scene that only shows a HUD/shop/path/empty field with no entities yet — "
            "waiting on the player to press a \"start wave\"/\"play\"/\"begin\" "
            "control still counts as NOT YET. If the canvas ALREADY shows entities "
            'like that, respond ONLY as JSON: {"in_play": true}. Otherwise identify '
            "the SINGLE most likely click target to progress toward that state (a "
            "level/start card, a \"Play\"/\"Start\" button, a \"Start Wave\" button, "
            "etc.) and respond ONLY as JSON: "
            '{"in_play": false, "x": <int>, "y": <int>} where x and y are PIXEL '
            f"coordinates measured from the TOP-LEFT corner of this image (0, 0), "
            f"with x ranging from 0 to {canvas_w} and y ranging from 0 to {canvas_h} — "
            "not a normalized/percentage scale."
        )
        raw = vision_fn(path, prompt)
        data = json.loads(_extract_json(raw))
        if bool(data.get("in_play")):
            return True, None
        x, y = data.get("x"), data.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
                and not isinstance(x, bool) and not isinstance(y, bool):
            fx, fy = float(x), float(y)
            # Sanity-clamp: a vision model occasionally answers outside the image it
            # was shown (hallucination, or a provider normalizing to a 0-1000/0-1
            # scale instead of raw pixels despite the prompt). An out-of-bounds click
            # target is meaningless — treat it the same as "no instruction" rather
            # than sending a click to a point that was never on screen.
            if 0 <= fx <= canvas_w and 0 <= fy <= canvas_h:
                return False, (fx, fy)
        return False, None
    except Exception:  # noqa: BLE001 - a checker must never break a build
        return False, None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _vision_locate_with_retry(
    vision_fn: VisionFn | None,
    shot_fn: Callable[[], tuple[bytes | None, dict[str, Any] | None]],
    *, attempts: int = 2, wait_fn: Callable[[], None] | None = None,
) -> tuple[bool, tuple[float, float] | None, dict[str, Any] | None]:
    """Retry ``_vision_locate_start_click`` up to ``attempts`` times within ONE
    round before giving up — a single transient parse failure (a CLI backend
    wrapping JSON in prose despite instructions, a frame caught mid-transition)
    shouldn't burn a whole round of the caller's outer retry budget. ``shot_fn()``
    is called FRESH each attempt (the canvas may have settled further by the
    retry) and must return ``(screenshot_bytes, bounding_box_dict)`` — a
    ``(None, None)`` result (can't even screenshot) stops immediately, no point
    retrying. Returns ``(in_play, click, box)`` from the first attempt that
    resolves to an actual instruction (``in_play=True`` or ``click is not
    None``), or the LAST attempt's ``(False, None, box)`` after exhausting
    attempts. Never raises (mirrors ``_vision_locate_start_click``'s soft-skip
    posture)."""
    result: tuple[bool, tuple[float, float] | None] = (False, None)
    box: dict[str, Any] | None = None
    for i in range(max(1, attempts)):
        shot, box = shot_fn()
        if not shot or not box:
            return False, None, box
        result = _vision_locate_start_click(
            vision_fn, shot, int(box["width"]), int(box["height"]))
        if result[0] or result[1] is not None:
            return result[0], result[1], box
        if i < attempts - 1 and wait_fn is not None:
            wait_fn()
    return result[0], result[1], box


class VisualChecker:
    """Screenshot a URL + judge it against a goal. Soft-skips on any missing dep."""

    def __init__(self, event_bus: Any | None = None) -> None:
        self.event_bus = event_bus

    async def _emit(self, payload: dict[str, Any], cid: str | None) -> None:
        if self.event_bus is None:
            return
        try:
            await self.event_bus.emit(EventType.VISUAL_CHECK, "improve", payload, correlation_id=cid)
        except Exception:  # noqa: BLE001 - events never break the loop
            pass

    async def check(self, url: str, goal: str, *, vision_fn: VisionFn | None = None,
                    correlation_id: str | None = None) -> VisualVerdict:
        if not playwright_available():
            verdict = VisualVerdict(skipped=True, reason="playwright not installed")
        else:
            try:
                fd, path = tempfile.mkstemp(prefix="skyn3t-shot-", suffix=".png")
                os.close(fd)
            except Exception as exc:  # noqa: BLE001
                verdict = VisualVerdict(skipped=True, reason=f"temp file error: {exc}")
            else:
                try:
                    # The sync Playwright API refuses to run inside a live asyncio
                    # loop, and check() is always awaited from one — so run the
                    # capture in a worker thread (no running loop there).
                    shot = await asyncio.to_thread(screenshot, url, path)
                    if shot is None:
                        verdict = VisualVerdict(skipped=True, reason="screenshot failed")
                    else:
                        verdict = inspect(shot, goal, vision_fn=vision_fn)
                finally:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        await self._emit({"url": url, "goal": goal, **verdict.to_dict()}, correlation_id)
        return verdict
