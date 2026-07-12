from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.studio.proof_run import ProofResult
from skyn3t.web.deps import BuildRecord
from skyn3t.web.routes import (
    ProjectReverifyError,
    build_router,
    delete_project,
    list_projects,
    reverify_project,
)
from skyn3t.worktree import SOURCE_TREE_DIGEST_ALGORITHM, source_tree_snapshot


class _Memory:
    def __init__(self, row: dict | None = None) -> None:
        self.row = row
        self.saved: list[dict] = []

    async def get_build(self, build_id: str):
        if self.row and self.row.get("build_id") == build_id:
            return dict(self.row)
        return None

    async def latest_builds_by_slug(self, slugs: list[str]):
        if self.row and self.row.get("slug") in slugs:
            return [dict(self.row)]
        return []

    async def save_build(self, **fields) -> None:
        self.saved.append(fields)
        self.row = dict(fields)


def _settings(tmp_path: Path) -> SimpleNamespace:
    projects = tmp_path / "Projects"
    projects.mkdir()
    return SimpleNamespace(
        projects_dir=projects,
        execution_backend="inline",
        run_generated_tests=True,
        generated_test_timeout=17,
        run_generated_build=True,
        generated_build_timeout=29,
        mock_llm_proof_enabled=True,
        proof_install_python_deps=False,
        proof_python_deps_timeout=31,
        degraded_proof_score_cap=74.0,
        auth_token="",
    )


def _state(tmp_path: Path, *, memory=None) -> SimpleNamespace:
    return SimpleNamespace(
        settings=_settings(tmp_path),
        builds={},
        memory=memory,
        preview_signing_key=b"r" * 32,
    )


def test_reverify_double_swap_failure_preserves_original_backup(tmp_path, monkeypatch):
    import skyn3t.web.routes as routes

    project = tmp_path / "project"
    staging_root = tmp_path / ".staging"
    candidate = staging_root / "candidate"
    project.mkdir()
    candidate.mkdir(parents=True)
    (project / "original.txt").write_text("original", encoding="utf-8")
    (candidate / "candidate.txt").write_text("candidate", encoding="utf-8")
    real_replace = routes.os.replace
    calls = 0

    def fail_candidate_and_rollback(source, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_replace(source, target)
        raise OSError("simulated swap failure")

    monkeypatch.setattr(routes.os, "replace", fail_candidate_and_rollback)

    with pytest.raises(ProjectReverifyError) as raised:
        routes._promote_reverify_candidate(project, candidate, staging_root)

    assert raised.value.preserve_staging is True
    backup = staging_root / "original"
    assert raised.value.recovery_path == str(backup)
    assert (backup / "original.txt").read_text(encoding="utf-8") == "original"
    assert not project.exists()


def test_reverify_explains_when_a_preview_locks_the_project(tmp_path, monkeypatch):
    import skyn3t.web.routes as routes

    project = tmp_path / "project"
    staging_root = tmp_path / ".staging"
    candidate = staging_root / "candidate"
    project.mkdir()
    candidate.mkdir(parents=True)

    def locked(*_args, **_kwargs):
        raise PermissionError(13, "project is in use")

    monkeypatch.setattr(routes.os, "replace", locked)

    with pytest.raises(ProjectReverifyError, match="preview or server process"):
        routes._promote_reverify_candidate(project, candidate, staging_root)


def _write_project(
    state: SimpleNamespace,
    slug: str,
    *,
    status: str = "cancelled",
    review_verdict: str = "go",
    with_file: bool = True,
) -> tuple[Path, dict]:
    project = state.settings.projects_dir / slug
    project.mkdir()
    manifest = {
        "build_id": f"build-{slug}",
        "slug": slug,
        "brief": "a complete local project",
        "stack": "static",
        "status": status,
        "verdict": "no_go",
        "score": 41.0,
        "cost_usd": 0.75,
        "artifact_dir": str(project),
        "files": ["stale.txt"],
        "stages": [
            {
                "name": "review",
                "agent_type": "reviewer",
                "capability": "review",
                "status": "completed",
                "score": 80.0,
                "output_summary": {
                    "score": 80.0,
                    "verdict": review_verdict,
                    "gaps": [],
                },
            }
        ],
        "extra": {
            "build_cost_usd": 0.75,
            "wasted_usd": 0.75,
            "non_shippable_spend_usd": 0.75,
            "cancellation": {
                "cancelled_at": "2026-07-10T12:00:00+00:00",
                "recovery": [{"path": "data/recovery/build"}],
            },
        },
    }
    (project / "skyn3t_manifest.json").write_text(json.dumps(manifest))
    if with_file:
        (project / "index.html").write_text(
            "<!doctype html><html><body><main class='grid'>"
            "<h1>Complete app</h1><a href='/'>Home</a></main></body></html>"
        )
    if isinstance(getattr(state, "memory", None), _Memory):
        memory = state.memory
        if memory.row and memory.row.get("build_id") == manifest["build_id"]:
            memory.row.update({
                "slug": slug,
                "brief": manifest["brief"],
                "stack": manifest["stack"],
                "artifact_dir": str(project),
                "manifest": json.loads(json.dumps(manifest)),
            })
    return project, manifest


def _wire_passing_proof(monkeypatch, *, detail=None, on_proof=None) -> None:
    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs",
        lambda *_args, **_kwargs: {},
    )

    def passing(root, **_kwargs):
        if on_proof is not None:
            on_proof(Path(root))
        return ProofResult(
            passed=True,
            mode="local",
            files_total=2,
            files_substantive=2,
            score=90.0,
            detail=detail or {"build": "passed", "tests": "passed"},
        )

    monkeypatch.setattr("skyn3t.studio.proof_run.proof_run", passing)


