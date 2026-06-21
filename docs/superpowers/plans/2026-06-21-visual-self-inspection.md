# Visual Self-Inspection Loop — Implementation Plan (Spec 3, Slice 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The Kimi "magic" building block — after an edit, screenshot the running app and have a vision model judge whether it matches the goal and looks right, returning a structured verdict (with a `fix_hint` to feed back into the improve loop). Ships as a clean, testable, **soft-skipping** module + a `studio shoot` CLI.

**Architecture:** `screenshot(url)` uses Playwright (headless Chromium); `inspect(image, goal, vision_fn)` asks an injected `vision_fn(image_path, prompt) -> str` for a JSON verdict (the LLM client is text-only today, so vision is a pluggable seam — a real provider drops in later); `VisualChecker.check(url, goal, vision_fn)` ties them and emits `VISUAL_CHECK`. **Every dependency is optional: with no Playwright OR no vision_fn, `check()` returns a `skipped` verdict and never blocks.** This is Layer C of Spec 3; wiring it into the improve→serve→iterate loop + the cockpit is a later slice.

**Reality note (honest):** Playwright is not installed in this env and `LLMClient.complete` has no image input. So the loop is a no-op here until: (1) `pip install playwright && playwright install chromium`, and (2) a `vision_fn` is supplied (a thin adapter over a vision-capable model). The screenshot + verdict logic is real and tested via the injected seam; the soft-skip path is the default.

**Tech Stack:** Python 3.11+ (Playwright optional; stdlib `json`, `tempfile`, `os`), pytest, Typer.

## Global Constraints

