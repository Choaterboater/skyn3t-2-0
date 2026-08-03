from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from skyn3t.studio import visual_proof as vp
from skyn3t.studio.visual_design_contract import derive_visual_design_contract
from skyn3t.studio.visual_proof import (
    DEFAULT_VIEWPORTS,
    VISUAL_PROOF_SCHEMA_VERSION,
    ViewportSpec,
    analyze_viewport_snapshot,
    audit_responsive_page,
)


def _snapshot(**overrides):
    base = {
        "ready_state": "complete",
        "target_kind": "main",
        "body_text_chars": 180,
        "main_text_chars": 120,
        "visible_controls": 2,
        "visible_media": 1,
        "visible_elements": 18,
        "image_count": 1,
        "broken_images": [],
        "canvas_count": 0,
        "canvas_area_ratio": 0.0,
        "canvases": [],
        "client_width": 390,
        "client_height": 844,
        "scroll_width": 390,
        "overflow_elements": [],
        "overlaps": [],
    }
    base.update(overrides)
    return base


def _codes(issues):
    return {issue.code for issue in issues}


def test_default_viewports_pin_desktop_and_mobile_evidence():
    assert [(v.name, v.width, v.height) for v in DEFAULT_VIEWPORTS] == [
        ("desktop", 1440, 900),
        ("mobile", 390, 844),
    ]


def test_clean_snapshot_passes_deterministic_analysis():
    metrics, issues = analyze_viewport_snapshot(_snapshot(), stack="react")

    assert issues == []
    assert metrics["horizontal_overflow_px"] == 0
    assert metrics["high_confidence_overlaps"] == 0


def test_visual_design_contract_lints_root_tokens_heading_and_mobile_controls():
    contract = derive_visual_design_contract("A calm bakery site")
    clean_snapshot = _snapshot(
        root_tokens=dict(contract["tokens"]),
        typography={
            "has_heading": True,
            "heading_font_family": f"{contract['typography']['heading']}, serif",
        },
        interactive_controls=[{"tag": "button", "label": "Order", "width": 120, "height": 40}],
    )

    metrics, issues = analyze_viewport_snapshot(
        clean_snapshot, stack="react", design_contract=contract
    )

    assert issues == []
    assert metrics["visual_design_contract"]["checked"] is True
    assert metrics["visual_design_contract"]["contract_id"] == contract["contract_id"]

    invalid_snapshot = _snapshot(
        root_tokens={"--bg": "#fff"},
        typography={"has_heading": True, "heading_font_family": "Arial, sans-serif"},
        interactive_controls=[{"tag": "button", "label": "Order", "width": 32, "height": 30}],
    )
    _, issues = analyze_viewport_snapshot(
        invalid_snapshot, stack="react", design_contract=contract
    )

    assert {
        "design_contract_tokens_missing",
        "design_contract_heading_font_missing",
        "design_contract_small_controls",
    }.issubset(_codes(issues))


def test_blank_broken_and_overflow_findings_are_independent():
    metrics, issues = analyze_viewport_snapshot(_snapshot(
        main_text_chars=3,
        visible_controls=0,
        visible_media=0,
        visible_elements=1,
        image_count=1,
        broken_images=[{"src": "/hero.webp", "alt": "Hero", "complete": True}],
        scroll_width=430,
        overflow_elements=[{"tag": "section", "right": 430, "width": 430}],
    ))

    assert _codes(issues) == {
        "blank_or_near_empty",
        "broken_images",
        "horizontal_overflow",
    }
    assert metrics["horizontal_overflow_px"] == 40


def test_two_pixel_rounding_drift_is_not_horizontal_overflow():
    _, issues = analyze_viewport_snapshot(_snapshot(scroll_width=392, client_width=390))

    assert "horizontal_overflow" not in _codes(issues)


def test_phaser_canvas_suppresses_only_canvas_blank_and_overflow_false_positives():
    metrics, issues = analyze_viewport_snapshot(_snapshot(
        body_text_chars=0,
        main_text_chars=0,
        visible_controls=0,
        visible_media=0,
        visible_elements=1,
        canvas_count=1,
        canvas_area_ratio=0.8,
        canvases=[{"readable": False, "nonblank": None}],
        scroll_width=1280,
        overflow_elements=[{"tag": "canvas", "right": 1280, "width": 1280}],
    ), stack="phaser")

    assert issues == []
    assert metrics["suppressed_game_canvas_overflow"] is True


