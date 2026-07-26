from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.preview_supervisor import ProofLadderResult, ProofStep
from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord
from skyn3t.studio.proof_run import ProofResult
from skyn3t.studio.runner import StudioRunner
from skyn3t.worktree import source_tree_snapshot


def _runner(tmp_path: Path, **overrides) -> StudioRunner:
    bus = EventBus()
    values = {
        "_env_file": None,
        "projects_dir": tmp_path / "Projects",
        "data_dir": tmp_path / "data",
        "logs_dir": tmp_path / "logs",
        "execution_backend": "docker",
        "proof_ladder_required": False,
        **overrides,
    }
    return StudioRunner(
        bus,
        Orchestrator(bus),
        settings=Settings(**values),
        memory=None,
    )


def _manifest(stack: str = "python") -> BuildManifest:
    return BuildManifest(
        build_id="build-requirement-trace",
        slug="trace-app",
        brief="Build a verified app",
        stack=stack,
    )


def _plan(stack: str = "python"):
    return SimpleNamespace(stack=stack, checklist=[])


def _spec(
    requirements: list[RequirementRecord],
    *,
    registry: int | None = 1,
) -> ProductSpecV1:
    return ProductSpecV1(
        project_id="trace-app",
        goal="Build a verified app",
        acceptance_registry_version=registry,
        requirements=requirements,
    )


def _proof(*, backend: str = "docker", passed: bool = True) -> ProofResult:
    return ProofResult(
        passed=passed,
        mode="sandbox" if backend == "docker" else "local",
        files_total=1,
        files_substantive=1,
        score=96.0 if passed else 40.0,
        detail={
            "build": "passed" if passed else "failed",
            "tests": "passed" if passed else "failed",
            "node_tests": "passed" if passed else "failed",
            "swift_tests": "passed" if passed else "failed",
            "ruff": "passed" if passed else "failed",
            "stack_check": "pass" if passed else "fail",
            "entrypoints": ["app.py"],
            "proof_environment": {
                "command_backend": backend,
                "execution_backend": backend,
            },
        },
    )


def _write_source(project: Path, *, web: bool = False) -> None:
    project.mkdir(parents=True)
    if web:
        (project / "index.html").write_text("<main>verified</main>")
    else:
        (project / "app.py").write_text("print('verified')\n")


def _passing_ladder(project: Path, stack: str, routes: list[str]) -> ProofLadderResult:
    result = ProofLadderResult(
        project_dir=str(project),
        stack=stack,
        artifact_dir=str(project / ".skyn3t" / "proof-ladder"),
        run_id="final-route-evidence",
    )
    result.steps.append(
        ProofStep(
            "playwright",
            "passed",
            True,
            detail={
                "routes": routes,
                "proofs": [
                    {
                        "schema_version": 1,
                        "route": route,
                        "passed": True,
                        "skipped": False,
                        "reason": "",
                        "report_path": (
                            f"playwright/{route.strip('/') or 'index'}.json"
                        ),
                        "viewports": [
                            {
                                "name": name,
                                "width": width,
                                "height": height,
                                "passed": True,
                                "skipped": False,
                                "reason": "",
                                "screenshot": (
                                    "playwright/"
                                    f"{route.strip('/') or 'index'}/{name}.png"
                                ),
                                "metrics": {},
                                "issues": [],
                                "console_errors": [],
                                "page_errors": [],
                            }
                            for name, width, height in (
                                ("desktop", 1440, 900),
                                ("mobile", 390, 844),
                            )
                        ],
                    }
                    for route in routes
                ],
            },
        )
    )
    result.finalize()
    result.report_path = str(
        project / ".skyn3t" / "proof-ladder" / "proof-ladder.json"
    )
    return result


def _passing_maestro_ladder(project: Path) -> ProofLadderResult:
    result = ProofLadderResult(
        project_dir=str(project),
        stack="react_native",
        artifact_dir=str(project / ".skyn3t" / "proof-ladder"),
        run_id="global-maestro-evidence",
    )
    result.steps.append(
        ProofStep(
            "maestro",
            "passed",
            True,
            detail={
                "flows": ["smoke.yaml"],
                "executions": [{"flow": "smoke.yaml", "passed": True}],
            },
        )
    )
    result.finalize()
    result.report_path = str(project / ".skyn3t/proof-ladder/proof-ladder.json")
    return result