@pytest.mark.asyncio
async def test_reverify_promotes_with_local_proof_and_persists_evidence(
    tmp_path, monkeypatch
):
    memory = _Memory({
        "build_id": "build-recover",
        "slug": "recover",
        "status": "cancelled",
    })
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, "recover")
    source = project / "src" / "app.js"
    source.parent.mkdir(parents=True)
    source.write_text("export const app = true;\n", encoding="utf-8")
    live = BuildRecord(
        build_id="build-recover",
        brief="a complete local project",
        slug="recover",
        stack="static",
        status="cancelled",
        cost_usd=0.75,
    )
    state.builds[live.build_id] = live

    main_thread = threading.get_ident()
    calls: dict = {}

    def fake_repairs(root, *, stack):
        calls["repair_thread"] = threading.get_ident()
        calls["repair"] = (Path(root), stack)
        return {"npm_deps_added": ["package.json"]}

    def fake_proof(root, **kwargs):
        calls["proof_thread"] = threading.get_ident()
        calls["proof"] = (Path(root), kwargs)
        for generated in (
            Path(root) / "dist" / "index.html",
            Path(root) / ".next" / "cache" / "entry",
            Path(root) / ".astro" / "content.d.ts",
            Path(root) / "node_modules" / "pkg" / "index.js",
        ):
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text("generated", encoding="utf-8")
        return ProofResult(
            passed=True,
            mode="local",
            files_total=2,
            files_substantive=2,
            score=90.0,
            detail={"build": "passed", "tests": "passed"},
        )

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs", fake_repairs
    )
    monkeypatch.setattr("skyn3t.studio.proof_run.proof_run", fake_proof)

    response = await reverify_project(state, "recover")

    assert response["promoted"] is True
    assert response["skyn3t_model_invocations"] == 0
    assert response["execution"]["external_cost_usd"] is None
    assert response["execution"]["project_command_network_isolation"] == "not_enforced"
    assert response["gates"]["security"]["ok"] is True
    assert response["gates"]["web_polish"]["ok"] is True
    assert response["gates"]["runtime_liveness"]["ok"] is True
    assert all(response["promotion_checks"]["candidate_gates"].values())
    assert response["status"] == "completed"
    assert response["verdict"] == "go"
    assert response["score"] == 82.0
    repair_root, repair_stack = calls["repair"]
    assert repair_stack == "static"
    assert repair_root != project
    assert repair_root.name == "candidate"
    assert repair_root.parent.name.startswith(".skyn3t-reverify-recover-")
    assert calls["proof"][0] == repair_root
    assert calls["repair_thread"] == calls["proof_thread"] != main_thread
    proof_kwargs = calls["proof"][1]
    assert proof_kwargs["execution_backend"] == "inline"
    assert proof_kwargs["run_tests"] is True
    assert proof_kwargs["test_timeout"] == 17
    assert proof_kwargs["run_build"] is True
    assert proof_kwargs["build_timeout"] == 29
    assert proof_kwargs["install_python_deps"] is False
    assert proof_kwargs["python_deps_timeout"] == 31

    persisted = json.loads((project / "skyn3t_manifest.json").read_text())
    assert persisted["status"] == "completed"
    assert persisted["verdict"] == "go"
    assert persisted["score"] == 82.0
    assert persisted["cost_usd"] == 0.75
    assert persisted["extra"]["build_cost_usd"] == 0.75
    assert persisted["extra"]["cancellation"]["recovery"] == [
        {"path": "data/recovery/build"}
    ]
    assert "wasted_usd" not in persisted["extra"]
    assert "non_shippable_spend_usd" not in persisted["extra"]
    assert persisted["extra"]["reverify"]["schema_version"] == 2
    assert (
        persisted["extra"]["reverify"]["execution"]["skyn3t_model_invocations"]
        == 0
    )
    assert persisted["extra"]["reverify"]["promoted"] is True
    assert persisted["extra"]["reverify"]["repairs_committed"] is True
    assert persisted["extra"]["reverify"]["gates"]["runtime_liveness"]["ok"] is True
    assert persisted["extra"]["reverify"]["review"]["binding"] == "legacy_durable_brief"
    assert persisted["extra"]["reverify"]["review"]["approved_current_tree"] is False
    assert persisted["extra"]["reverify"]["candidate"]["unchanged_during_proof"] is True
    assert persisted["extra"]["proof"]["passed"] is True
    assert persisted["files"] == ["index.html", "src/app.js"]
    assert not list(state.settings.projects_dir.glob(".skyn3t-reverify-*"))

    assert memory.saved[-1]["status"] == "completed"
    assert memory.saved[-1]["manifest"] == persisted
    assert live.status == "completed"
    assert live.verdict == "go"
    assert live.score == 82.0
    assert live.cost_usd == 0.75