def test_canvas_suppression_does_not_hide_broken_images_or_runtime_findings():
    _, issues = analyze_viewport_snapshot(_snapshot(
        main_text_chars=0,
        visible_controls=0,
        visible_media=0,
        visible_elements=1,
        canvas_count=1,
        canvas_area_ratio=0.8,
        canvases=[{"readable": False, "nonblank": None}],
        broken_images=[{"src": "/sprite.webp", "complete": True}],
        scroll_width=1280,
        overflow_elements=[{"tag": "canvas", "right": 1280, "width": 1280}],
    ), stack="phaser")

    assert _codes(issues) == {"broken_images"}


def test_readable_blank_non_game_canvas_does_not_certify_content():
    _, issues = analyze_viewport_snapshot(_snapshot(
        main_text_chars=0,
        visible_controls=0,
        visible_media=0,
        visible_elements=1,
        canvas_count=1,
        canvas_area_ratio=0.8,
        canvases=[{"readable": True, "nonblank": False}],
    ), stack="react")

    assert "blank_or_near_empty" in _codes(issues)


def test_empty_wrapper_divs_do_not_certify_content():
    _, issues = analyze_viewport_snapshot(_snapshot(
        main_text_chars=0,
        visible_controls=0,
        visible_media=0,
        visible_elements=20,
    ))

    assert "blank_or_near_empty" in _codes(issues)


def test_phaser_canvas_container_is_a_canvas_only_overflow_culprit():
    metrics, issues = analyze_viewport_snapshot(_snapshot(
        main_text_chars=0,
        visible_controls=0,
        visible_media=0,
        visible_elements=2,
        canvas_count=1,
        canvas_area_ratio=0.8,
        canvases=[{"readable": False, "nonblank": None}],
        scroll_width=1280,
        overflow_elements=[
            {"tag": "div", "contains_canvas": True, "right": 1280, "width": 1280},
            {"tag": "canvas", "right": 1280, "width": 1280},
        ],
    ), stack="phaser")

    assert issues == []
    assert metrics["suppressed_game_canvas_overflow"] is True


def _overlap(**overrides):
    pair = {
        "a": {"tag": "button", "text": "Save", "interactive": True},
        "b": {"tag": "p", "text": "Account details", "interactive": False},
        "intersection_ratio": 0.72,
        "intersection_width": 80,
        "intersection_height": 24,
        "smaller_area": 1920,
        "same_parent": True,
        "positioned": False,
        "transformed": False,
        "overlay_scope": False,
        "canvas_intersection": False,
    }
    pair.update(overrides)
    return pair


def test_high_confidence_normal_flow_overlap_is_reported():
    metrics, issues = analyze_viewport_snapshot(_snapshot(overlaps=[_overlap()]))

    assert _codes(issues) == {"incoherent_overlap"}
    assert metrics["high_confidence_overlaps"] == 1


def test_overlapping_duplicate_interactive_controls_are_still_reported():
    pair = _overlap(
        b={"tag": "button", "text": "Save", "interactive": True},
    )
    _, issues = analyze_viewport_snapshot(_snapshot(overlaps=[pair]))

    assert "incoherent_overlap" in _codes(issues)


@pytest.mark.parametrize("suppression", [
    {"positioned": True},
    {"transformed": True},
    {"overlay_scope": True},
    {"intersection_ratio": 0.44},
    {"intersection_width": 5},
    {"smaller_area": 99},
    {"b": {"tag": "span", "text": "Save", "interactive": False}},
])
def test_overlap_precision_safeguards_suppress_ambiguous_composition(suppression):
    metrics, issues = analyze_viewport_snapshot(_snapshot(overlaps=[_overlap(**suppression)]))

    assert "incoherent_overlap" not in _codes(issues)
    assert metrics["high_confidence_overlaps"] == 0


class _Response:
    status = 200
    url = "http://example.test/"


class _Message:
    def __init__(self, text: str, url: str = ""):
        self.type = "error"
        self.text = text
        self.location = {"url": url}