async def test_legacy_and_partial_contracts_add_zero_verifier_calls(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project)
    runner = _runner(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.proof_run",
        lambda *args, **kwargs: calls.append("proof"),
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.stabilize_node_dependencies",
        lambda *args, **kwargs: calls.append("stabilize"),
    )

    legacy = _spec(
        [RequirementRecord("Build", acceptance_ids=["proof:build"])],
        registry=None,
    )
    partial = _spec(
        [
            RequirementRecord("Build", acceptance_ids=["proof:build"]),
            RequirementRecord("Document", acceptance_ids=[]),
        ]
    )
    for product, expected_mode in (
        (legacy, "legacy_advisory"),
        (partial, "partial"),
    ):
        manifest = _manifest()
        await runner._run_terminal_evidence(
            manifest,
            product,
            project,
            _plan(),
            _proof(),
            90.0,
            "go",
        )
        assert manifest.extra["requirement_trace"]["mode"] == expected_mode

    monkeypatch.setattr(
        "skyn3t.studio.runner.source_tree_snapshot",
        lambda *args, **kwargs: {
            "valid": False,
            "algorithm": "source-tree-sha256-v1",
            "sha256": "",
        },
    )
    partial_manifest = _manifest()
    _, partial_verdict, _ = await runner._run_terminal_evidence(
        partial_manifest,
        partial,
        project,
        _plan(),
        _proof(),
        90.0,
        "go",
    )
    assert partial_verdict == "go"
    assert partial_manifest.extra["requirement_trace"]["mode"] == "partial"
    assert calls == []


async def test_safe_must_runs_once_after_consistency_and_should_route_does_not(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project)
    runner = _runner(tmp_path)
    events: list[str] = []

    def stabilize(*args, **kwargs):
        events.append("stabilize")
        return False, False, "not needed"

    def consistency(self, project_dir, plan, manifest, verdict):
        events.append("consistency")
        return verdict

    def prove(*args, **kwargs):
        events.append("proof")
        return _proof()

    class _ForbiddenCoordinator:
        async def run(self, *args, **kwargs):
            raise AssertionError("should-level route must not invoke Playwright")

    monkeypatch.setattr(
        "skyn3t.studio.runner.stabilize_node_dependencies",
        stabilize,
    )
    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        consistency,
    )
    monkeypatch.setattr("skyn3t.studio.runner.proof_run", prove)
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _ForbiddenCoordinator,
    )
    product = _spec(
        [
            RequirementRecord("Build", acceptance_ids=["proof:build"]),
            RequirementRecord(
                "Optional page",
                priority="should",
                acceptance_ids=["ui:route:/optional"],
            ),
        ]
    )
    manifest = _manifest()
    manifest.extra["proof_ladder"] = {"run_id": "stale-prior-ladder"}

    score, verdict, final_proof = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan(),
        _proof(),
        96.0,
        "go",
    )

    assert events == ["stabilize", "consistency", "proof"]
    assert final_proof.passed is True
    assert score == 96.0
    assert verdict == "go"
    assert manifest.extra["requirement_trace"]["go_eligible"] is True
    assert (
        manifest.extra["requirement_evidence_binding"]["evidence_run_id"]
        != "stale-prior-ladder"
    )

    (project / "app.py").write_text("print('changed after binding')\n")
    settled_score, settled_verdict = runner._settle_requirement_trace_delivery(
        manifest,
        project,
        "python",
        source_tree_snapshot(project),
        score,
        verdict,
        74.0,
    )
    assert (settled_score, settled_verdict) == (74.0, "no_go")
    assert manifest.extra["requirement_trace"]["status"] == "stale"
    summary = manifest.extra["requirement_trace"]["summary"]
    assert summary["total"] == summary["must_total"] == 1
    assert summary["proven"] == summary["must_proven"] == 0
    assert summary["must_stale"] == summary["blocking_failed"] == 1


async def test_unsupported_must_is_missing_and_never_replayed(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project)
    runner = _runner(tmp_path)
    product = _spec(
        [
            RequirementRecord(
                "Build and drive MCP",
                acceptance_ids=["proof:build", "gate:mcp"],
            )
        ]
    )
    manifest = _manifest()

    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.proof_run",
        lambda *args, **kwargs: _proof(),
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.stabilize_node_dependencies",
        lambda *args, **kwargs: (False, False, "no dependency phase"),
    )
    score, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan(),
        _proof(),
        96.0,
        "go",
    )

    trace = manifest.extra["requirement_trace"]
    assert verdict == "no_go"
    assert score == 74.0
    assert trace["evidence"]["proof:build"]["status"] == "passed"
    assert trace["evidence"]["gate:mcp"]["status"] == "missing"