@pytest.mark.asyncio
async def test_reverify_never_promotes_without_completed_review_go(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    project, _ = _write_project(state, "review-blocked", review_verdict="no_go")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("proof must not run without a reviewer GO")

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs",
        forbidden,
    )
    monkeypatch.setattr(
        "skyn3t.studio.proof_run.proof_run",
        forbidden,
    )

    with pytest.raises(ProjectReverifyError, match="review with verdict go"):
        await reverify_project(state, "review-blocked")

    persisted = json.loads((project / "skyn3t_manifest.json").read_text())
    assert persisted["status"] == "cancelled"
    assert persisted["verdict"] == "no_go"
    assert persisted["score"] == 41.0
    assert persisted["extra"]["wasted_usd"] == 0.75
    assert persisted["extra"]["non_shippable_spend_usd"] == 0.75
    assert persisted["extra"]["cancellation"]

    row = (await list_projects(state))["projects"][0]
    assert row["can_reverify"] is False
    assert "review with verdict go" in row["reverify_reason"]


@pytest.mark.asyncio
async def test_reverify_refuses_active_and_manifestless_projects(tmp_path, monkeypatch):
    state = _state(tmp_path)
    _write_project(state, "busy")
    state.builds["newer"] = BuildRecord(
        build_id="newer",
        brief="busy",
        slug="busy",
        status="running",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("repairs must not run")

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs", forbidden
    )
    with pytest.raises(ProjectReverifyError, match="active build"):
        await reverify_project(state, "busy")

    manifestless = state.settings.projects_dir / "manifestless"
    manifestless.mkdir()
    (manifestless / "index.html").write_text("<main>partial</main>")
    with pytest.raises(ProjectReverifyError, match="no valid build manifest"):
        await reverify_project(state, "manifestless")


@pytest.mark.asyncio
async def test_project_rows_expose_reverify_eligibility(tmp_path):
    state = _state(
        tmp_path,
        memory=_Memory({
            "build_id": "active-no-go-rebuild",
            "slug": "active-no-go",
            "status": "running",
        }),
    )
    _write_project(state, "eligible")
    _write_project(state, "delivered", status="completed")
    _write_project(state, "active-no-go", status="completed_no_go")
    delivered = json.loads(
        (state.settings.projects_dir / "delivered" / "skyn3t_manifest.json").read_text()
    )
    delivered["verdict"] = "go"
    (state.settings.projects_dir / "delivered" / "skyn3t_manifest.json").write_text(
        json.dumps(delivered)
    )
    (state.settings.projects_dir / "orphan").mkdir()

    rows = {row["slug"]: row for row in (await list_projects(state))["projects"]}

    assert rows["eligible"]["can_reverify"] is True
    assert rows["eligible"]["reverify_reason"] == ""
    assert rows["delivered"]["can_reverify"] is False
    assert rows["delivered"]["reverify_reason"] == "The project is already delivered."
    assert rows["active-no-go"]["can_reverify"] is False
    assert rows["active-no-go"]["reverify_reason"] == "The build is still active."
    assert rows["orphan"]["can_reverify"] is False
    assert rows["orphan"]["reverify_reason"] == "No build manifest is available."