class _FakePage:
    def __init__(self, snapshot, *, emit_errors=False, screenshot_error=None):
        self.snapshot = snapshot
        self.emit_errors = emit_errors
        self.screenshot_error = screenshot_error
        self.handlers = {}
        self.closed = False

    def on(self, event, handler):
        self.handlers[event] = handler

    def goto(self, url, **kwargs):
        if self.emit_errors:
            self.handlers["console"](_Message("application exploded", url + "app.js"))
            self.handlers["pageerror"](RuntimeError("render exploded"))
        return _Response()

    def wait_for_load_state(self, state, **kwargs):
        return None

    def wait_for_timeout(self, timeout):
        return None

    def evaluate(self, script):
        if "scrollIntoView" in script:
            return 0
        if "getAnimations" in script:
            return 0
        assert "broken_images" in script
        if isinstance(self.snapshot, BaseException):
            raise self.snapshot
        return self.snapshot

    def screenshot(self, *, path, full_page):
        assert full_page is True
        if self.screenshot_error:
            raise self.screenshot_error
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nproof")

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, pages):
        self.pages = list(pages)
        self.viewports = []
        self.closed = False

    def new_page(self, *, viewport):
        self.viewports.append(viewport)
        return self.pages.pop(0)

    def close(self):
        self.closed = True


def _install_fake_playwright(monkeypatch, browser=None, launch_error=None):
    launches = []

    class _Chromium:
        def launch(self):
            launches.append(True)
            if launch_error:
                raise launch_error
            return browser

    class _Playwright:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = lambda: _Playwright()
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.sync_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)
    monkeypatch.setattr(vp, "playwright_available", lambda: True)
    return launches


def test_batch_uses_one_browser_and_serializes_both_viewports(monkeypatch, tmp_path):
    browser = _FakeBrowser([_FakePage(_snapshot()), _FakePage(_snapshot())])
    launches = _install_fake_playwright(monkeypatch, browser=browser)

    proof = audit_responsive_page("http://example.test/", tmp_path, stack="react")

    assert proof.passed is True and proof.skipped is False
    assert launches == [True]
    assert browser.viewports == [
        {"width": 1440, "height": 900},
        {"width": 390, "height": 844},
    ]
    assert all((tmp_path / viewport.screenshot).is_file() for viewport in proof.viewports)
    report = json.loads((tmp_path / "visual-proof.json").read_text())
    assert report["schema_version"] == VISUAL_PROOF_SCHEMA_VERSION
    assert report["passed"] is True
    route_report = json.loads((tmp_path / proof.report_path).read_text())
    assert [viewport["name"] for viewport in route_report["viewports"]] == [
        "desktop", "mobile",
    ]


def test_missing_playwright_is_honest_skip_with_report(monkeypatch, tmp_path):
    monkeypatch.setattr(vp, "playwright_available", lambda: False)

    proof = audit_responsive_page("http://example.test/", tmp_path)

    assert proof.skipped is True and proof.passed is False
    assert all(viewport.skipped and not viewport.passed for viewport in proof.viewports)
    report = json.loads((tmp_path / "visual-proof.json").read_text())
    assert report["skipped"] is True and report["passed"] is False


def test_new_batch_removes_only_route_dirs_from_previous_report(monkeypatch, tmp_path):
    old_route = tmp_path / "old-route-deadbeef"
    old_route.mkdir()
    (old_route / "desktop.png").write_bytes(b"old")
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()
    (unrelated / "owner.txt").write_text("user artifact")
    (tmp_path / "visual-proof.json").write_text(json.dumps({
        "routes": [{"report_path": "old-route-deadbeef/report.json"}],
    }))
    monkeypatch.setattr(vp, "playwright_available", lambda: False)

    audit_responsive_page("http://example.test/", tmp_path)

    assert not old_route.exists()
    assert (unrelated / "owner.txt").read_text() == "user artifact"


