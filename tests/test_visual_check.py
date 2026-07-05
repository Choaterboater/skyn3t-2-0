# tests/test_visual_check.py
from __future__ import annotations

import asyncio
import sys
import types

from skyn3t.core.events import EventBus, EventType
from skyn3t.studio.visual_check import (
    VisualChecker,
    _dom_start_click,
    _fit_viewport_to_canvas,
    inspect,
    playwright_available,
    screenshot,
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


def test_screenshot_waits_for_client_hydration_before_capture(monkeypatch, tmp_path):
    import skyn3t.studio.visual_check as vc

    calls = []

    class FakePage:
        def goto(self, url, **kwargs):
            calls.append(("goto", url, kwargs))

        def wait_for_load_state(self, state, **kwargs):
            calls.append(("wait_for_load_state", state, kwargs))

        def wait_for_timeout(self, ms):
            calls.append(("wait_for_timeout", ms))

        def screenshot(self, **kwargs):
            calls.append(("screenshot", kwargs))

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def close(self):
            calls.append(("browser.close",))

    class FakeChromium:
        def launch(self):
            calls.append(("chromium.launch",))
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("playwright.exit",))

    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = lambda: FakePlaywright()
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.sync_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)
    monkeypatch.setattr(vc, "playwright_available", lambda: True)

    out = tmp_path / "shot.png"
    assert screenshot("http://example.test", str(out)) == str(out)
    assert calls[0] == ("chromium.launch",)
    assert calls[1][0] == "goto"
    assert calls[2][0] == "wait_for_load_state"
    assert calls[2][1] == "networkidle"
    assert calls[3][0] == "wait_for_timeout"
    assert calls[4][0] == "screenshot"


# ── _fit_viewport_to_canvas (fake page, no browser) ────────────────────────────
# A fixed-resolution game canvas (e.g. 1280x720) routinely exceeds the driver's default
# 800x600 viewport. The scaffold's CSS centers it in an `overflow:hidden` container with
# NO scrollbar, so Playwright's "scroll into view" has nothing to scroll — the clipped
# portion (often including the whole left/top of the canvas, at a NEGATIVE bounding-box
# x/y) is genuinely unreachable by click. This grows the viewport BEFORE any interaction
# so the canvas is never clipped in the first place — root cause of a game that plays
# fine but qa_playtest/game_visual_check couldn't click into (see
# memory/qa-gate-menu-driven-games.md).

class _FakeCanvasLocator:
    def __init__(self, result):
        self._result = result

    def evaluate(self, js):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeLocator:
    def __init__(self, result):
        self.first = _FakeCanvasLocator(result)


class _FakePage:
    def __init__(self, result):
        self._locator = _FakeLocator(result)
        self.viewport_calls: list[dict] = []

    def locator(self, selector):
        assert selector == "canvas"
        return self._locator

    def set_viewport_size(self, size):
        self.viewport_calls.append(size)


def test_fit_viewport_grows_for_larger_canvas():
    page = _FakePage({"w": 1280, "h": 720})
    _fit_viewport_to_canvas(page)
    assert len(page.viewport_calls) == 1
    size = page.viewport_calls[0]
    assert size["width"] >= 1280
    assert size["height"] >= 720


def test_fit_viewport_keeps_minimum_for_small_canvas():
    page = _FakePage({"w": 300, "h": 200})
    _fit_viewport_to_canvas(page, min_w=800, min_h=600)
    size = page.viewport_calls[0]
    assert size["width"] == 800
    assert size["height"] == 600


def test_fit_viewport_noop_on_zero_size_rect():
    page = _FakePage({"w": 0, "h": 0})
    _fit_viewport_to_canvas(page)
    assert page.viewport_calls == []


def test_fit_viewport_never_raises_when_evaluate_explodes():
    page = _FakePage(RuntimeError("no canvas yet"))
    _fit_viewport_to_canvas(page)  # must not raise
    assert page.viewport_calls == []


