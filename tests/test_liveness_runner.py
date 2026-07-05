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


def _visually_broken():
    return LivenessOutcome(passed=False, report=LivenessReport(
        results=[RouteResult("/", "GET", 200, True, "page",
                             {"matches": False, "issues": ["loading"]})],
        total=1, ok=1, dead=0, dead_routes=[], health=1.0,
        visual_total=1, visual_failed=1, visual_failed_routes=["/"],
        visual_health=0.0))


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


def test_liveness_visual_failure_gates_ui_stack(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return _visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert score == 40.0
    assert verdict == "no_go"
    assert man.extra["liveness_visual_health"] == 0.0
    assert "visual liveness" in man.extra["liveness_gate"]


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


def test_liveness_repair_does_not_record_improve_history(tmp_path, monkeypatch):
    captured = []

    async def fake(*a, **k):
        engine = k["improve_engine"]
        captured.append(getattr(engine, "record_history", True))
        return LivenessOutcome(skipped=True, reason="no live preview")

    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="nextjs")

    asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert captured == [False]


def test_visual_self_heal_records_skip_for_non_ui_stack(tmp_path):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="python_cli")
    changed = asyncio.run(
        r._run_visual_self_heal(man, str(tmp_path), SimpleNamespace(stack="python_cli")))
    assert changed is False
    assert man.extra["visual_self_heal"]["skipped"] is True
    assert "rendered UI" in man.extra["visual_self_heal"]["reason"]


def test_visual_self_heal_repair_does_not_record_improve_history(tmp_path, monkeypatch):
    captured = []

    async def fake_loop(project_dir, goal, **kw):
        engine = kw["improve_engine"]
        captured.append(getattr(engine, "record_history", True))

        class _Outcome:
            def to_dict(self):
                return {
                    "passed": True,
                    "skipped": False,
                    "reason": "",
                    "rounds": [{"index": 0, "improved": False}],
                }

        return _Outcome()

    monkeypatch.setattr(runner_mod, "visual_self_improve", fake_loop)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="a polished dashboard", stack="nextjs")

    asyncio.run(r._run_visual_self_heal(man, str(tmp_path), SimpleNamespace(stack="nextjs")))
    assert captured == [False]


def test_visual_self_heal_requested_honors_per_build_override(tmp_path):
    r = _runner(tmp_path, visual_self_heal=False)
    assert r._visual_self_heal_requested(r.settings, {}) is False
    assert r._visual_self_heal_requested(r.settings, {"visual_self_heal": True}) is True

    globally_enabled = _runner(tmp_path, visual_self_heal=True)
    assert globally_enabled._visual_self_heal_requested(globally_enabled.settings, {}) is True
    assert globally_enabled._visual_self_heal_requested(
        globally_enabled.settings, {"visual_self_heal": False}
    ) is False


def test_visual_self_heal_records_result_and_refreshes_files(tmp_path, monkeypatch):
    async def fake_loop(project_dir, goal, **kw):
        assert goal == "a polished landing page"
        assert kw["stack"] == "react"
        (tmp_path / "visual-fix.txt").write_text("fixed")

        class _Outcome:
            def to_dict(self):
                return {
                    "passed": True,
                    "skipped": False,
                    "reason": "",
                    "rounds": [{"index": 0, "improved": True}],
                }

        return _Outcome()

    monkeypatch.setattr(runner_mod, "visual_self_improve", fake_loop)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="a polished landing page", stack="react")
    changed = asyncio.run(
        r._run_visual_self_heal(man, str(tmp_path), SimpleNamespace(stack="react")))
    assert changed is True
    assert man.extra["visual_self_heal"]["passed"] is True
    assert "visual-fix.txt" in man.files


def test_product_quality_gates_flip_finance_build_to_no_go(tmp_path):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="AI paper trading dashboard", stack="nextjs")
    (tmp_path / "app" / "api" / "portfolio").mkdir(parents=True)
    (tmp_path / "app" / "api" / "portfolio" / "route.js").write_text("export const GET = () => {}", encoding="utf-8")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "store.js").write_text(
        "for (let d=0; d<30; d++) { Math.random(); createTrade({status: 'filled'}); }",
        encoding="utf-8",
    )

    score, verdict = r._run_product_quality_gates(
        man,
        str(tmp_path),
        SimpleNamespace(stack="nextjs", brief=man.brief),
        91.0,
        "go",
    )

    assert verdict == "no_go"
    assert score == 49.0
    assert man.extra["finance_sanity"]["ok"] is False
    assert man.extra["workflow_depth"]["ok"] is False
    assert "product_quality_gate" in man.extra


def test_product_quality_gates_skip_non_finance_build(tmp_path):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="wedding photography website", stack="nextjs")

    score, verdict = r._run_product_quality_gates(
        man,
        str(tmp_path),
        SimpleNamespace(stack="nextjs", brief=man.brief),
        88.0,
        "go",
    )

    assert verdict == "go"
    assert score == 88.0
    assert man.extra["finance_sanity"]["skipped"] is True
    assert man.extra["workflow_depth"]["skipped"] is True