def test_previous_report_cleanup_refuses_route_directory_symlink(monkeypatch, tmp_path):
    target = tmp_path / "target-route-deadbeef"
    target.mkdir()
    (target / "owner.txt").write_text("keep", encoding="utf-8")
    linked = tmp_path / "linked-route-cafebabe"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    (tmp_path / "visual-proof.json").write_text(json.dumps({
        "routes": [{"report_path": "linked-route-cafebabe/report.json"}],
    }))
    monkeypatch.setattr(vp, "playwright_available", lambda: False)

    audit_responsive_page("http://example.test/", tmp_path)

    assert linked.is_symlink()
    assert (target / "owner.txt").read_text(encoding="utf-8") == "keep"


def test_missing_chromium_is_honest_skip_with_reason(monkeypatch, tmp_path):
    _install_fake_playwright(
        monkeypatch,
        launch_error=RuntimeError("Executable does not exist; install chromium"),
    )

    proof = audit_responsive_page("http://example.test/", tmp_path)

    assert proof.skipped is True and proof.passed is False
    assert "chromium unavailable" in proof.reason
    assert "install chromium" in proof.reason


def test_console_and_page_errors_fail_each_viewport(monkeypatch, tmp_path):
    browser = _FakeBrowser([
        _FakePage(_snapshot(), emit_errors=True),
        _FakePage(_snapshot(), emit_errors=True),
    ])
    _install_fake_playwright(monkeypatch, browser=browser)

    proof = audit_responsive_page("http://example.test/", tmp_path)

    assert proof.passed is False and proof.skipped is False
    for viewport in proof.viewports:
        assert _codes(viewport.issues) == {"console_errors", "page_errors"}
        assert viewport.console_errors == ["application exploded (http://example.test/app.js)"]
        assert viewport.page_errors == ["render exploded"]


def test_partial_dom_audit_is_an_honest_incomplete_skip(monkeypatch, tmp_path):
    browser = _FakeBrowser([
        _FakePage(_snapshot()),
        _FakePage(RuntimeError("execution context destroyed")),
    ])
    _install_fake_playwright(monkeypatch, browser=browser)

    proof = audit_responsive_page("http://example.test/", tmp_path)

    assert proof.passed is False and proof.skipped is True
    assert proof.viewports[0].passed is True
    assert proof.viewports[1].skipped is True
    assert "required viewports" in proof.reason


def test_screenshot_failure_is_a_failed_proof_not_a_pass(monkeypatch, tmp_path):
    browser = _FakeBrowser([
        _FakePage(_snapshot(), screenshot_error=OSError("disk full")),
        _FakePage(_snapshot()),
    ])
    _install_fake_playwright(monkeypatch, browser=browser)

    proof = audit_responsive_page("http://example.test/", tmp_path)

    assert proof.passed is False and proof.skipped is True
    assert "screenshot_failed" in _codes(proof.viewports[0].issues)


def test_json_artifact_failure_cannot_return_a_passing_proof(monkeypatch, tmp_path):
    browser = _FakeBrowser([_FakePage(_snapshot()), _FakePage(_snapshot())])
    _install_fake_playwright(monkeypatch, browser=browser)
    monkeypatch.setattr(vp, "_write_json", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("read-only artifact directory")
    ))

    proof = audit_responsive_page("http://example.test/", tmp_path)

    assert proof.passed is False and proof.skipped is True
    assert all("artifact_write_failed" in _codes(viewport.issues)
               for viewport in proof.viewports)


def test_console_filter_ignores_only_known_browser_noise_and_favicon():
    assert vp._console_message(_Message("ResizeObserver loop limit exceeded")) is None
    assert vp._console_message(_Message("404", "http://example.test/favicon.ico")) is None
    assert vp._console_message(_Message("TypeError: boom", "http://example.test/app.js")) == (
        "TypeError: boom (http://example.test/app.js)"
    )


def test_custom_viewport_is_reflected_in_batch_report(monkeypatch, tmp_path):
    browser = _FakeBrowser([_FakePage(_snapshot(client_width=800, scroll_width=800))])
    _install_fake_playwright(monkeypatch, browser=browser)

    proof = audit_responsive_page(
        "http://example.test/",
        tmp_path,
        viewports=[ViewportSpec("tablet", 800, 1024)],
    )

    assert proof.passed is True
    report = json.loads((tmp_path / "visual-proof.json").read_text())
    assert report["viewports"] == [{"name": "tablet", "width": 800, "height": 1024}]
