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


async def test_required_proof_ladder_blocks_missing_external_proof(tmp_path, monkeypatch):
    class _Coordinator:
        async def run(self, project_dir, stack, routes=("/",)):
            return SimpleNamespace(
                passed=False,
                status="skipped",
                to_dict=lambda: {
                    "status": "skipped",
                    "passed": False,
                    "steps": [
                        {
                            "name": "docker",
                            "status": "skipped",
                            "required": True,
                            "reason": "daemon unavailable",
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
    )

    assert verdict == "no_go"
    assert score == 74.0
    assert manifest.extra["proof_ladder"]["status"] == "skipped"
    assert "required UI proof did not pass" in manifest.extra["proof_ladder_gate"]


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