@pytest.mark.asyncio
async def test_incomplete_project_row_exposes_compact_local_reverify_result(tmp_path):
    state = _state(tmp_path)
    project, manifest = _write_project(state, "proof-visible")
    manifest["extra"]["reverify"] = {
        "verified_at": "2026-07-11T15:00:00+00:00",
        "promoted": False,
        "reason": "fresh review needed for changed source",
        "proof": {"passed": True, "score": 100.0, "detail": {"large": "omitted"}},
        "execution": {"skyn3t_model_invocations": 0},
    }
    (project / "skyn3t_manifest.json").write_text(json.dumps(manifest))

    row = (await list_projects(state))["projects"][0]

    assert row["local_reverify"] == {
        "verified_at": "2026-07-11T15:00:00+00:00",
        "promoted": False,
        "review_refreshed": False,
        "proof_passed": True,
        "score": None,
        "reason": "fresh review needed for changed source",
        "skyn3t_model_invocations": 0,
    }


@pytest.mark.asyncio
async def test_reverify_rejects_identity_mismatch_before_repairs(tmp_path, monkeypatch):
    state = _state(tmp_path)
    project, manifest = _write_project(state, "identity")
    manifest["slug"] = "somewhere-else"
    (project / "skyn3t_manifest.json").write_text(json.dumps(manifest))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("repairs must not run for an invalid identity")

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs", forbidden
    )
    with pytest.raises(ProjectReverifyError, match="slug does not match"):
        await reverify_project(state, "identity")


@pytest.mark.asyncio
async def test_reverify_rejects_missing_or_duplicate_build_id(tmp_path, monkeypatch):
    state = _state(tmp_path)
    missing_project, missing = _write_project(state, "missing-id")
    missing["build_id"] = ""
    (missing_project / "skyn3t_manifest.json").write_text(json.dumps(missing))
    with pytest.raises(ProjectReverifyError, match="build_id is required"):
        await reverify_project(state, "missing-id")

    first, first_manifest = _write_project(state, "duplicate-one")
    second, second_manifest = _write_project(state, "duplicate-two")
    second_manifest["build_id"] = first_manifest["build_id"]
    (second / "skyn3t_manifest.json").write_text(json.dumps(second_manifest))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("repairs must not run for a duplicate build id")

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs", forbidden
    )
    with pytest.raises(ProjectReverifyError, match="owned by another project"):
        await reverify_project(state, second.name)
    assert first.is_dir()


@pytest.mark.asyncio
async def test_reverify_durable_review_mismatch_cannot_promote(tmp_path, monkeypatch):
    memory = _Memory({"build_id": "build-review-mismatch", "slug": "review-mismatch"})
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, "review-mismatch")
    memory.row["manifest"]["stages"][0]["output_summary"]["score"] = 12.0
    _wire_passing_proof(monkeypatch)

    response = await reverify_project(state, project.name)

    assert response["promoted"] is False
    assert response["review"]["binding"] == "legacy_review_mismatch"
    assert "durably corroborated" in response["reason"]
    persisted = json.loads((project / "skyn3t_manifest.json").read_text())
    assert persisted["status"] == "cancelled"


@pytest.mark.asyncio
async def test_reverify_manifest_only_reviewer_go_cannot_promote(tmp_path, monkeypatch):
    state = _state(tmp_path)
    project, _ = _write_project(state, "manifest-only")
    _wire_passing_proof(monkeypatch)

    response = await reverify_project(state, project.name)

    assert response["promoted"] is False
    assert response["review"]["binding"] == "unbound"
    assert response["review"]["source"] == "build_manifest"


@pytest.mark.asyncio
async def test_reverify_exact_tree_mismatch_never_downgrades_to_legacy(
    tmp_path, monkeypatch
):
    memory = _Memory({"build_id": "build-exact-mismatch", "slug": "exact-mismatch"})
    state = _state(tmp_path, memory=memory)
    project, manifest = _write_project(state, "exact-mismatch")
    summary = manifest["stages"][0]["output_summary"]
    summary["source_tree_sha256"] = "0" * 64
    summary["source_tree_digest_algorithm"] = SOURCE_TREE_DIGEST_ALGORITHM
    (project / "skyn3t_manifest.json").write_text(json.dumps(manifest))
    _wire_passing_proof(monkeypatch)

    response = await reverify_project(state, project.name)

    assert response["promoted"] is False
    assert response["review"]["binding"] == "exact_tree_mismatch"


