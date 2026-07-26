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
    r = _runner(tmp_path)
    man = BuildManifest(slug="x", brief="b", stack="nextjs")
    score, verdict = asyncio.run(
        r._run_liveness(man, str(tmp_path), SimpleNamespace(stack="nextjs"),
                        SimpleNamespace(passed=True), 80.0, "go"))
    assert score == 40.0
    assert verdict == "no_go"
    assert man.extra["liveness_visual_health"] == 0.0
    assert man.extra["responsive_visual_proof"]["status"] == "failed"
    assert "visual liveness" in man.extra["liveness_gate"]


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
    r = _runner(tmp_path)
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
