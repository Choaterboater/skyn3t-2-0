# tests/test_visual_loop.py
"""Visual self-inspection loop (Spec 3, Slice 3): serve -> screenshot -> inspect
-> if visually wrong, improve toward the fix -> re-check. The loop is injected
with fakes (runner / checker / improver) so its control flow is testable without
a real browser, vision model, or build."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from skyn3t.studio.visual_check import LayoutAudit, VisualVerdict
from skyn3t.studio.visual_loop import VisualLoopResult, visual_self_improve


def test_screenshot_capture_is_offloaded_from_asyncio_loop(monkeypatch):
    # Regression: VisualChecker.check runs inside the event loop, but the sync
    # Playwright API refuses to run there — the capture must be thread-offloaded.
    from skyn3t.studio import visual_check
    from skyn3t.studio.visual_check import VisualChecker

    event_loop_thread = threading.get_ident()
    capture_threads = []

    def fake_capture(url, out_path, **kwargs):
        capture_threads.append(threading.get_ident())
        return out_path, None

    monkeypatch.setattr(visual_check, "playwright_available", lambda: True)
    monkeypatch.setattr(visual_check, "capture_visual_evidence", fake_capture, raising=False)
    verdict = asyncio.run(VisualChecker().check("http://example.test", "a page"))

    assert capture_threads
    assert capture_threads[0] != event_loop_thread
    assert verdict.reason == "no vision provider wired"


@pytest.mark.requires_loopback
def test_real_browser_can_capture_static_app(tmp_path):
    pytest.importorskip("playwright")
    from skyn3t.studio.app_runner import AppRunner as LiveAppRunner
    from skyn3t.studio.app_runner import cleanup_serve
    from skyn3t.studio.visual_check import VisualChecker, playwright_available

    if not playwright_available():
        pytest.skip("playwright not installed")
    (tmp_path / "index.html").write_text("<html><body><h1>hi</h1></body></html>")

    async def _go():
        runner = LiveAppRunner()
        app = await runner.start(tmp_path, "static", ready_timeout=15)
        if app.status != "running":
            pytest.skip("could not serve the static app")
        try:
            return await VisualChecker().check(app.url, "a page", vision_fn=None)
        finally:
            runner.stop(app)
            cleanup_serve(app)

    verdict = asyncio.run(_go())
    if verdict.reason == "screenshot failed":
        pytest.skip("Playwright Chromium is not installed or cannot launch")
    # Screenshot SUCCEEDED -> the only soft-skip is the (unwired) vision step.
    assert verdict.reason == "no vision provider wired"


class _App:
    def __init__(self, status="running", url="http://127.0.0.1:9/"):
        self.status = status
        self.url = url
        self.pid = None
        self.log_path = None


class _Runner:
    def __init__(self, app):
        self._app = app
        self.stops = 0

    async def start(self, project_dir, stack="", **kw):
        return self._app

    def stop(self, app):
        self.stops += 1


class _AsyncRunner(_Runner):
    async def stop(self, app):
        await asyncio.sleep(0)
        self.stops += 1


class _Checker:
    def __init__(self, verdicts):
        self._v = list(verdicts)
        self.calls = 0
        self.layout_profiles = []

    async def check(
        self,
        url,
        goal,
        *,
        vision_fn=None,
        correlation_id=None,
        layout_profile=None,
    ):
        self.layout_profiles.append(layout_profile)
        v = self._v[min(self.calls, len(self._v) - 1)]
        self.calls += 1
        return v


class _Improver:
    def __init__(self):
        self.goals = []

    async def improve(self, project, goal="", **kw):
        self.goals.append(goal)
        return SimpleNamespace(status="completed")


def _run(project_dir="/p", **kw):
    return asyncio.run(visual_self_improve(project_dir, "a clean landing page", **kw))


def test_passes_on_first_match_without_improving():
    checker = _Checker([VisualVerdict(matches=True, confidence=0.9)])
    imp = _Improver()
    res = _run(app_runner=_Runner(_App()), checker=checker, improve_engine=imp)
    assert isinstance(res, VisualLoopResult)
    assert res.passed is True and res.skipped is False
    assert imp.goals == []  # nothing to fix


def test_default_layout_profile_preserves_legacy_checker_signature():
    class _LegacyChecker:
        async def check(self, url, goal, *, vision_fn=None, correlation_id=None):
            return VisualVerdict(matches=True)

    res = _run(
        app_runner=_Runner(_App()),
        checker=_LegacyChecker(),
        improve_engine=_Improver(),
    )

    assert res.passed is True


def test_improves_then_passes():
    checker = _Checker([
        VisualVerdict(matches=False, issues=["button overlaps text"], fix_hint="fix the overlap"),
        VisualVerdict(matches=True, confidence=0.95),
    ])
    imp = _Improver()
    res = _run(app_runner=_Runner(_App()), checker=checker, improve_engine=imp, max_rounds=2)
    assert res.passed is True
    assert imp.goals == ["fix the overlap"]  # the fix_hint drove the improve
    assert len(res.rounds) == 2


def test_no_live_preview_soft_skips():
    checker = _Checker([VisualVerdict(matches=True)])
    imp = _Improver()
    res = _run(app_runner=_Runner(_App(status="no_preview")), checker=checker, improve_engine=imp)
    assert res.skipped is True and res.passed is False
    assert imp.goals == []  # never inspected -> never improved


def test_vision_unavailable_soft_skips():
    # checker returns a skipped verdict (no vision model) -> loop skips, no improve
    checker = _Checker([VisualVerdict(skipped=True, reason="no vision_fn")])
    imp = _Improver()
    res = _run(app_runner=_Runner(_App()), checker=checker, improve_engine=imp)
    assert res.skipped is True and imp.goals == []


def test_issues_remain_after_max_rounds():
    # never matches: improves up to max_rounds-1 times, then gives up (not passed)
    checker = _Checker([VisualVerdict(matches=False, issues=["bad colors"])])
    imp = _Improver()
    res = _run(app_runner=_Runner(_App()), checker=checker, improve_engine=imp, max_rounds=3)
    assert res.passed is False and res.skipped is False
    assert len(imp.goals) == 2  # improved after rounds 0 and 1, not after the last
    assert checker.calls == 3   # inspected 3 times


def test_layout_profile_is_forwarded_unchanged_to_every_check():
    from skyn3t.studio.layout_profiles import resolve_layout_profile

    profile = resolve_layout_profile(
        "dashboard", stack="react", engine="dom",
    ).to_dict()
    checker = _Checker([
        VisualVerdict(matches=False, issues=["under-filled"], fix_hint="use a split pane"),
        VisualVerdict(matches=True),
    ])

    res = _run(
        app_runner=_Runner(_App()),
        checker=checker,
        improve_engine=_Improver(),
        layout_profile=profile,
        max_rounds=2,
    )

    assert res.passed is True
    assert checker.layout_profiles == [profile, profile]
    assert checker.layout_profiles[0] is profile


def test_audit_issue_without_vision_drives_one_improve_and_records_evidence():
    audit = LayoutAudit(
        profile="workspace",
        viewport={"width": 1440, "height": 900},
        metrics={"fill_ratio": 0.45, "data_bearing_count": 0},
        issues=["Desktop workspace is under-filled."],
        fix_hint="Use the desktop workspace for a table/detail or split-pane composition.",
        skipped=False,
        reason="",
    )
    checker = _Checker([
        VisualVerdict(
            matches=False,
            issues=list(audit.issues),
            fix_hint=audit.fix_hint,
            layout_audit=audit,
            advisory_only=True,
        ),
        VisualVerdict(
            matches=False,
            issues=list(audit.issues),
            fix_hint=audit.fix_hint,
            layout_audit=audit,
            advisory_only=True,
        ),
    ])
    improver = _Improver()

    res = _run(
        app_runner=_Runner(_App()),
        checker=checker,
        improve_engine=improver,
        vision_fn=None,
        max_rounds=2,
    )

    assert len(improver.goals) == 1
    assert "split-pane" in improver.goals[0]
    assert res.skipped is False
    assert res.rounds[0].advisory_only is True
    assert res.rounds[0].layout_audit is audit
    assert res.to_dict()["rounds"][0]["layout_audit"]["profile"] == "workspace"


def test_runner_always_stopped_even_on_pass():
    runner = _Runner(_App())
    _run(app_runner=runner, checker=_Checker([VisualVerdict(matches=True)]), improve_engine=_Improver())
    assert runner.stops == 1  # the served app is torn down


def test_async_runner_stop_is_awaited():
    runner = _AsyncRunner(_App())
    _run(
        app_runner=runner,
        checker=_Checker([VisualVerdict(matches=True)]),
        improve_engine=_Improver(),
    )
    assert runner.stops == 1