@pytest.mark.asyncio
async def test_reverify_refreshes_durable_stale_review_on_the_verified_tree(
    tmp_path, monkeypatch
):
    memory = _Memory({"build_id": "build-fresh-review", "slug": "fresh-review"})
    state = _state(tmp_path, memory=memory)
    project, manifest = _write_project(state, "fresh-review")
    source = project / "src"
    source.mkdir()
    for index in range(5):
        (source / f"module-{index}.js").write_text(
            f"export const value{index} = {index};\n", encoding="utf-8"
        )
    (project / "package.json").write_text('{"name":"fresh-review"}', encoding="utf-8")
    summary = manifest["stages"][0]["output_summary"]
    summary["source_tree_sha256"] = "0" * 64
    summary["source_tree_digest_algorithm"] = SOURCE_TREE_DIGEST_ALGORITHM
    (project / "skyn3t_manifest.json").write_text(json.dumps(manifest))
    memory.row["manifest"] = json.loads(json.dumps(manifest))
    _wire_passing_proof(monkeypatch)

    response = await reverify_project(state, project.name)

    assert response["promoted"] is True
    assert response["review_refreshed"] is True
    assert response["review"]["binding"] == "exact_tree"
    persisted = json.loads((project / "skyn3t_manifest.json").read_text())
    refreshed = persisted["stages"][-1]
    assert refreshed["name"] == "reverify-review"
    assert refreshed["output_summary"]["review_scope"] == "local_reverify_fresh_tree"
    assert refreshed["output_summary"]["source_tree_sha256"]


@pytest.mark.asyncio
async def test_reverify_invalid_new_review_snapshot_never_uses_legacy(
    tmp_path, monkeypatch
):
    memory = _Memory({"build_id": "build-invalid-tree", "slug": "invalid-tree"})
    state = _state(tmp_path, memory=memory)
    project, manifest = _write_project(state, "invalid-tree")
    summary = manifest["stages"][0]["output_summary"]
    summary["source_tree_snapshot_valid"] = False
    summary["source_tree_sha256"] = ""
    summary["source_tree_digest_algorithm"] = SOURCE_TREE_DIGEST_ALGORITHM
    (project / "skyn3t_manifest.json").write_text(json.dumps(manifest))
    _wire_passing_proof(monkeypatch)

    response = await reverify_project(state, project.name)

    assert response["promoted"] is False
    assert response["review"]["binding"] == "review_tree_invalid"


@pytest.mark.asyncio
async def test_reverify_exact_tree_review_promotes_current_source(tmp_path, monkeypatch):
    memory = _Memory({"build_id": "build-exact", "slug": "exact"})
    state = _state(tmp_path, memory=memory)
    project, manifest = _write_project(state, "exact")
    snapshot = source_tree_snapshot(project)
    summary = manifest["stages"][0]["output_summary"]
    summary["source_tree_sha256"] = snapshot["sha256"]
    summary["source_tree_digest_algorithm"] = snapshot["algorithm"]
    (project / "skyn3t_manifest.json").write_text(json.dumps(manifest))
    _wire_passing_proof(monkeypatch)

    response = await reverify_project(state, project.name)

    assert response["promoted"] is True
    assert response["review"]["binding"] == "exact_tree"
    assert response["review"]["approved_current_tree"] is True


@pytest.mark.asyncio
async def test_reverify_source_mutation_during_proof_blocks_promotion(
    tmp_path, monkeypatch
):
    memory = _Memory({"build_id": "build-mutated", "slug": "mutated"})
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, "mutated")
    _wire_passing_proof(
        monkeypatch,
        on_proof=lambda root: (root / "index.html").write_text("changed source"),
    )

    response = await reverify_project(state, project.name)

    assert response["promoted"] is False
    assert response["promotion_checks"]["source_unchanged_during_proof"] is False
    assert "source tree changed" in response["reason"]
    assert (project / "index.html").read_text() != "changed source"
    persisted = json.loads((project / "skyn3t_manifest.json").read_text())
    assert persisted["extra"]["reverify"]["repairs_committed"] is False