@pytest.mark.parametrize(
    "acceptance_id",
    [
        "proof:overall",
        "proof:entrypoint",
        "gate:headless",
        "gate:qa-playtest",
        "gate:rag",
        "gate:workflow",
        "gate:cli",
        "gate:cli-playtest",
        "gate:cli-playtest:smoke",
        "mobile:maestro:smoke.yaml",
    ],
)
async def test_unsafe_acceptance_families_never_invoke_runtime_or_agents(
    tmp_path,
    monkeypatch,
    acceptance_id,
):
    project = tmp_path / "project"
    _write_source(project)
    runner = _runner(tmp_path)
    product = _spec(
        [RequirementRecord("Unsafe replay", acceptance_ids=[acceptance_id])]
    )
    manifest = _manifest()

    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe acceptance evidence must never be replayed")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )
    monkeypatch.setattr("skyn3t.studio.runner.proof_run", forbidden)
    monkeypatch.setattr(
        "skyn3t.studio.runner.stabilize_node_dependencies",
        forbidden,
    )
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        forbidden,
    )
    monkeypatch.setattr(runner.orchestrator, "submit", forbidden_async)

    _, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan(),
        _proof(),
        96.0,
        "go",
    )

    assert verdict == "no_go"
    assert (
        manifest.extra["requirement_trace"]["evidence"][acceptance_id]["status"]
        == "missing"
    )


async def test_non_docker_detail_is_missing_and_final_failure_remains_no_go(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project)
    runner = _runner(tmp_path)
    product = _spec(
        [RequirementRecord("Build", acceptance_ids=["proof:build"])]
    )

    local_manifest = _manifest()
    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.stabilize_node_dependencies",
        lambda *args, **kwargs: (False, False, "not needed"),
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.proof_run",
        lambda *args, **kwargs: _proof(backend="local"),
    )
    _, verdict, _ = await runner._run_terminal_evidence(
        local_manifest,
        product,
        project,
        _plan(),
        _proof(),
        96.0,
        "go",
    )
    assert verdict == "no_go"
    assert (
        local_manifest.extra["requirement_trace"]["evidence"]["proof:build"][
            "status"
        ]
        == "missing"
    )

    failed_manifest = _manifest()
    monkeypatch.setattr(
        "skyn3t.studio.runner.proof_run",
        lambda *args, **kwargs: _proof(passed=False),
    )
    _, failed_verdict, final_proof = await runner._run_terminal_evidence(
        failed_manifest,
        product,
        project,
        _plan(),
        _proof(),
        96.0,
        "go",
    )
    assert final_proof.passed is False
    assert failed_verdict == "no_go"


async def test_source_mutation_during_final_proof_fails_closed(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project)
    runner = _runner(tmp_path)
    product = _spec(
        [RequirementRecord("Build", acceptance_ids=["proof:build"])]
    )
    manifest = _manifest()

    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.stabilize_node_dependencies",
        lambda *args, **kwargs: (False, False, "not needed"),
    )

    def mutate(*args, **kwargs):
        (project / "app.py").write_text("print('mutated during proof')\n")
        return _proof()

    monkeypatch.setattr("skyn3t.studio.runner.proof_run", mutate)

    _, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan(),
        _proof(),
        96.0,
        "go",
    )

    assert verdict == "no_go"
    assert "authored source changed during final proof" in (
        manifest.extra["requirement_trace_evidence_errors"]
    )
    assert manifest.extra["requirement_trace"]["go_eligible"] is False


async def test_selected_web_route_passes_with_bounded_real_shaped_evidence(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project, web=True)
    runner = _runner(tmp_path)
    product = _spec(
        [RequirementRecord("Home page", acceptance_ids=["ui:route:/"])]
    )
    manifest = _manifest("static")
    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )

    class _Coordinator:
        async def run(self, project_dir, stack, **kwargs):
            return _passing_ladder(Path(project_dir), stack, list(kwargs["routes"]))

    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _Coordinator,
    )

    _, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan("static"),
        _proof(),
        96.0,
        "go",
    )

    trace = manifest.extra["requirement_trace"]
    assert verdict == "go"
    assert trace["go_eligible"] is True
    assert trace["evidence"]["ui:route:/"]["status"] == "passed"


async def test_route_runtime_mutation_fails_closed_without_source_false_positive(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project, web=True)
    runner = _runner(tmp_path)
    product = _spec(
        [RequirementRecord("Home page", acceptance_ids=["ui:route:/"])]
    )
    manifest = _manifest("static")

    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )

    class _Coordinator:
        async def run(self, project_dir, stack, **kwargs):
            dist = Path(project_dir) / "dist"
            dist.mkdir(exist_ok=True)
            (dist / "bundle.js").write_text("runtime changed")
            return _passing_ladder(Path(project_dir), stack, list(kwargs["routes"]))

    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _Coordinator,
    )

    _, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan("static"),
        _proof(),
        96.0,
        "go",
    )

    assert verdict == "no_go"
    assert "runtime inputs changed during final UI proof" in (
        manifest.extra["requirement_trace_evidence_errors"]
    )
    assert not any(
        "authored source changed" in error
        for error in manifest.extra["requirement_trace_evidence_errors"]
    )


