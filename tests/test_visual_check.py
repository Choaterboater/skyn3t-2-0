# tests/test_visual_check.py
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.studio import visual_check as visual_check_module
from skyn3t.studio.layout_profiles import resolve_layout_profile
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
    monkeypatch.setattr(
        vc,
        "capture_visual_evidence",
        lambda url, out, **k: (out, None),
    )
    checker = VisualChecker()
    v = asyncio.run(checker.check("http://127.0.0.1:9/", "make it blue",
                                  vision_fn=lambda i, p: '{"matches": true, "confidence": 1.0}'))
    assert v.matches and not v.skipped


def test_normalize_layout_metrics_rejects_strings_booleans_and_non_finite_values():
    metrics = visual_check_module.normalize_layout_metrics({
        "viewport": {"width": "1440", "height": True},
        "fill_ratio": float("nan"),
        "repeated_cards": "6",
        "card_area_ratio": float("inf"),
        "data_bearing_count": False,
    })

    assert metrics == {
        "viewport_width": 0,
        "viewport_height": 0,
        "fill_ratio": 0.0,
        "repeated_cards": 0,
        "card_area_ratio": 0.0,
        "data_bearing_count": 0,
    }


def test_workspace_audit_accepts_healthy_data_bearing_composition():
    profile = resolve_layout_profile("dashboard")
    audit = visual_check_module.assess_layout(profile, {
        "viewport": {"width": 1440, "height": 900},
        "fill_ratio": 0.78,
        "repeated_cards": 3,
        "card_area_ratio": 0.34,
        "data_bearing_count": 7,
    })

    assert audit.skipped is False
    assert audit.issues == []
    assert audit.metrics["data_bearing_count"] == 7


def test_workspace_audit_flags_narrow_card_monoculture():
    profile = resolve_layout_profile("dashboard")
    audit = visual_check_module.assess_layout(profile, {
        "viewport": {"width": 1440, "height": 900},
        "fill_ratio": 0.48,
        "repeated_cards": 6,
        "card_area_ratio": 0.71,
        "data_bearing_count": 0,
    })

    assert audit.skipped is False
    assert "under-filled" in " ".join(audit.issues)
    assert "card" in " ".join(audit.issues).lower()
    assert "split" in audit.fix_hint.lower()


def test_workspace_audit_uses_strict_conservative_boundaries():
    profile = resolve_layout_profile("crud_app")
    at_boundaries = visual_check_module.assess_layout(profile, {
        "viewport": {"width": 1024, "height": 768},
        "fill_ratio": 0.62,
        "repeated_cards": 4,
        "card_area_ratio": 0.50,
        "data_bearing_count": 1,
    })
    over_boundaries = visual_check_module.assess_layout(profile, {
        "viewport": {"width": 1024, "height": 768},
        "fill_ratio": 0.63,
        "repeated_cards": 4,
        "card_area_ratio": 0.51,
        "data_bearing_count": 1,
    })

    assert at_boundaries.issues == []
    assert len(over_boundaries.issues) == 1
    assert "card" in over_boundaries.issues[0].lower()


def test_workspace_audit_does_not_fail_only_for_missing_data_bearing_elements():
    profile = resolve_layout_profile("saas_product")
    audit = visual_check_module.assess_layout(profile, {
        "viewport": {"width": 1280, "height": 800},
        "fill_ratio": 0.75,
        "repeated_cards": 2,
        "card_area_ratio": 0.20,
        "data_bearing_count": 0,
    })

    assert audit.issues == []
    assert audit.metrics["data_bearing_count"] == 0


def test_workspace_audit_skips_mobile_malformed_and_missing_capture():
    profile = resolve_layout_profile("data_viz")

    mobile = visual_check_module.assess_layout(profile, {
        "viewport": {"width": 768, "height": 900},
        "fill_ratio": 0.20,
        "repeated_cards": 8,
        "card_area_ratio": 0.90,
        "data_bearing_count": 0,
    })
    malformed = visual_check_module.assess_layout(
        profile,
        {"viewport": {"width": "wide", "height": 900}},
    )
    partial = visual_check_module.assess_layout(
        profile,
        {"viewport": {"width": 1440, "height": 900}},
    )
    invalid_aggregate = visual_check_module.assess_layout(
        profile,
        {
            "viewport": {"width": 1440, "height": 900},
            "fill_ratio": "0.5",
            "repeated_cards": 4,
            "card_area_ratio": 0.60,
            "data_bearing_count": 2,
        },
    )
    missing = visual_check_module.assess_layout(profile, None)

    assert mobile.skipped is True and mobile.reason == "mobile viewport"
    assert malformed.skipped is True and malformed.reason == "missing layout metrics"
    assert partial.skipped is True and partial.reason == "missing layout metrics"
    assert (
        invalid_aggregate.skipped is True
        and invalid_aggregate.reason == "missing layout metrics"
    )
    assert missing.skipped is True and missing.reason == "missing layout metrics"