@pytest.mark.asyncio
async def test_reverify_stabilizes_lockfile_before_candidate_snapshot(
    tmp_path, monkeypatch
):
    memory = _Memory({"build_id": "build-lockfile", "slug": "lockfile"})
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, "lockfile")
    (project / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs",
        lambda *_args, **_kwargs: {},
    )

    def stabilize(root, **_kwargs):
        (Path(root) / "package-lock.json").write_text(
            '{"lockfileVersion": 3}', encoding="utf-8"
        )
        return True, True, "npm install ok"

    def passing(root, **_kwargs):
        assert (Path(root) / "package-lock.json").is_file()
        return ProofResult(
            passed=True,
            mode="local",
            files_total=3,
            files_substantive=2,
            score=90.0,
            detail={"build": "passed", "tests": "skipped"},
        )

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.stabilize_node_dependencies", stabilize
    )
    monkeypatch.setattr("skyn3t.studio.proof_run.proof_run", passing)

    response = await reverify_project(state, project.name)

    assert response["promoted"] is True
    assert response["promotion_checks"]["source_unchanged_during_proof"] is True
    assert (project / "package-lock.json").is_file()
    persisted = json.loads((project / "skyn3t_manifest.json").read_text())
    assert persisted["extra"]["reverify"]["dependency_stabilization"]["ran"] is True


@pytest.mark.asyncio
async def test_reverify_invalid_candidate_never_runs_project_commands(
    tmp_path, monkeypatch
):
    import skyn3t.web.routes as routes

    state = _state(tmp_path)
    project, _ = _write_project(state, "invalid-candidate")
    repaired = threading.Event()
    real_snapshot = routes.source_tree_snapshot

    def repairs(*_args, **_kwargs):
        repaired.set()
        return {}

    def snapshot(root):
        value = real_snapshot(root)
        if repaired.is_set() and Path(root).name == "candidate":
            value = dict(value)
            value["valid"] = False
            value["unsafe_aliases"] = ["src/escape"]
        return value

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid candidates must not execute project commands")

    monkeypatch.setattr(routes, "source_tree_snapshot", snapshot)
    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs", repairs
    )
    monkeypatch.setattr(
        "skyn3t.studio.proof_run.stabilize_node_dependencies", forbidden
    )
    monkeypatch.setattr("skyn3t.studio.proof_run.proof_run", forbidden)

    with pytest.raises(ProjectReverifyError, match="ambiguous or unreadable"):
        await reverify_project(state, project.name)

    persisted = json.loads((project / "skyn3t_manifest.json").read_text())
    assert "reverify" not in persisted["extra"]
    assert not list(state.settings.projects_dir.glob(".skyn3t-reverify-*"))


@pytest.mark.asyncio
async def test_reverify_declared_build_and_tests_must_not_be_skipped(
    tmp_path, monkeypatch
):
    memory = _Memory({"build_id": "build-skipped", "slug": "skipped"})
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, "skipped")
    (project / "package.json").write_text(
        json.dumps({
            "scripts": {"build": "vite build", "test": "vitest run"},
            "devDependencies": {"vitest": "1.0.0"},
        })
    )
    _wire_passing_proof(
        monkeypatch,
        detail={
            "build": "skipped",
            "tests": "skipped",
            "proof_environment": {
                "command_backend": "local",
                "degraded": True,
                "degraded_reasons": ["build skipped", "tests skipped"],
            },
        },
    )

    response = await reverify_project(state, project.name)

    assert response["promoted"] is False
    failures = response["promotion_checks"]["failures"]
    assert any("node build did not pass" in value for value in failures)
    assert any("node tests did not pass" in value for value in failures)


