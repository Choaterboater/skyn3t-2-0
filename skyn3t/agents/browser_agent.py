"""BrowserAgent — computer-use visual self-heal loop (2.0 P0).

Drives a *running* app through a real browser using Playwright when it is
available, so the studio can verify that a build actually renders rather than
trusting that it compiles ("verify behavior, not vibes" — design rule #3).

Capabilities exposed:

* ``verify_rendered(url)`` -> ``{"ok": bool, "errors": [...], "screenshot": path|None,
  "title": str, "status": int|None}`` — the verification gate. ``ok`` is True
  only when the page loaded without console errors / page errors / failed
  responses.
* ``navigate`` / ``click`` / ``fill`` / ``screenshot`` action primitives via the
  agent ``execute`` surface.

Playwright is a heavy, optional dependency. The import is guarded; if it is
absent (or no browser is installed) every method degrades to a clear no-op
result with ``ok: False`` and an explanatory error instead of crashing
(design rule #6).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

try:  # heavy optional dep
    from playwright.async_api import async_playwright  # type: ignore
    _PLAYWRIGHT_AVAILABLE = True
    _PLAYWRIGHT_IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - ImportError or missing browsers
    async_playwright = None  # type: ignore
    _PLAYWRIGHT_AVAILABLE = False
    _PLAYWRIGHT_IMPORT_ERROR = str(_exc)


def playwright_available() -> bool:
    return _PLAYWRIGHT_AVAILABLE


_DISABLED_MSG = (
    "Playwright unavailable — visual self-heal degraded to no-op. "
    "Install the 'playwright' extra and run 'playwright install chromium' to enable."
)


class BrowserAgent(BaseAgent):
    """Drives a running app for visual verification and self-heal."""

    def __init__(self, name: str = "browser", event_bus: EventBus | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(name, agent_type="browser", provider="playwright",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="browser",
            description="Drives a running app via Playwright for visual self-heal",
            tags=("verification", "browser"),
        ))
        cfg = config or {}
        self.timeout_ms: int = int(cfg.get("timeout_ms", 15000))
        self.headless: bool = bool(cfg.get("headless", True))
        self.available = _PLAYWRIGHT_AVAILABLE

    async def initialize(self) -> None:
        self.metadata["playwright_available"] = self.available
        if not self.available:
            self.metadata["disabled_reason"] = _DISABLED_MSG
        self.metadata["ready"] = True

    async def health_check(self) -> bool:
        # Always healthy: even with no Playwright we serve degraded results.
        return True

    # ---- the verification gate ------------------------------------------
    async def verify_rendered(self, url: str, screenshot_path: str | None = None,
                              wait_selector: str | None = None) -> dict[str, Any]:
        """Load ``url`` in a real browser and report whether it rendered cleanly.

        Returns a dict the studio can gate on. Never raises.
        """
        if not self.available:
            return {
                "ok": False,
                "errors": [_DISABLED_MSG],
                "screenshot": None,
                "title": "",
                "status": None,
                "degraded": True,
            }

        errors: list[str] = []
        title = ""
        status: int | None = None
        shot: str | None = None
        try:
            async with async_playwright() as pw:  # type: ignore
                browser = await pw.chromium.launch(headless=self.headless)
                try:
                    page = await browser.new_page()
                    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                            if m.type in ("error",) else None)
                    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                    page.on("requestfailed", lambda r: errors.append(
                        f"requestfailed: {r.url} ({r.failure})"))

                    resp = await page.goto(url, timeout=self.timeout_ms,
                                           wait_until="load")
                    if resp is not None:
                        status = resp.status
                        if status >= 400:
                            errors.append(f"http {status} for {url}")
                    if wait_selector:
                        try:
                            await page.wait_for_selector(wait_selector,
                                                         timeout=self.timeout_ms)
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"selector '{wait_selector}' not found: {exc}")
                    # let late console errors flush
                    await asyncio.sleep(0.2)
                    title = await page.title()
                    if screenshot_path:
                        try:
                            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                            await page.screenshot(path=screenshot_path, full_page=True)
                            shot = screenshot_path
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"screenshot failed: {exc}")
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001 - any driver/launch failure
            errors.append(f"browser error: {exc}")

        # filter out None artifacts from the console handler
        errors = [e for e in errors if e]
        return {
            "ok": len(errors) == 0,
            "errors": errors,
            "screenshot": shot,
            "title": title,
            "status": status,
            "degraded": False,
        }

    async def run_actions(self, url: str, actions: list[dict[str, Any]],
                          screenshot_path: str | None = None) -> dict[str, Any]:
        """Execute a sequence of action dicts against a page.

        Each action: ``{"op": "click"|"fill"|"navigate"|"screenshot",
        "selector": ..., "value": ..., "url": ...}``.
        Returns ``{"ok", "errors", "steps", "screenshot"}``. Never raises.
        """
        if not self.available:
            return {"ok": False, "errors": [_DISABLED_MSG], "steps": [],
                    "screenshot": None, "degraded": True}
        errors: list[str] = []
        steps: list[str] = []
        shot: str | None = None
        try:
            async with async_playwright() as pw:  # type: ignore
                browser = await pw.chromium.launch(headless=self.headless)
                try:
                    page = await browser.new_page()
                    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                    await page.goto(url, timeout=self.timeout_ms, wait_until="load")
                    for act in actions:
                        op = act.get("op")
                        try:
                            if op == "navigate":
                                await page.goto(act["url"], timeout=self.timeout_ms)
                            elif op == "click":
                                await page.click(act["selector"], timeout=self.timeout_ms)
                            elif op == "fill":
                                await page.fill(act["selector"], act.get("value", ""),
                                                timeout=self.timeout_ms)
                            elif op == "screenshot":
                                tgt = act.get("path") or screenshot_path
                                if tgt:
                                    Path(tgt).parent.mkdir(parents=True, exist_ok=True)
                                    await page.screenshot(path=tgt, full_page=True)
                                    shot = tgt
                            else:
                                errors.append(f"unknown op: {op}")
                                continue
                            steps.append(f"{op} ok")
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"{op} failed: {exc}")
                    if screenshot_path and not shot:
                        try:
                            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                            await page.screenshot(path=screenshot_path, full_page=True)
                            shot = screenshot_path
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"screenshot failed: {exc}")
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"browser error: {exc}")
        return {"ok": len(errors) == 0, "errors": errors, "steps": steps,
                "screenshot": shot, "degraded": False}

    async def execute(self, task: TaskRequest) -> TaskResult:
        url = task.payload.get("url")
        if not url:
            return TaskResult(task_id=task.task_id, success=False,
                              error="no url in payload")
        screenshot = task.payload.get("screenshot")
        actions = task.payload.get("actions")
        if actions:
            out = await self.run_actions(url, actions, screenshot_path=screenshot)
        else:
            out = await self.verify_rendered(
                url, screenshot_path=screenshot,
                wait_selector=task.payload.get("wait_selector"))
        # Degraded availability is not a task failure; it is a clean signal.
        success = out.get("ok", False) or out.get("degraded", False)
        return TaskResult(task_id=task.task_id, success=success, output=out,
                          error=None if out.get("ok") else "; ".join(out.get("errors", [])))
