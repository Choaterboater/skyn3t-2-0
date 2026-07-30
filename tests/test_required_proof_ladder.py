from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner


def _runner(tmp_path) -> StudioRunner:
    bus = EventBus()
    return StudioRunner(
        bus,
        Orchestrator(bus),
        settings=Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            proof_ladder_required=True,
        ),
        memory=None,
    )


def _ladder_coordinator(status: str, step_status: str, reason: str):
    class _Coordinator:
        async def run(self, project_dir, stack, routes=("/",)):
            return SimpleNamespace(
                passed=False,
                status=status,
                to_dict=lambda: {
                    "status": status,
                    "passed": False,
                    "steps": [
                        {
                            "name": "docker",
                            "status": step_status,
                            "required": True,
                            "reason": reason,
                        }
                    ],
                },
            )

    return _Coordinator


async def test_unavailable_proof_tooling_caps_the_score_but_never_blocks(
    tmp_path, monkeypatch
):
    """A ladder that COULD NOT RUN is not evidence that the app is broken.

    ProofLadderResult.finalize() already reports status="skipped" when a required
    step had no tooling, but only ``result.passed`` was consulted — so every web
    build on a host without Docker/Playwright was no_go regardless of quality.
    Degraded evidence caps the score (that is what degraded_proof_score_cap is
    for); it must not flip the verdict, in either posture.
    """
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _ladder_coordinator("skipped", "skipped", "daemon unavailable"),
    )
    runner = _runner(tmp_path)
    manifest = BuildManifest(slug="app", brief="brief", stack="react")

    score, verdict = await runner._run_required_proof_ladder(
        manifest,
        tmp_path,
        SimpleNamespace(stack="react", slug="app"),
        91.0,
        "go",
    )

    assert verdict == "go"
    assert score == 74.0
    assert manifest.extra["proof_ladder"]["status"] == "skipped"
    assert "proof_ladder_gate" not in manifest.extra
    assert "tooling unavailable" in manifest.extra["proof_ladder_unavailable"]
    assert "docker" in manifest.extra["proof_ladder_unavailable"]


async def test_required_proof_ladder_blocks_a_real_failure(tmp_path, monkeypatch):
    """The other half of the contract: a step that RAN and FAILED still blocks."""
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _ladder_coordinator("failed", "failed", "page crashed"),
    )
    runner = _runner(tmp_path)
    manifest = BuildManifest(slug="app", brief="brief", stack="react")

    score, verdict = await runner._run_required_proof_ladder(
        manifest,
        tmp_path,
        SimpleNamespace(stack="react", slug="app"),
        91.0,
        "go",
    )

    assert verdict == "no_go"
    assert score == 74.0
    assert "required UI proof did not pass" in manifest.extra["proof_ladder_gate"]
    assert "proof_ladder_unavailable" not in manifest.extra


async def test_required_proof_ladder_keeps_passing_verdict(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    class _Coordinator:
        async def run(self, project_dir, stack, routes=("/",)):
            seen["routes"] = list(routes)
            return SimpleNamespace(
                passed=True,
                status="passed",
                to_dict=lambda: {
                    "status": "passed",
                    "passed": True,
                    "steps": [],
                },
            )

    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _Coordinator,
    )
    runner = _runner(tmp_path)
    manifest = BuildManifest(slug="app", brief="brief", stack="react")

    score, verdict = await runner._run_required_proof_ladder(
        manifest,
        tmp_path,
        SimpleNamespace(stack="react"),
        91.0,
        "go",
    )

    assert (score, verdict) == (91.0, "go")
    assert seen["routes"] == ["/"]
    assert "proof_ladder_gate" not in manifest.extra


async def test_required_proof_ladder_can_collect_without_preemptive_gate(
    tmp_path,
    monkeypatch,
):
    class _Coordinator:
        async def run(self, project_dir, stack, routes=("/",)):
            return SimpleNamespace(
                passed=False,
                status="failed",
                to_dict=lambda: {
                    "schema_version": 1,
                    "run_id": "trace-route-run",
                    "status": "failed",
                    "passed": False,
                    "persistence_error": "",
                    "steps": [
                        {
                            "name": "playwright",
                            "status": "failed",
                            "required": True,
                            "reason": "route failed",
                            "detail": {"routes": list(routes), "proofs": []},
                        }
                    ],
                },
            )

    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _Coordinator,
    )
    runner = _runner(tmp_path)
    manifest = BuildManifest(slug="app", brief="brief", stack="react")

    score, verdict = await runner._run_required_proof_ladder(
        manifest,
        tmp_path,
        SimpleNamespace(stack="react"),
        91.0,
        "go",
        blocking=False,
        routes=("/settings",),
        include_discovered_routes=False,
    )

    assert (score, verdict) == (91.0, "go")
    assert manifest.extra["proof_ladder"]["run_id"] == "trace-route-run"
    assert "proof_ladder_gate" not in manifest.extra