async def test_runtime_fingerprint_exception_persists_fail_closed_trace(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project, web=True)
    runner = _runner(tmp_path)
    product = _spec(
        [RequirementRecord("Home page", acceptance_ids=["ui:route:/"])]
    )
    manifest = _manifest("static")

    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.preview_input_fingerprint",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fingerprint broke")),
    )

    _, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan("static"),
        _proof(),
        96.0,
        "go",
    )

    assert verdict == "no_go"
    assert manifest.extra["requirement_trace"]["go_eligible"] is False
    assert "fingerprint broke" in " ".join(
        manifest.extra["requirement_trace_evidence_errors"]
    )


async def test_global_ladder_rejects_malformed_source_snapshot_without_contract(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project, web=True)
    runner = _runner(tmp_path, proof_ladder_required=True)
    manifest = _manifest("static")
    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.source_tree_snapshot",
        lambda *args, **kwargs: {
            "valid": True,
            "algorithm": "wrong-algorithm",
            "sha256": "a" * 64,
            "file_count": 1,
            "byte_count": 10,
        },
    )
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid source must not execute global proof")
        ),
    )

    _, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        None,
        project,
        _plan("static"),
        _proof(),
        96.0,
        "go",
    )

    assert verdict == "no_go"
    assert manifest.extra["proof_ladder"]["status"] == "failed"


async def test_global_maestro_runs_once_but_stays_missing_to_registry(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project)
    runner = _runner(tmp_path, proof_ladder_required=True)
    product = _spec([
        RequirementRecord(
            "Mobile smoke",
            acceptance_ids=["mobile:maestro:smoke.yaml"],
        )
    ])
    manifest = _manifest("react_native")
    calls: list[str] = []
    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        lambda self, project_dir, plan, manifest, verdict: verdict,
    )

    class _Coordinator:
        async def run(self, project_dir, stack, **kwargs):
            calls.append("global-maestro")
            return _passing_maestro_ladder(Path(project_dir))

    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _Coordinator,
    )

    _, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan("react_native"),
        _proof(),
        96.0,
        "go",
    )

    evidence = manifest.extra["requirement_trace"]["evidence"]
    assert calls == ["global-maestro"]
    assert verdict == "no_go"
    assert evidence["mobile:maestro:smoke.yaml"]["status"] == "missing"


async def test_moved_global_ladder_runs_once_after_final_consistency_without_upgrade(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_source(project, web=True)
    runner = _runner(tmp_path, proof_ladder_required=True)
    product = _spec(
        [RequirementRecord("Build", acceptance_ids=["proof:build"])]
    )
    manifest = _manifest("static")
    events: list[str] = []

    monkeypatch.setattr(
        "skyn3t.studio.runner.stabilize_node_dependencies",
        lambda *args, **kwargs: (
            events.append("stabilize") is not None,
            True,
            "ok",
        ),
    )

    def consistency(self, project_dir, plan, manifest, verdict):
        events.append("consistency")
        return verdict

    monkeypatch.setattr(
        StudioRunner,
        "_final_consistency_check",
        consistency,
    )
    monkeypatch.setattr(
        "skyn3t.studio.runner.proof_run",
        lambda *args, **kwargs: (events.append("proof"), _proof())[1],
    )
    fingerprint = {
        "algorithm": "preview-input-sha256-v1",
        "sha256": "a" * 64,
        "file_count": 1,
        "byte_count": 20,
    }
    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.preview_input_fingerprint",
        lambda *args, **kwargs: dict(fingerprint),
    )

    class _Coordinator:
        async def run(self, project_dir, stack, **kwargs):
            events.append("ladder")
            return _passing_ladder(Path(project_dir), stack, list(kwargs["routes"]))

    monkeypatch.setattr(
        "skyn3t.studio.preview_supervisor.ProofLadderCoordinator",
        _Coordinator,
    )

    _, verdict, _ = await runner._run_terminal_evidence(
        manifest,
        product,
        project,
        _plan("static"),
        _proof(),
        96.0,
        "no_go",
    )

    assert events == ["stabilize", "consistency", "proof", "ladder"]
    assert verdict == "no_go"
    assert manifest.extra["proof_ladder"]["run_id"] == "final-route-evidence"