# ── _dom_start_click (fake page, no browser) ───────────────────────────────────
# DOM-first / vision-fallback, the pattern used by browser-use, Stagehand, and similar
# agent frameworks: many codegen UIs render the start/level-select screen as REAL HTML
# (a <button> overlaid on the canvas, not Phaser-drawn) — invisible to a canvas-only
# screenshot, but trivially and reliably findable via the accessibility tree/DOM text,
# at ZERO vision cost. Root cause of a SECOND false no_go (a fresh claude/sonnet build
# whose UI used an HTML `<button id="btn-start-game">Start Run</button>` overlay): the
# vision-only click loop never saw it because it only screenshots the canvas element.

class _FakeElement:
    def __init__(self, visible=True, click_exc=None):
        self.visible = visible
        self.click_exc = click_exc
        self.clicked = False

    def is_visible(self):
        return self.visible

    def click(self, timeout=None):
        if self.click_exc:
            raise self.click_exc
        self.clicked = True


class _FakeLocatorSet:
    def __init__(self, elements):
        self._elements = elements

    def count(self):
        return len(self._elements)

    def nth(self, i):
        return self._elements[i]


class _FakeDomPage:
    def __init__(self, by_role=None, by_text=None):
        self._by_role = by_role or {}
        self._by_text = by_text

    def get_by_role(self, role, name=None):
        return self._by_role.get(role, _FakeLocatorSet([]))

    def get_by_text(self, pattern):
        return self._by_text if self._by_text is not None else _FakeLocatorSet([])


def test_dom_start_click_clicks_a_visible_button_by_role():
    btn = _FakeElement()
    page = _FakeDomPage(by_role={"button": _FakeLocatorSet([btn])})
    assert _dom_start_click(page) is True
    assert btn.clicked is True


def test_dom_start_click_falls_back_to_link_role():
    link = _FakeElement()
    page = _FakeDomPage(by_role={"link": _FakeLocatorSet([link])})
    assert _dom_start_click(page) is True
    assert link.clicked is True


def test_dom_start_click_falls_back_to_text_match():
    el = _FakeElement()
    page = _FakeDomPage(by_text=_FakeLocatorSet([el]))
    assert _dom_start_click(page) is True
    assert el.clicked is True


def test_dom_start_click_skips_invisible_elements():
    invisible = _FakeElement(visible=False)
    visible = _FakeElement(visible=True)
    page = _FakeDomPage(by_role={"button": _FakeLocatorSet([invisible, visible])})
    assert _dom_start_click(page) is True
    assert invisible.clicked is False
    assert visible.clicked is True


def test_dom_start_click_returns_false_when_nothing_matches():
    page = _FakeDomPage()
    assert _dom_start_click(page) is False


def test_dom_start_click_never_raises_when_click_explodes():
    bad = _FakeElement(click_exc=RuntimeError("detached element"))
    good = _FakeElement()
    page = _FakeDomPage(by_role={"button": _FakeLocatorSet([bad, good])})
    assert _dom_start_click(page) is True  # recovers via the next candidate
    assert good.clicked is True


def test_dom_start_click_never_raises_when_page_explodes():
    class _ExplodingPage:
        def get_by_role(self, role, name=None):
            raise RuntimeError("page closed")

        def get_by_text(self, pattern):
            raise RuntimeError("page closed")

    assert _dom_start_click(_ExplodingPage()) is False


def test_check_skips_and_emits_when_screenshot_fails(monkeypatch):
    import skyn3t.studio.visual_check as vc
    monkeypatch.setattr(vc, "playwright_available", lambda: True)
    monkeypatch.setattr(vc, "screenshot", lambda url, path, **k: None)
    bus = EventBus()
    seen = []

    async def _h(ev):
        seen.append(ev.type)

    bus.subscribe(EventType.ALL, _h)
    checker = VisualChecker(event_bus=bus)
    v = asyncio.run(checker.check("http://127.0.0.1:9/", "g"))  # must NOT raise
    assert v.skipped and "screenshot failed" in v.reason
    assert EventType.VISUAL_CHECK in seen