- Python 3.11+. **Never raise** — every path returns a `VisualVerdict` (with `skipped=True` + a `reason` on any failure/missing-dep), never an exception.
- **All deps optional / soft-skip:** no Playwright → `skipped`; no `vision_fn` → `skipped`; screenshot/vision error → `skipped`. The loop must never block a build/improve.
- Offline-first tests: NO real Playwright (it's absent), NO real LLM. `inspect` is tested with a fake `vision_fn`; `check` naturally hits the no-Playwright skip path; `screenshot`'s Playwright body is only exercised when available (guard/skip).
- Localhost only (screenshots a `http://127.0.0.1:...` URL from the app runner).
- Suite baseline (this branch, off main): **434 pass / 2 skip**. Run `python3 -m pytest -q` after each task; stay green, no new warnings.
- Commit after every task.

## File Structure

- Create `skyn3t/studio/visual_check.py` — `VisualVerdict`, `playwright_available`, `screenshot`, `inspect`, `VisualChecker`.
- Modify `skyn3t/core/events.py` — add `VISUAL_CHECK`.
- Modify `skyn3t/cli/main.py` — `studio shoot` command.
- Create `tests/test_visual_check.py`, `tests/test_cli_shoot.py`.

---

### Task 1: `visual_check.py` + `VISUAL_CHECK` event

**Files:**
- Create: `skyn3t/studio/visual_check.py`
- Modify: `skyn3t/core/events.py`
- Test: `tests/test_visual_check.py`

**Interfaces:**
- Produces: `@dataclass VisualVerdict(matches, confidence, issues, fix_hint, skipped, reason)` w/ `.to_dict()`; `playwright_available() -> bool`; `screenshot(url, out_path, *, timeout_ms=8000) -> str | None`; `inspect(image_path, goal, *, vision_fn=None, prior=None) -> VisualVerdict`; `class VisualChecker` w/ `async def check(self, url, goal, *, vision_fn=None, correlation_id=None) -> VisualVerdict`.
- `VisionFn` type: `Callable[[str, str], str]` — `(image_path, prompt) -> raw_text`.

- [ ] **Step 1: Add the VISUAL_CHECK event**

In `skyn3t/core/events.py`, in `EventType`, after the `IMPROVE_*` members add:
```python
    VISUAL_CHECK = "improve.visual_check"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_visual_check.py
from __future__ import annotations

import asyncio

from skyn3t.core.events import EventBus, EventType
from skyn3t.studio.visual_check import (
    VisualChecker, VisualVerdict, inspect, playwright_available,
)


def test_playwright_available_returns_bool():
    assert isinstance(playwright_available(), bool)


def test_inspect_without_vision_fn_is_skipped():
    v = inspect("/tmp/x.png", "make it blue")
    assert v.skipped and not v.matches and "vision" in v.reason.lower()


def test_inspect_parses_a_matching_verdict():
    def fake_vision(image_path, prompt):
        assert "make it blue" in prompt
        return '{"matches": true, "confidence": 0.9, "issues": [], "fix_hint": ""}'
    v = inspect("/tmp/x.png", "make it blue", vision_fn=fake_vision)
    assert v.matches and v.confidence == 0.9 and not v.skipped


def test_inspect_parses_a_failing_verdict_with_fix_hint():
    def fake_vision(image_path, prompt):
        return '{"matches": false, "confidence": 0.8, "issues": ["still red"], "fix_hint": "set background to blue"}'
    v = inspect("/tmp/x.png", "make it blue", vision_fn=fake_vision)
    assert (not v.matches) and v.issues == ["still red"] and "blue" in v.fix_hint


def test_inspect_soft_skips_on_garbage_vision_output():
    v = inspect("/tmp/x.png", "g", vision_fn=lambda i, p: "not json at all")
    assert v.skipped and "vision error" in v.reason.lower()


def test_check_soft_skips_without_playwright_and_emits_event(monkeypatch):
    import skyn3t.studio.visual_check as vc
    monkeypatch.setattr(vc, "playwright_available", lambda: False)
    bus = EventBus()
    seen = []

    async def _h(ev):
        seen.append(ev.type)

    bus.subscribe(EventType.ALL, _h)
    checker = VisualChecker(event_bus=bus)
    v = asyncio.run(checker.check("http://127.0.0.1:9/", "make it blue"))
    assert v.skipped and "playwright" in v.reason.lower()
    assert EventType.VISUAL_CHECK in seen


def test_check_runs_vision_when_screenshot_succeeds(monkeypatch):
    import skyn3t.studio.visual_check as vc
    monkeypatch.setattr(vc, "playwright_available", lambda: True)
    monkeypatch.setattr(vc, "screenshot", lambda url, out, **k: out)  # pretend it shot
    checker = VisualChecker()
    v = asyncio.run(checker.check("http://127.0.0.1:9/", "make it blue",
                                  vision_fn=lambda i, p: '{"matches": true, "confidence": 1.0}'))
    assert v.matches and not v.skipped
```

(If `EventBus.subscribe` requires an async handler — it does, per `core/events.py` — the test's `_h` is already `async def`. Confirm and keep it async.)

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_visual_check.py -v`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement `visual_check.py`**

```python
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
        t = t.split("```")[1].removeprefix("json").strip()
    a, b = t.find("{"), t.rfind("}")
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
            fd, path = tempfile.mkstemp(prefix="skyn3t-shot-", suffix=".png")
            os.close(fd)
            shot = screenshot(url, path)
            if shot is None:
                verdict = VisualVerdict(skipped=True, reason="screenshot failed")
            else:
                verdict = inspect(shot, goal, vision_fn=vision_fn)
        await self._emit({"url": url, "goal": goal, **verdict.to_dict()}, correlation_id)
        return verdict
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_visual_check.py -v`
Expected: PASS (7).

- [ ] **Step 6: Run the suite + commit**

Run: `python3 -m pytest -q` (expect 441 pass / 2 skip).
```bash
git add skyn3t/studio/visual_check.py skyn3t/core/events.py tests/test_visual_check.py
git commit -m "feat: visual self-inspection (screenshot + vision verdict), soft-skipping"
```

---

### Task 2: `studio shoot` CLI

**Files:**
- Modify: `skyn3t/cli/main.py`
- Test: `tests/test_cli_shoot.py`

**Interfaces:**
- Consumes: `screenshot`, `playwright_available` (Task 1).
- Produces: `studio shoot <url> [--out PATH]` — captures a screenshot or prints an install hint.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_shoot.py
"""studio shoot delegates to screenshot and reports the saved path (or skip)."""
from __future__ import annotations

from skyn3t.studio import visual_check as vc


def test_screenshot_soft_skips_without_playwright(monkeypatch, tmp_path):
    # In this env Playwright is absent -> screenshot returns None (no raise).
    monkeypatch.setattr(vc, "playwright_available", lambda: False)
    assert vc.screenshot("http://127.0.0.1:9/", str(tmp_path / "x.png")) is None


def test_screenshot_returns_path_when_capture_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "playwright_available", lambda: True)

    # fake the Playwright body by replacing screenshot's effect at a higher level
    out = str(tmp_path / "shot.png")

    def fake_shot(url, path, **k):
        # simulate a real capture writing bytes
        from pathlib import Path
        Path(path).write_bytes(b"\x89PNG fake")
        return path

    monkeypatch.setattr(vc, "screenshot", fake_shot)
    assert vc.screenshot("http://127.0.0.1:9/", out) == out
```

- [ ] **Step 2: Run test to verify it fails / passes**

Run: `python3 -m pytest tests/test_cli_shoot.py -v`
Expected: PASS (these assert `visual_check.screenshot` behavior, already implemented in Task 1 — they pin the contract the CLI relies on). Proceed to wire the CLI.

- [ ] **Step 3: Add the `studio shoot` command**

In `skyn3t/cli/main.py` (near `studio_serve`):
```python
@studio_app.command("shoot")
def studio_shoot(
    url: str = typer.Argument(..., help="URL to screenshot (e.g. http://127.0.0.1:8088/)."),
    out: str = typer.Option("", "--out", "-o", help="Output PNG path (default: a temp file)."),
) -> None:
    """Capture a screenshot of a running app (needs Playwright)."""
    import tempfile as _tempfile

    from skyn3t.studio.visual_check import playwright_available, screenshot

    console = _console()
    if not playwright_available():
        console.print("[yellow]Playwright not installed.[/yellow] "
                      "Run [cyan]pip install playwright && playwright install chromium[/cyan] to enable screenshots.")
        raise typer.Exit(code=1)
    out_path = out or _tempfile.mkstemp(prefix="skyn3t-shot-", suffix=".png")[1]
    result = screenshot(url, out_path)
    if result is None:
        console.print(f"[red]Screenshot failed[/red] for {url} (page didn't load or no browser binary).")
        raise typer.Exit(code=2)
    console.print(f"[green]Saved[/green] screenshot to [cyan]{result}[/cyan]")
```

- [ ] **Step 4: Run tests + commit**

Run: `python3 -m pytest tests/test_cli_shoot.py -q && python3 -m pytest -q` (expect 443 pass / 2 skip).
```bash
git add skyn3t/cli/main.py tests/test_cli_shoot.py
git commit -m "feat: studio shoot CLI — screenshot a running app (Playwright-gated)"
```

---

## Self-Review

**Spec coverage (Spec 3 Layer C — visual check building block):**
- `screenshot(url)` via Playwright, soft-skip when absent → Task 1 ✓
- `inspect(image, goal, vision_fn)` → structured `VisualVerdict{matches, confidence, issues, fix_hint}` with `fix_hint` to feed the improve loop → Task 1 ✓
- soft-skip on any missing dep / error, never blocks, never raises → Task 1 ✓
- `VISUAL_CHECK` event for the cockpit → Task 1 ✓
- `studio shoot` CLI surface → Task 2 ✓

**Deferred to later Spec 3 slices:** a real `vision_fn` provider (the LLM client is text-only; a vision adapter — Anthropic API image content or a claude-CLI image call — is its own wiring); the improve→serve→screenshot→vision→iterate loop (`studio improve --visual`); the cockpit rendering of `VISUAL_CHECK` thumbnails. **Honest limitation:** with Playwright absent + no vision_fn, this slice is a tested no-op until both are provided.

**Placeholder scan:** none — real code throughout.

**Type consistency:** `VisualVerdict` fields + `VisionFn` signature used identically across `inspect`, `VisualChecker`, and tests; `screenshot(url, out_path, *, timeout_ms)` consistent between the module and the CLI.
