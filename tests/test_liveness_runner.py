# tests/test_liveness_runner.py
"""The runner's liveness hook: dampen the score by route health and (opt-in) gate
the verdict, without crashing the build."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import runner as runner_mod
from skyn3t.studio.liveness import LivenessOutcome, LivenessReport, RouteResult
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner


def _runner(tmp_path, **over):
    s = Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
                 logs_dir=tmp_path / "logs", **over)
    return StudioRunner(EventBus(), Orchestrator(EventBus()), settings=s, memory=None)


def _half_dead():
    return LivenessOutcome(passed=False, report=LivenessReport(
        results=[RouteResult("/", "GET", 200, True, "page"),
                 RouteResult("/x", "GET", 500, False, "page")],
        total=2, ok=1, dead=1, dead_routes=["/x"], health=0.5))


def test_liveness_dampens_score_by_health(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return _half_dead()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="fastapi")
    plan = SimpleNamespace(stack="fastapi")
    proof = SimpleNamespace(passed=True)

    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), plan, proof, 80.0, "go"))
    assert score == 60.0                       # 80 * (0.5 + 0.5*0.5) = 60
    assert verdict == "go"                      # default: dampen, don't gate
    assert man.extra["liveness_health"] == 0.5
    assert man.extra["liveness"]["dead"] == 1


def test_liveness_opt_in_gate_flips_to_no_go(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return _half_dead()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, liveness_gates_verdict=True)
    man = BuildManifest(slug="x", brief="b", stack="fastapi")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="fastapi"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert verdict == "no_go"
    assert "liveness_gate" in man.extra


def test_liveness_skipped_leaves_score_and_verdict(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return LivenessOutcome(skipped=True, reason="no live preview")
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="fastapi")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="fastapi"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert score == 80.0 and verdict == "go"
    assert man.extra["liveness"]["skipped"] is True


def test_liveness_never_crashes_the_build(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("liveness exploded")
    monkeypatch.setattr(runner_mod, "liveness_self_improve", boom)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="fastapi")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="fastapi"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert score == 80.0 and verdict == "go"  # degraded, build unaffected
