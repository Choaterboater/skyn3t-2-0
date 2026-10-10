# tests/test_liveness_runner.py
"""The runner's liveness hook: dampen the score by route health and (opt-in) gate
the verdict, without crashing the build."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import runner as runner_mod
from skyn3t.studio.liveness import LivenessOutcome, LivenessReport, RouteResult, _extract_links
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


def test_liveness_crawler_ignores_remix_dev_module_urls():
    html = """
    <script type="module" src="/@id/__x00__virtual:remix/browser-manifest"></script>
    <script type="module" src="/app/root.tsx"></script>
    <script type="module" src="/app/routes/_index.tsx"></script>
    <a href="/cart">Cart</a>
    """

    assert _extract_links(html) == {"/cart"}


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
    r = _runner(tmp_path, build_posture="release")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert score == 40.0
    assert verdict == "no_go"
    assert man.extra["liveness_visual_health"] == 0.0
    assert man.extra["responsive_visual_proof"]["status"] == "failed"
    assert "visual liveness" in man.extra["liveness_gate"]
    finding = man.extra["gate_findings"][-1]
    assert finding["gate"] == "liveness"
    assert finding["status"] == "failed"
    assert finding["blocked"] is True


def test_liveness_visual_failure_records_without_blocking_in_lab(tmp_path, monkeypatch):
    """Visual liveness is a viewport heuristic / vision judgement, not proof the
    delivery is broken: under lab posture it lands in the gate_findings ledger
    (with the score capped like any other non-blocking finding) but must not
    flip a delivered, served app to no_go."""
    async def fake(*a, **k):
        return _visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert score == r.settings.degraded_proof_score_cap  # capped, not halved to 40
    assert verdict == "go"
    finding = man.extra["gate_findings"][-1]
    assert finding["gate"] == "liveness"
    assert finding["status"] == "failed"
    assert finding["blocked"] is False
    assert finding["posture"] == "lab"
    # The legacy key survives in both postures — it feeds the fix loop/reports.
    assert "visual liveness" in man.extra["liveness_gate"]


def test_liveness_visual_failure_blocking_gates_escape_hatch(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return _visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab", blocking_gates="liveness")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert verdict == "no_go"
    # Liveness blocks here, so the visual multiplier applies exactly as in release.
    assert score == 40.0
    assert man.extra["liveness_visual_score_policy"] == "health_multiplier"


def _half_dead_and_visually_broken():
    return LivenessOutcome(passed=False, report=LivenessReport(
        results=[RouteResult("/", "GET", 200, True, "page",
                             {"matches": False, "issues": ["overflow"]}),
                 RouteResult("/x", "GET", 500, False, "page")],
        total=2, ok=1, dead=1, dead_routes=["/x"], health=0.5,
        visual_total=1, visual_failed=1, visual_failed_routes=["/"],
        visual_health=0.0))


def test_liveness_lab_visual_failure_caps_score_instead_of_halving(tmp_path, monkeypatch):
    """A single viewport heuristic (one console.error, a >2px overflow at 390px)
    zeroes visual_health on a one-route SPA. Under lab that must not halve a
    decent build (91 -> 45.5 while still 'go'): it caps at
    degraded_proof_score_cap like every other non-blocking finding, and the
    verdict is untouched. _shape_final_score still caps the final 'go' at
    70 + 25*visual_health afterwards; only the halving is gone."""
    async def fake(*a, **k):
        return _visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 91.0, "go"))
    assert score == 74.0                       # not 91 * 0.5 = 45.5
    assert man.score == 74.0
    # End-to-end the shaping pass lands it at 70 (visual_health 0.0), not 74.
    assert StudioRunner._shape_final_score(
        man, SimpleNamespace(passed=True), score, verdict) == 70.0
    assert verdict == "go"
    assert man.extra["liveness_visual_score_policy"] == "finding_cap"
    # Consumers keep reading the same health keys in every posture.
    assert man.extra["liveness_health"] == 1.0
    assert man.extra["liveness_visual_health"] == 0.0
    assert man.extra["liveness_effective_health"] == 0.0
    assert man.extra["responsive_visual_proof"]["status"] == "failed"
    assert "visual liveness" in man.extra["liveness_gate"]


def test_liveness_lab_visual_failure_below_cap_is_not_dampened(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return _visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 60.0, "go"))
    assert score == 60.0
    assert verdict == "go"


def test_liveness_release_visual_failure_keeps_health_multiplier(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return _visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="release")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 91.0, "go"))
    assert score == 45.5                       # 91 * (0.5 + 0.5*min(1.0, 0.0))
    assert man.score == 45.5
    assert verdict == "no_go"
    assert man.extra["liveness_visual_score_policy"] == "health_multiplier"


def test_liveness_lab_dead_route_still_dampens(tmp_path, monkeypatch):
    """Dead HTTP routes are a real failure: they dampen the score in lab too."""
    async def fake(*a, **k):
        return _half_dead()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="b", stack="fastapi")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="fastapi"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert score == 60.0                       # 80 * (0.5 + 0.5*0.5)
    assert verdict == "go"
    assert "liveness_visual_score_policy" not in man.extra


def test_liveness_lab_dead_route_and_visual_failure_dampens_then_caps(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return _half_dead_and_visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 100.0, "go"))
    # Route health dampens (100 * 0.75 = 75), then the lab finding cap applies.
    assert score == 74.0
    assert verdict == "go"
    assert man.extra["liveness_effective_health"] == 0.0


def _ten_pages_one_visually_broken():
    pages = [RouteResult(f"/p{i}", "GET", 200, True, "page") for i in range(10)]
    pages[3].visual = {"matches": False, "issues": ["overflow"]}
    return LivenessOutcome(passed=False, report=LivenessReport(
        results=pages, total=10, ok=10, dead=0, dead_routes=[], health=1.0,
        visual_total=10, visual_failed=1, visual_failed_routes=["/p3"],
        visual_health=0.9))


_NON_UI_WEB_STACKS = ["fastapi", "express", "node_express", "rag", "workflow"]


@pytest.mark.parametrize("stack", _NON_UI_WEB_STACKS)
@pytest.mark.parametrize("outcome, lab, release, lab_policy", [
    # 92 * (0.5 + 0.5*min(1.0, 0.9)) = 87.4 beats the 74 cap in both postures.
    (_ten_pages_one_visually_broken, 87.4, 87.4, "health_multiplier"),
    # One console.error on a single-page app: lab caps at 74, release halves.
    (_visually_broken, 74.0, 46.0, "finding_cap"),
])
def test_liveness_non_ui_stack_visual_failure_follows_posture_without_finding(
        tmp_path, monkeypatch, stack, outcome, lab, release, lab_policy):
    """Visual liveness runs on 'page' routes for every web stack, so the same
    non-blocking scoring rule applies everywhere: under lab a single
    console.error on a one-page fastapi app must not halve the score (92 -> 46)
    while the verdict stays go. Release (and lab with blocking_gates=liveness)
    keeps the old multiplier exactly. The visual gate finding stays UI-only."""
    async def fake(*a, **k):
        return outcome()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    cases = [
        ({"build_posture": "lab"}, lab, lab_policy),
        ({"build_posture": "release"}, release, "health_multiplier"),
        ({"build_posture": "lab", "blocking_gates": "liveness"},
         release, "health_multiplier"),
    ]
    for over, score_expected, policy in cases:
        r = _runner(tmp_path, **over)
        man = BuildManifest(slug="x", brief="b", stack=stack)
        result = asyncio.run(
            r._run_liveness(man, str(tmp_path), SimpleNamespace(stack=stack),
                            SimpleNamespace(passed=True), 92.0, "go"))
        assert result == (score_expected, "go")
        assert man.score == score_expected
        assert man.extra["liveness_visual_score_policy"] == policy
        assert not [f for f in man.extra.get("gate_findings", [])
                    if f.get("gate") == "liveness"]
        assert "liveness_gate" not in man.extra


def _visually_healthy():
    return LivenessOutcome(passed=True, report=LivenessReport(
        results=[RouteResult("/", "GET", 200, True, "page",
                             {"matches": True, "issues": []})],
        total=1, ok=1, dead=0, dead_routes=[], health=1.0,
        visual_total=1, visual_failed=0, visual_failed_routes=[],
        visual_health=1.0))


@pytest.mark.parametrize("stack", ["nextjs", *_NON_UI_WEB_STACKS])
def test_liveness_lab_policy_reads_multiplier_when_visual_passes(tmp_path, monkeypatch, stack):
    """The policy key names what decided the score: with no visual failure the
    lab cap never ran, so it must not read 'finding_cap'."""
    async def fake(*a, **k):
        return _visually_healthy()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="b", stack=stack)
    result = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack=stack),
                        SimpleNamespace(passed=True), 92.0, "go"))
    assert result == (92.0, "go")
    assert man.extra["liveness_visual_score_policy"] == "health_multiplier"


@pytest.mark.parametrize("stack", ["nextjs", "react", "static", *_NON_UI_WEB_STACKS])
@pytest.mark.parametrize("outcome", [
    _visually_broken, _ten_pages_one_visually_broken, _half_dead_and_visually_broken,
])
@pytest.mark.parametrize("start", [100.0, 92.0, 74.0, 60.0])
def test_liveness_lab_score_never_below_release(
        tmp_path, monkeypatch, stack, outcome, start):
    """Lab is the more lenient posture: whatever the stack, a visual failure
    must never leave a lab build scored below the same build under release."""
    async def fake(*a, **k):
        return outcome()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    scores = {}
    for posture in ("lab", "release"):
        r = _runner(tmp_path, build_posture=posture)
        man = BuildManifest(slug="x", brief="b", stack=stack)
        scores[posture], _verdict = asyncio.run(
            r._run_liveness(man, str(tmp_path), SimpleNamespace(stack=stack),
                            SimpleNamespace(passed=True), start, "go"))
    assert scores["lab"] >= scores["release"]


def test_liveness_lab_ui_partial_visual_failure_never_below_old_multiplier(
        tmp_path, monkeypatch):
    """One failing page out of ten (visual_health 0.9) used to cost 5%; the lab
    finding cap must not turn that into a flat 74. Lab is never harsher than the
    old multiplier: max(finding cap, multiplier) — and the finding is still
    recorded without blocking."""
    async def fake(*a, **k):
        return _ten_pages_one_visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 92.0, "go"))
    assert score == 87.4                       # 92 * (0.5 + 0.5*0.9), not 74
    assert man.score == 87.4
    assert verdict == "go"
    assert man.extra["liveness_visual_score_policy"] == "health_multiplier"
    finding = man.extra["gate_findings"][-1]
    assert finding["gate"] == "liveness"
    assert finding["blocked"] is False


def test_liveness_lab_visual_failure_with_failed_proof_leaves_score(tmp_path, monkeypatch):
    """A proof-failed build is already halved by _honest_score; liveness must
    not double-count it with either the multiplier or the finding cap."""
    async def fake(*a, **k):
        return _visually_broken()
    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, _verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=False), 80.0, "go"))
    assert score == 80.0


def test_liveness_visual_success_clears_stale_visual_self_heal_gate(tmp_path, monkeypatch):
    async def fake(*a, **k):
        return LivenessOutcome(passed=True, report=LivenessReport(
            results=[RouteResult("/", "GET", 200, True, "page",
                                 {"matches": True, "issues": []})],
            total=1, ok=1, dead=0, dead_routes=[], health=1.0,
            visual_total=1, visual_failed=0, visual_failed_routes=[],
            visual_health=1.0))

    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    man.extra["visual_self_heal_gate"] = "rendered UI still failed visual check after self-heal"
    # The gate itself flipped the verdict go -> no_go; reconciliation may undo it.
    man.extra["visual_self_heal_gate_flipped_from"] = "go"
    man.extra["visual_self_heal"] = {
        "passed": False,
        "skipped": False,
        "reason": "visual issues remain after max rounds",
    }

    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 80.0, "no_go"))

    assert score == 80.0
    assert verdict == "go"
    assert man.extra["liveness_visual_health"] == 1.0
    assert man.extra["responsive_visual_proof"]["status"] == "passed"
    assert "visual_self_heal_gate" not in man.extra
    assert "visual_self_heal_gate_flipped_from" not in man.extra


def test_liveness_reconciliation_keeps_no_go_from_other_causes(tmp_path, monkeypatch):
    """A stale visual_self_heal marker is cleared when the rendered UI proves
    healthy, but a no_go the gate did NOT cause (lab-posture reviewer/rescore,
    delivery signals, forced_blocking gates) must survive: perfect liveness says
    nothing about those findings. Only the flip recorded via
    visual_self_heal_gate_flipped_from is restored."""
    async def fake(*a, **k):
        return LivenessOutcome(passed=True, report=LivenessReport(
            results=[RouteResult("/", "GET", 200, True, "page",
                                 {"matches": True, "issues": []})],
            total=1, ok=1, dead=0, dead_routes=[], health=1.0,
            visual_total=1, visual_failed=0, visual_failed_routes=[],
            visual_health=1.0))

    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    man.extra["visual_self_heal_gate"] = "rendered UI still failed visual check after self-heal"
    # No visual_self_heal_gate_flipped_from marker: the gate never flipped the
    # verdict (lab posture records it non-blocking), so this no_go came from
    # somewhere else and must be preserved.
    man.extra["visual_self_heal"] = {
        "passed": False,
        "skipped": False,
        "reason": "visual issues remain after max rounds",
    }

    _score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 80.0, "no_go"))

    assert verdict == "no_go"
    assert "visual_self_heal_gate" not in man.extra
    assert man.extra["visual_self_heal_reconciled_by_liveness"] is True


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


def test_liveness_records_honest_responsive_proof_skip(tmp_path, monkeypatch):
    evidence = tmp_path / ".skyn3t" / "visual-proof"

    async def fake(*a, **k):
        return LivenessOutcome(passed=True, report=LivenessReport(
            results=[RouteResult("/", "GET", 200, True, "page", {
                "matches": None,
                "skipped": True,
                "reason": "playwright chromium unavailable",
            })],
            total=1,
            ok=1,
            dead=0,
            health=1.0,
            visual_total=0,
            visual_failed=0,
            visual_skipped=1,
            visual_skipped_routes=["/"],
            visual_health=None,
            visual_artifact_dir=str(evidence),
            visual_report_path="visual-proof.json",
        ))

    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake)
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="react")

    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="react"),
                        SimpleNamespace(passed=True), 80.0, "go"))

    assert score == 80.0 and verdict == "go"
    assert "liveness_visual_health" not in man.extra
    assert man.extra["liveness"]["visual_artifact_dir"] == ".skyn3t/visual-proof"
    assert man.extra["responsive_visual_proof"] == {
        "schema_version": 1,
        "status": "skipped",
        "routes_checked": 0,
        "routes_failed": 0,
        "routes_skipped": 1,
        "failed_routes": [],
        "skipped_routes": ["/"],
        "viewports": [
            {"name": "desktop", "width": 1440, "height": 900},
            {"name": "mobile", "width": 390, "height": 844},
        ],
        "artifact_dir": ".skyn3t/visual-proof",
        "report_path": "visual-proof.json",
    }


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
        from skyn3t.studio.preview_supervisor import PreviewSupervisor

        assert isinstance(kw["app_runner"], PreviewSupervisor)
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


def test_visual_self_heal_forwards_frozen_manifest_layout_profile(tmp_path, monkeypatch):
    from skyn3t.studio.layout_profiles import resolve_layout_profile

    profile = resolve_layout_profile(
        "dashboard", stack="react", engine="dom",
    ).to_dict()
    captured = []

    async def fake_loop(project_dir, goal, **kw):
        captured.append(kw.get("layout_profile"))

        class _Outcome:
            def to_dict(self):
                return {
                    "passed": False,
                    "skipped": True,
                    "reason": "no vision provider wired",
                    "rounds": [],
                }

        return _Outcome()

    monkeypatch.setattr(runner_mod, "visual_self_improve", fake_loop)
    runner = _runner(tmp_path)
    manifest = BuildManifest(slug="x", brief="dashboard", stack="nextjs")
    manifest.extra["layout_profile"] = profile

    asyncio.run(
        runner._run_visual_self_heal(
            manifest,
            str(tmp_path),
            SimpleNamespace(stack="nextjs"),
        )
    )

    assert captured == [profile]
    assert captured[0] is profile


def test_visual_self_heal_requested_honors_per_build_override(tmp_path):
    r = _runner(tmp_path, visual_self_heal=False)
    assert r._visual_self_heal_requested(r.settings, {}) is False
    assert r._visual_self_heal_requested(r.settings, {"visual_self_heal": True}) is True

    globally_enabled = _runner(tmp_path, visual_self_heal=True)
    assert globally_enabled._visual_self_heal_requested(globally_enabled.settings, {}) is True
    assert globally_enabled._visual_self_heal_requested(
        globally_enabled.settings, {"visual_self_heal": False}
    ) is False


def test_audit_only_visual_failure_remains_advisory():
    audit_only = {
        "passed": False,
        "skipped": False,
        "rounds": [
            {
                "matches": False,
                "skipped": False,
                "advisory_only": True,
                "layout_audit": {
                    "profile": "workspace",
                    "issues": ["Desktop workspace is under-filled."],
                },
            }
        ],
    }
    vision_failure = {
        **audit_only,
        "rounds": [{**audit_only["rounds"][0], "advisory_only": False}],
    }

    assert StudioRunner._visual_failure_is_advisory(audit_only) is True
    assert StudioRunner._visual_failure_is_advisory(vision_failure) is False
    assert StudioRunner._visual_failure_is_advisory({"rounds": []}) is False
    assert StudioRunner._visual_failure_should_gate(audit_only, "nextjs") is False
    assert StudioRunner._visual_failure_should_gate(vision_failure, "nextjs") is True
    assert StudioRunner._visual_failure_should_gate(
        {"passed": False, "skipped": True, "rounds": []},
        "nextjs",
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


def test_ai_native_gate_blocks_real_contract_findings(tmp_path):
    r = _runner(tmp_path, build_posture="release")
    man = BuildManifest(slug="x", brief="chat with docs", stack="rag")
    man.extra["rag_check"] = {
        "ok": False,
        "skipped": False,
        "issues": ["query did not retrieve the planted marker"],
    }

    verdict = r._apply_ai_native_gates(man, "go")

    assert verdict == "no_go"
    assert "ai_native_gate" in man.extra
    assert "rag_check" in man.extra["ai_native_gate"]
    finding = man.extra["gate_findings"][-1]
    assert finding["gate"] == "ai_native"
    assert finding["blocked"] is True


def test_ai_native_gate_records_without_blocking_in_lab(tmp_path):
    """The MCP/RAG/workflow probes are environment-dependent contract checks:
    under lab posture a real finding is recorded (and the runner caps the score
    via _score_after_finding at the call site) but must not flip a delivered,
    working app to no_go."""
    r = _runner(tmp_path, build_posture="lab")
    man = BuildManifest(slug="x", brief="chat with docs", stack="rag")
    man.extra["rag_check"] = {
        "ok": False,
        "skipped": False,
        "issues": ["query did not retrieve the planted marker"],
    }

    verdict = r._apply_ai_native_gates(man, "go")

    assert verdict == "go"
    assert "rag_check" in man.extra["ai_native_gate"]
    finding = man.extra["gate_findings"][-1]
    assert finding["gate"] == "ai_native"
    assert finding["status"] == "failed"
    assert finding["blocked"] is False
    assert finding["posture"] == "lab"
    # The call-site score honesty rule (mirrors product_quality): even a
    # non-blocking finding must not ship a success-looking score.
    assert r._score_after_finding(95.0, verdict) == r.settings.degraded_proof_score_cap


def test_ai_native_gate_blocking_gates_escape_hatch_restores_block(tmp_path):
    r = _runner(tmp_path, build_posture="lab", blocking_gates="ai_native")
    man = BuildManifest(slug="x", brief="chat with docs", stack="rag")
    man.extra["rag_check"] = {
        "ok": False,
        "skipped": False,
        "issues": ["query did not retrieve the planted marker"],
    }

    assert r._apply_ai_native_gates(man, "go") == "no_go"


def test_ai_native_gate_never_blocks_soft_skips(tmp_path):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="chat with docs", stack="rag")
    man.extra["rag_check"] = {
        "ok": False,
        "skipped": True,
        "issues": ["ignored because skipped"],
    }

    assert r._apply_ai_native_gates(man, "go") == "go"
    assert "ai_native_gate" not in man.extra


def test_game_quality_master_switch_blocks_even_in_lab(tmp_path):
    """Mirrors the runner's _signals fold: ("game_quality", ok=False) routed
    through _gate_outcome with a lab posture must still block while the
    documented master switch (game_quality_gates_verdict, default ON) is on —
    the switch outranks the posture default via forced_blocking."""
    from skyn3t.studio.gate_posture import GatePosture

    r = _runner(tmp_path, build_posture="lab", game_quality_gates_verdict=True)
    man = BuildManifest(slug="x", brief="a game", stack="phaser")
    posture = GatePosture.from_settings(r.settings)

    verdict = r._gate_outcome(man, "game_quality", False, "go",
                              "game visual/playtest gate failed", posture=posture)

    assert verdict == "no_go"
    assert r._clamp_score_to_verdict(88.0, verdict) <= 49.0
    finding = man.extra["gate_findings"][-1]
    assert finding["gate"] == "game_quality"
    assert finding["blocked"] is True

    advisory = _runner(tmp_path, build_posture="lab", game_quality_gates_verdict=False)
    lab = GatePosture.from_settings(advisory.settings)
    assert advisory._gate_outcome(
        man, "game_quality", False, "go", "still failing", posture=lab
    ) == "go"


def test_degraded_proof_caps_success_score(tmp_path):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="app", stack="fastapi")
    proof = SimpleNamespace(detail={
        "proof_environment": {
            "degraded": True,
            "degraded_reasons": ["build skipped"],
        }
    })

    score = r._apply_degraded_proof_score(man, proof, 96.0, "go")

    assert score == 74.0
    assert man.extra["proof_environment_gate"]["degraded"] is True


def test_scaffold_stub_proof_blocks_success_verdict(tmp_path):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="chat with docs", stack="rag")
    proof = SimpleNamespace(detail={
        "scaffold_stub": (
            "delivered tree is essentially the pristine rag scaffold "
            "(mean 8-word-shingle overlap 1.00 across 3 source file(s), "
            "no app code added) - the build shipped the stub instead of "
            "implementing the brief"
        )
    })

    score, verdict = r._apply_scaffold_stub_proof_gate(man, proof, 86.0, "go")

    assert verdict == "no_go"
    assert score == 49.0
    assert man.extra["scaffold_stub_gate"]["triggered"] is True
    assert "pristine rag scaffold" in man.extra["scaffold_stub_gate"]["reason"]


def test_post_proof_repair_triggers_full_reproof(tmp_path, monkeypatch):
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="game", stack="phaser")
    man.extra["post_proof_repair_changed"] = True
    plan = SimpleNamespace(stack="phaser", checklist=["src/sim.js"])
    calls = []

    class _Proof:
        def __init__(self, passed):
            self.passed = passed
            self.detail = {"build": "failed" if not passed else "passed"}

        def to_dict(self):
            return {"passed": self.passed, "detail": dict(self.detail)}

    def fake_proof(project_dir, **kwargs):
        calls.append(kwargs)
        return _Proof(False)

    monkeypatch.setattr(runner_mod, "proof_run", fake_proof)

    proof = asyncio.run(
        r._reproof_after_post_proof_repairs(man, plan, str(tmp_path), _Proof(True))
    )

    assert proof.passed is False
    assert calls and calls[0]["run_build"] is True
    assert man.extra["proof"]["passed"] is False
    assert man.extra["proof_after_post_proof_repair"]["passed"] is False


def test_non_game_fix_loop_records_runtime_self_heal(tmp_path, monkeypatch):
    r = _runner(tmp_path, max_fix_attempts=1)
    man = BuildManifest(slug="x", brief="dashboard", stack="fastapi")
    plan = SimpleNamespace(
        stack="fastapi",
        checklist=["main.py"],
        to_dict=lambda: {"stack": "fastapi", "checklist": ["main.py"]},
    )

    class _Proof:
        def __init__(self, passed):
            self.passed = passed
            self.missing = []
            self.detail = {"runtime": "failed" if not passed else "passed"}

        def error_gaps(self):
            return ["server raised RuntimeError on boot"]

        def to_dict(self):
            return {"passed": self.passed, "detail": dict(self.detail)}

    def fake_proof(project_dir, **kwargs):
        return _Proof(True)

    monkeypatch.setattr(runner_mod, "proof_run", fake_proof)

    proof = asyncio.run(
        r._fix_loop(man, plan, str(tmp_path), _Proof(False), "corr", {})
    )

    assert proof.passed is True
    assert man.extra["runtime_self_heal"]["stack"] == "fastapi"
    assert man.extra["runtime_self_heal"]["attempts"] == 1
    assert man.extra["runtime_self_heal"]["passed"] is True
    assert man.extra["runtime_self_heal"]["gated"] is True
