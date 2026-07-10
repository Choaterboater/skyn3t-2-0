# tests/test_liveness_loop.py
import asyncio
from types import SimpleNamespace

import skyn3t.studio.liveness as lv
from skyn3t.studio.liveness import (
    LivenessReport,
    RouteResult,
    _repair_goal,
    liveness_self_improve,
)


class _App:
    def __init__(self, url):
        self._url = url

    async def start(self, project_dir, stack=""):
        return SimpleNamespace(status="running", url=self._url)

    def stop(self, app):
        ...


def test_skipped_when_no_preview(tmp_path):
    stopped = {"stop": 0, "cleanup": 0}

    class _NoApp:
        async def start(self, *a, **k):
            return SimpleNamespace(status="failed", url="", pid=123, log_path="x")

        def stop(self, app):
            stopped["stop"] += 1

    def fake_cleanup(app):
        stopped["cleanup"] += 1

    old_cleanup = lv.cleanup_serve
    lv.cleanup_serve = fake_cleanup
    try:
        out = asyncio.run(liveness_self_improve(tmp_path, app_runner=_NoApp(),
                                                improve_engine=None, max_rounds=1))
    finally:
        lv.cleanup_serve = old_cleanup
    assert out.skipped is True
    assert stopped == {"stop": 1, "cleanup": 1}


def test_healthy_first_round_no_improve(tmp_path, monkeypatch):
    seen = {}

    async def fake_check(base, routes, **k):
        seen.update(k)
        return LivenessReport(results=[RouteResult("/", "GET", 200, True, "page")],
                              total=1, ok=1, dead=0, dead_routes=[], health=1.0)
    monkeypatch.setattr(lv, "check_liveness", fake_check)

    class _Improve:
        async def improve(self, *a, **k):
            raise AssertionError("should not improve a healthy app")

    out = asyncio.run(liveness_self_improve(tmp_path, app_runner=_App("http://127.0.0.1:1"),
                                            improve_engine=_Improve(), max_rounds=2))
    assert out.passed is True and out.rounds == 1
    assert seen["artifact_dir_label"] == ".skyn3t/visual-proof"
    assert seen["screenshot_dir"] == str(tmp_path / ".skyn3t" / "visual-proof")


def test_repairs_dead_routes_then_reports(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text('@app.get("/")\ndef h(): ...\n')
    calls = {"n": 0}

    async def fake_check(base, routes, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return LivenessReport(results=[RouteResult("/", "GET", 500, False, "page")],
                                  total=1, ok=0, dead=1, dead_routes=["/"], health=0.0)
        return LivenessReport(results=[RouteResult("/", "GET", 200, True, "page")],
                              total=1, ok=1, dead=0, dead_routes=[], health=1.0)
    monkeypatch.setattr(lv, "check_liveness", fake_check)

    improved = {"n": 0}

    class _Improve:
        async def improve(self, project, goal, **k):
            improved["n"] += 1
            assert "/" in goal  # repair goal names the dead route
            return SimpleNamespace(status="completed")

    out = asyncio.run(liveness_self_improve(tmp_path, app_runner=_App("http://127.0.0.1:1"),
                                            improve_engine=_Improve(), max_rounds=2))
    assert improved["n"] == 1 and out.passed is True and out.report.health == 1.0


def test_unfixable_returns_not_passed(tmp_path, monkeypatch):
    async def fake_check(base, routes, **k):
        return LivenessReport(results=[RouteResult("/x", "GET", 500, False, "page")],
                              total=1, ok=0, dead=1, dead_routes=["/x"], health=0.0)
    monkeypatch.setattr(lv, "check_liveness", fake_check)

    class _Improve:
        async def improve(self, *a, **k):
            return SimpleNamespace(status="failed")

    out = asyncio.run(liveness_self_improve(tmp_path, app_runner=_App("http://127.0.0.1:1"),
                                            improve_engine=_Improve(), max_rounds=2))
    assert out.passed is False and out.skipped is False and out.report.dead == 1


def test_visual_repair_goal_includes_deterministic_browser_findings():
    report = LivenessReport(results=[RouteResult(
        "/pricing",
        "GET",
        200,
        True,
        "page",
        {
            "matches": False,
            "skipped": False,
            "issues": [
                "mobile: page is 84px wider than the viewport",
                "desktop: 1 visible image did not load",
            ],
        },
    )])

    goal = _repair_goal(report)

    assert "/pricing" in goal
    assert "84px wider" in goal
    assert "image did not load" in goal