def test_layout_metric_script_ignores_full_screen_painted_shell():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment-dependent binary
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(
                """
                <style>
                  html, body, #root, .shell { margin: 0; width: 100%; height: 100%; }
                  .shell { background: #eee; }
                  main { width: 420px; height: 800px; margin: auto; background: #fff; }
                  section { padding: 24px; }
                </style>
                <div id="root">
                  <div class="shell">
                    <main><section><h1>Operations</h1><p>Queue status</p></section></main>
                  </div>
                </div>
                """
            )
            raw = page.evaluate(visual_check_module._LAYOUT_METRICS_JS)
        finally:
            browser.close()

    audit = visual_check_module.assess_layout(
        resolve_layout_profile("dashboard"),
        raw,
    )
    assert raw["fill_ratio"] < 0.62
    assert "under-filled" in " ".join(audit.issues)


def test_layout_metric_script_prefers_qualifying_card_group_by_area():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment-dependent binary
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(
                """
                <style>
                  html, body, main { margin: 0; width: 1440px; height: 900px; }
                  main { position: relative; }
                  .large, .small { position: absolute; border-radius: 12px; }
                  .large { width: 430px; height: 415px; background: #123456; }
                  .small { width: 252px; height: 360px; background: #654321; }
                  .large:nth-of-type(1) { left: 0; top: 0; }
                  .large:nth-of-type(2) { left: 430px; top: 0; }
                  .large:nth-of-type(3) { left: 860px; top: 0; }
                  .large:nth-of-type(4) { left: 0; top: 415px; }
                  .small:nth-of-type(5) { left: 0; top: 540px; }
                  .small:nth-of-type(6) { left: 252px; top: 540px; }
                  .small:nth-of-type(7) { left: 504px; top: 540px; }
                  .small:nth-of-type(8) { left: 756px; top: 540px; }
                  .small:nth-of-type(9) { left: 1008px; top: 540px; }
                </style>
                <main>
                  <div class="large"></div><div class="large"></div>
                  <div class="large"></div><div class="large"></div>
                  <div class="small"></div><div class="small"></div>
                  <div class="small"></div><div class="small"></div>
                  <div class="small"></div>
                </main>
                """
            )
            raw = page.evaluate(visual_check_module._LAYOUT_METRICS_JS)
        finally:
            browser.close()

    audit = visual_check_module.assess_layout(
        resolve_layout_profile("dashboard"),
        raw,
    )
    assert raw["repeated_cards"] == 4
    assert raw["card_area_ratio"] > 0.50
    assert "similarly sized cards" in " ".join(audit.issues)


def test_non_workspace_profiles_are_explicitly_exempt():
    raw = {
        "viewport": {"width": 1440, "height": 900},
        "fill_ratio": 0.10,
        "repeated_cards": 20,
        "card_area_ratio": 0.95,
        "data_bearing_count": 0,
    }

    for app_type, expected in (
        ("landing_page", "editorial"),
        ("game", "immersive"),
        ("python_cli", "compact"),
    ):
        audit = visual_check_module.assess_layout(resolve_layout_profile(app_type), raw)
        assert audit.skipped is True
        assert expected in audit.reason
        assert audit.issues == []


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
        def new_page(self, **kwargs):
            calls.append(("new_page", kwargs))
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
    assert calls[1] == ("new_page", {})
    assert calls[2][0] == "goto"
    assert calls[3][0] == "wait_for_load_state"
    assert calls[3][1] == "networkidle"
    assert calls[4][0] == "wait_for_timeout"
    assert calls[5][0] == "screenshot"


