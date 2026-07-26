from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import runner as runner_mod
from skyn3t.studio.liveness import LivenessOutcome, LivenessReport, RouteResult
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


async def test_liveness_records_reusable_candidate_for_fully_passing_web_proof(
    tmp_path,
    monkeypatch,
) -> None:
    evidence = tmp_path / ".skyn3t" / "visual-proof"
    candidate = {
        "schema_version": 1,
        "source": "liveness",
        "runtime_input_fingerprint": {"sha256": "digest"},
    }
    runtime_fingerprint = {
        "algorithm": "preview-input-sha256-v1",
        "sha256": "digest",
        "file_count": 1,
        "byte_count": 5,
    }
    seen: dict[str, object] = {}

    async def fake_liveness(*_args, **_kwargs):
        return LivenessOutcome(
            passed=True,
            report=LivenessReport(
                results=[
                    RouteResult(
                        "/",
                        "GET",
                        200,
                        True,
                        "page",
                        {"matches": True, "issues": []},
                    )
                ],
                total=1,
                ok=1,
                dead=0,
                health=1.0,
                visual_total=1,
                visual_failed=0,
                visual_health=1.0,
                visual_artifact_dir=str(evidence),
                visual_report_path="visual-proof.json",
            ),
        )

    def fake_candidate_builder(
        project_dir,
        stack,
        *,
        artifact_dir,
        report_path,
        runtime_input_fingerprint,
    ):
        seen.update(
            {
                "project_dir": project_dir,
                "stack": stack,
                "artifact_dir": artifact_dir,
                "report_path": report_path,
                "runtime_input_fingerprint": runtime_input_fingerprint,
            }
        )
        return candidate

    monkeypatch.setattr(runner_mod, "liveness_self_improve", fake_liveness)
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.preview_input_fingerprint",
        lambda *_args, **_kwargs: runtime_fingerprint,
    )
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.build_reusable_web_proof",
        fake_candidate_builder,
    )
    runner = _runner(tmp_path)
    manifest = BuildManifest(slug="app", brief="brief", stack="react")

    await runner._run_liveness(
        manifest,
        str(tmp_path),
        SimpleNamespace(stack="react"),
        SimpleNamespace(passed=True),
        90.0,
        "go",
    )

    assert seen == {
        "project_dir": str(tmp_path),
        "stack": "react",
        "artifact_dir": ".skyn3t/visual-proof",
        "report_path": "visual-proof.json",
        "runtime_input_fingerprint": runtime_fingerprint,
    }
    assert manifest.extra["responsive_visual_proof"]["reusable_web_proof"] == candidate


async def test_required_web_proof_receives_liveness_reuse_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = {
        "schema_version": 1,
        "source": "liveness",
        "runtime_input_fingerprint": {"sha256": "digest"},
    }
    seen: dict[str, object] = {}

    class _Coordinator:
        async def run(
            self,
            project_dir,
            stack,
            routes=("/",),
            reusable_web_proof=None,
        ):
            seen["candidate"] = reusable_web_proof
            return SimpleNamespace(
                passed=True,
                status="passed",
                to_dict=lambda: {
                    "status": "passed",
                    "passed": True,
                    "cache_hit": True,
                    "reused_from": "liveness",
                    "steps": [],
                },
            )

    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _Coordinator,
    )
    runner = _runner(tmp_path)
    manifest = BuildManifest(slug="app", brief="brief", stack="react")
    manifest.extra["responsive_visual_proof"] = {
        "status": "passed",
        "reusable_web_proof": candidate,
    }

    score, verdict = await runner._run_required_proof_ladder(
        manifest,
        tmp_path,
        SimpleNamespace(stack="react"),
        91.0,
        "go",
    )

    assert (score, verdict) == (91.0, "go")
    assert seen["candidate"] == candidate
    assert manifest.extra["proof_ladder"]["cache_hit"] is True
