# skyn3t/studio/visual_check.py
"""Visual self-inspection (Spec 3, Slice 3): screenshot a running app and ask a
vision model whether it matches the goal. EVERY dependency is optional — with no
Playwright or no vision_fn, check() returns a `skipped` verdict and never blocks.
Never raises."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from skyn3t.core.events import EventType

VisionFn = Callable[[str, str], str]  # (image_path, prompt) -> raw model text


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
                    shot = screenshot(url, path)
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