def test_audited_capture_uses_desktop_viewport_and_returns_aggregate_metrics(
    monkeypatch, tmp_path,
):
    import skyn3t.studio.visual_check as vc

    calls = []
    raw_metrics = {
        "viewport": {"width": 1440, "height": 900},
        "fill_ratio": 0.73,
        "repeated_cards": 2,
        "card_area_ratio": 0.22,
        "data_bearing_count": 9,
    }

    class FakePage:
        def goto(self, url, **kwargs):
            calls.append(("goto", url, kwargs))

        def wait_for_load_state(self, state, **kwargs):
            calls.append(("wait_for_load_state", state, kwargs))

        def wait_for_timeout(self, ms):
            calls.append(("wait_for_timeout", ms))

        def screenshot(self, **kwargs):
            calls.append(("screenshot", kwargs))

        def evaluate(self, script):
            calls.append(("evaluate", script))
            return raw_metrics

    class FakeBrowser:
        def new_page(self, **kwargs):
            calls.append(("new_page", kwargs))
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

    out = tmp_path / "desktop.png"
    shot, metrics = visual_check_module.capture_visual_evidence(
        "http://example.test",
        str(out),
        audited_desktop=True,
    )

    assert shot == str(out)
    assert metrics == raw_metrics
    assert ("new_page", {"viewport": {"width": 1440, "height": 900}}) in calls
    assert [call[0] for call in calls].index("wait_for_timeout") < [
        call[0] for call in calls
    ].index("screenshot")
    assert [call[0] for call in calls].index("screenshot") < [
        call[0] for call in calls
    ].index("evaluate")


def test_workspace_audit_issue_overrides_soft_vision_skip(monkeypatch):
    import skyn3t.studio.visual_check as vc

    monkeypatch.setattr(vc, "playwright_available", lambda: True)
    monkeypatch.setattr(
        vc,
        "capture_visual_evidence",
        lambda *a, **k: (
            a[1],
            {
                "viewport": {"width": 1440, "height": 900},
                "fill_ratio": 0.40,
                "repeated_cards": 6,
                "card_area_ratio": 0.75,
                "data_bearing_count": 0,
            },
        ),
    )
    profile = resolve_layout_profile("dashboard").to_dict()

    verdict = asyncio.run(
        VisualChecker().check("http://example.test", "dashboard", layout_profile=profile)
    )

    assert verdict.skipped is False
    assert verdict.matches is False
    assert verdict.advisory_only is True
    assert verdict.layout_audit is not None
    assert verdict.layout_audit.issues == verdict.issues
    assert "split" in verdict.fix_hint.lower()


def test_clean_workspace_audit_preserves_soft_vision_skip(monkeypatch):
    import skyn3t.studio.visual_check as vc

    monkeypatch.setattr(vc, "playwright_available", lambda: True)
    monkeypatch.setattr(
        vc,
        "capture_visual_evidence",
        lambda *a, **k: (
            a[1],
            {
                "viewport": {"width": 1440, "height": 900},
                "fill_ratio": 0.80,
                "repeated_cards": 2,
                "card_area_ratio": 0.20,
                "data_bearing_count": 4,
            },
        ),
    )
    profile = resolve_layout_profile("dashboard").to_dict()

    verdict = asyncio.run(
        VisualChecker().check("http://example.test", "dashboard", layout_profile=profile)
    )

    assert verdict.skipped is True
    assert verdict.reason == "no vision provider wired"
    assert verdict.layout_audit is not None
    assert verdict.layout_audit.issues == []


def test_workspace_audit_does_not_mask_a_vision_failure(monkeypatch):
    import skyn3t.studio.visual_check as vc

    monkeypatch.setattr(vc, "playwright_available", lambda: True)
    monkeypatch.setattr(
        vc,
        "capture_visual_evidence",
        lambda *a, **k: (
            a[1],
            {
                "viewport": {"width": 1440, "height": 900},
                "fill_ratio": 0.40,
                "repeated_cards": 6,
                "card_area_ratio": 0.75,
                "data_bearing_count": 0,
            },
        ),
    )
    profile = resolve_layout_profile("dashboard").to_dict()

    verdict = asyncio.run(
        VisualChecker().check(
            "http://example.test",
            "dashboard",
            layout_profile=profile,
            vision_fn=lambda *_args: (
                '{"matches": false, "issues": ["broken contrast"], '
                '"fix_hint": "increase contrast"}'
            ),
        )
    )

    assert verdict.advisory_only is False
    assert verdict.issues[-1] == "broken contrast"


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
    monkeypatch.setattr(
        vc,
        "capture_visual_evidence",
        lambda url, path, **k: (None, None),
    )
    bus = EventBus()
    seen = []

    async def _h(ev):
        seen.append(ev.type)

    bus.subscribe(EventType.ALL, _h)
    checker = VisualChecker(event_bus=bus)
    v = asyncio.run(checker.check("http://127.0.0.1:9/", "g"))  # must NOT raise
    assert v.skipped and "screenshot failed" in v.reason
    assert EventType.VISUAL_CHECK in seen