def test_reverify_accepts_separate_node_test_evidence_after_generic_skip(
    tmp_path, monkeypatch
):
    import skyn3t.web.routes as routes

    monkeypatch.setattr(
        routes,
        "_project_validation_requirements",
        lambda *_args, **_kwargs: {
            "node_build": True,
            "node_tests": True,
            "python_tests": False,
            "swift_build": False,
            "swift_tests": False,
        },
    )
    snapshot = {
        "valid": True,
        "algorithm": "source-tree-sha256-v1",
        "sha256": "verified-current-tree",
    }
    proof = SimpleNamespace(
        passed=True,
        detail={
            "build": "passed",
            "node_tests": "passed",
            "tests": "skipped",
            "proof_environment": {
                "degraded_reasons": ["tests skipped"],
            },
        },
    )
    gates = {
        name: {"ok": True, "skipped": False, "warnings": []}
        for name in ("security", "web_polish", "runtime_liveness")
    }

    result = routes._reverify_promotion_checks(
        tmp_path,
        "react",
        proof,
        gates,
        {"valid": True},
        snapshot,
        snapshot,
        snapshot,
        snapshot,
    )

    assert result["passed"] is True
    assert result["validation"]["node_tests"] == "passed"
    assert result["blocking_degradation"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_gate", "expected_failure"),
    [
        ("security", "security check failed"),
        ("web_polish", "web polish check failed"),
    ],
)
async def test_reverify_static_candidate_gates_block_promotion(
    tmp_path,
    monkeypatch,
    failed_gate,
    expected_failure,
):
    memory = _Memory({"build_id": f"build-{failed_gate}", "slug": failed_gate})
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, failed_gate)
    original_source = (project / "index.html").read_text(encoding="utf-8")
    _wire_passing_proof(monkeypatch)
    monkeypatch.setattr(
        "skyn3t.web.routes._run_reverify_runtime_liveness",
        lambda *_args, **_kwargs: {
            "ok": True,
            "skipped": False,
            "routes": [{"path": "/", "status": 200, "ok": True}],
            "dead_routes": [],
        },
    )
    failing = lambda *_args, **_kwargs: {  # noqa: E731 - injectable gate stub
        "ok": False,
        "skipped": False,
        "issues": [f"deliberate {failed_gate} failure"],
        "checked": ["index.html"],
    }
    monkeypatch.setattr(
        f"skyn3t.studio.{'security_check' if failed_gate == 'security' else 'web_polish_check'}."
        f"{'check_security' if failed_gate == 'security' else 'check_web_polish'}",
        failing,
    )

    response = await reverify_project(state, project.name)

    assert response["promoted"] is False
    assert response["promotion_checks"]["candidate_gates"][failed_gate] is False
    assert expected_failure in response["reason"]
    assert (project / "index.html").read_text(encoding="utf-8") == original_source
    assert not list(state.settings.projects_dir.glob(".skyn3t-reverify-*"))


@pytest.mark.asyncio
async def test_reverify_dead_runtime_route_blocks_promotion(tmp_path, monkeypatch):
    memory = _Memory({"build_id": "build-runtime-dead", "slug": "runtime-dead"})
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, "runtime-dead")
    original_source = (project / "index.html").read_text(encoding="utf-8")
    _wire_passing_proof(monkeypatch)
    monkeypatch.setattr(
        "skyn3t.web.routes._run_reverify_runtime_liveness",
        lambda *_args, **_kwargs: {
            "ok": False,
            "skipped": False,
            "reason": "1 runtime route(s) did not respond",
            "routes": [{"path": "/broken", "status": 500, "ok": False}],
            "dead_routes": ["/broken"],
        },
    )

    response = await reverify_project(state, project.name)

    assert response["promoted"] is False
    assert response["promotion_checks"]["candidate_gates"]["runtime_liveness"] is False
    assert "runtime liveness check failed: /broken" in response["reason"]
    assert response["gates"]["runtime_liveness"]["dead_routes"] == ["/broken"]
    assert (project / "index.html").read_text(encoding="utf-8") == original_source
    assert not list(state.settings.projects_dir.glob(".skyn3t-reverify-*"))


def test_reverify_runtime_probe_disables_secrets_and_stops_candidate(
    tmp_path,
    monkeypatch,
):
    import skyn3t.studio.app_runner as app_runner
    import skyn3t.studio.liveness as liveness
    import skyn3t.web.routes as routes

    project = tmp_path / "candidate"
    project.mkdir()
    (project / "index.html").write_text("<h1>Candidate</h1>", encoding="utf-8")
    calls: dict = {}
    running = SimpleNamespace(
        status="running",
        url="http://127.0.0.1:9876",
        detail={},
        kind="static",
        pid=None,
        log_path=None,
    )

    class FakeRunner:
        async def start(self, root, stack, **kwargs):
            calls["start"] = (Path(root), stack, kwargs)
            return running

        def stop(self, app):
            calls["stopped"] = app

    async def no_crawled_routes(_url):
        return []

    async def dead_report(_url, _routes):
        return liveness.LivenessReport(
            results=[
                liveness.RouteResult("/broken", "GET", 500, False, "page")
            ],
            total=1,
            ok=0,
            dead=1,
            dead_routes=["/broken"],
            health=0.0,
        )

    monkeypatch.setattr(app_runner, "AppRunner", FakeRunner)
    monkeypatch.setattr(liveness, "enumerate_routes", lambda *_args: [liveness.Route("/broken")])
    monkeypatch.setattr(liveness, "crawl_routes", no_crawled_routes)
    monkeypatch.setattr(liveness, "check_liveness", dead_report)

    result = routes._run_reverify_runtime_liveness(
        project,
        stack="static",
        settings=SimpleNamespace(generated_build_timeout=3),
    )

    assert result["ok"] is False
    assert result["dead_routes"] == ["/broken"]
    assert calls["start"][0] == project
    assert calls["start"][2]["allow_secret_passthrough"] is False
    assert calls["stopped"] is running


