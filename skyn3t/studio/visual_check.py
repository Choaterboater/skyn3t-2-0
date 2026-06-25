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
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from skyn3t.core.events import EventType

VisionFn = Callable[[str, str], str]  # (image_path, prompt) -> raw model text

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


def make_vision_fn(settings: Any) -> VisionFn | None:
    """Build a vision_fn that judges a screenshot via OpenRouter, or None when no
    key is configured (the loop then soft-skips). This auto-activates the visual
    loop's judgement step wherever an OpenRouter key + vision model are available
    — closing the 'text-only LLM' gap without changing the loop."""
    key = str(getattr(settings, "openrouter_api_key", "") or "")
    if key:
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

    # No OpenRouter key: fall back to a vision-capable CLI on PATH (claude/kimi).
    # The CLI reads the image FILE (not base64 over HTTP), so it judges the
    # screenshot natively — no OpenRouter dependency anywhere.
    from skyn3t.adapters.llm import _no_mcp_args  # local import avoids any cycle

    provider = str(getattr(settings, "cli_llm_provider", "") or "claude").lower()
    for prov in (provider, "claude", "kimi"):
        if shutil.which(prov):
            def _cli_vision_fn(image_path: str, prompt: str, _prov: str = prov) -> str:
                full = (f"View the image file at {image_path}. {prompt} "
                        "Respond with ONLY the JSON object, no prose.")
                try:
                    out = subprocess.run([_prov, "-p", full, *_no_mcp_args(settings, _prov)],
                                         capture_output=True, text=True, timeout=120)
                    return out.stdout or ""
                except Exception:  # noqa: BLE001 - CLI missing/slow -> empty -> soft-skip
                    return ""
            return _cli_vision_fn
    return None


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