def test_app_run_spec_secret_override_wins_over_global_opt_in(tmp_path, monkeypatch):
    import skyn3t.studio.app_runner as app_runner

    (tmp_path / "index.html").write_text("<h1>Candidate</h1>", encoding="utf-8")
    monkeypatch.setenv("SKYN3T_PREVIEW_SECRET_PASSTHROUGH", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret-value")
    monkeypatch.setattr(
        app_runner,
        "needed_secret_names",
        lambda *_args, **_kwargs: {"OPENROUTER_API_KEY"},
    )

    spec = app_runner.build_run_spec(
        tmp_path,
        "static",
        port=9876,
        allow_secret_passthrough=False,
    )

    assert spec is not None
    assert "OPENROUTER_API_KEY" not in spec.env
    assert spec.injected == ()
    assert spec.missing_secrets == ("OPENROUTER_API_KEY",)


@pytest.mark.asyncio
async def test_cancelled_reverify_holds_claim_until_worker_finishes(
    tmp_path, monkeypatch
):
    memory = _Memory({"build_id": "build-lock", "slug": "lock"})
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, "lock")
    started = threading.Event()
    release = threading.Event()

    def blocking_proof(_root, **_kwargs):
        started.set()
        assert release.wait(5)
        return ProofResult(
            passed=True,
            mode="local",
            files_total=1,
            files_substantive=1,
            score=90.0,
        )

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr("skyn3t.studio.proof_run.proof_run", blocking_proof)
    request = asyncio.create_task(reverify_project(state, project.name))
    assert await asyncio.to_thread(started.wait, 2)
    request.cancel()
    await asyncio.sleep(0)

    with pytest.raises(ProjectReverifyError, match="already running"):
        await reverify_project(state, project.name)
    with pytest.raises(ValueError, match="still running"):
        await delete_project(state, project.name)

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    for _ in range(100):
        try:
            await delete_project(state, project.name)
            break
        except ValueError:
            await asyncio.sleep(0.01)
    else:
        raise AssertionError("reverify claim was not released after worker completion")


@pytest.mark.asyncio
async def test_cancelled_reverify_discards_staged_repairs_without_live_audit(
    tmp_path, monkeypatch
):
    memory = _Memory({"build_id": "build-cancel-stage", "slug": "cancel-stage"})
    state = _state(tmp_path, memory=memory)
    project, _ = _write_project(state, "cancel-stage")
    original_manifest = (project / "skyn3t_manifest.json").read_text()
    started = threading.Event()
    release = threading.Event()

    def repairs(root, **_kwargs):
        (Path(root) / "repaired.js").write_text("export const repaired = true\n")
        return {"files": ["repaired.js"]}

    def blocking_proof(root, **_kwargs):
        assert (Path(root) / "repaired.js").is_file()
        started.set()
        assert release.wait(5)
        return ProofResult(
            passed=True,
            mode="local",
            files_total=2,
            files_substantive=2,
            score=90.0,
        )

    monkeypatch.setattr(
        "skyn3t.studio.proof_run.apply_deterministic_repairs", repairs
    )
    monkeypatch.setattr("skyn3t.studio.proof_run.proof_run", blocking_proof)
    request = asyncio.create_task(reverify_project(state, project.name))
    assert await asyncio.to_thread(started.wait, 2)

    request.cancel()
    await asyncio.sleep(0)
    assert not (project / "repaired.js").exists()
    assert (project / "skyn3t_manifest.json").read_text() == original_manifest
    with pytest.raises(ProjectReverifyError, match="already running"):
        await reverify_project(state, project.name)

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    for _ in range(100):
        if not list(state.settings.projects_dir.glob(".skyn3t-reverify-*")):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("cancelled reverify staging tree was not removed")
    assert not (project / "repaired.js").exists()
    assert (project / "skyn3t_manifest.json").read_text() == original_manifest


def test_reverify_route_is_registered_before_project_file_catch_all(tmp_path):
    state = _state(tmp_path)
    router = build_router(state)
    signatures = [
        (route.path, frozenset(route.methods or ()))
        for route in router.routes
    ]

    reverify_index = signatures.index(
        ("/api/projects/{slug}/reverify", frozenset({"POST"}))
    )
    catch_all_index = signatures.index(
        ("/api/projects/{slug}/{path:path}", frozenset({"GET"}))
    )
    assert reverify_index < catch_all_index
