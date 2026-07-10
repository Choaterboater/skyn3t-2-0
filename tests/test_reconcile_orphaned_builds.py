"""A build can't survive a server restart — any row left 'running' by a dead
server process must be reconciled to a terminal status on startup, else it
lingers as a phantom 'running' build forever (observed: abfe... ran 3.5h)."""

from __future__ import annotations

import json

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore
from skyn3t.process_utils import is_process_alive
from skyn3t.studio.manifest import MANIFEST_FILENAME


def test_pid_liveness_probe_is_safe_for_current_process():
    import os

    assert is_process_alive(os.getpid()) is True
    assert is_process_alive(True) is False
    assert is_process_alive(0) is False
    assert is_process_alive(-1) is False


async def test_reconcile_marks_orphaned_running_as_interrupted(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    await store.save_build(build_id="zombie", slug="x", status="running")
    await store.save_build(build_id="done", slug="y", status="completed", verdict="go")

    n = await store.reconcile_orphaned_builds()

    assert n == 1  # only the orphaned 'running' row
    assert (await store.get_build("zombie"))["status"] == "interrupted"
    assert (await store.get_build("done"))["status"] == "completed"  # terminal row untouched


async def test_reconcile_noop_when_nothing_running(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    await store.save_build(build_id="done", slug="y", status="completed_no_go")
    assert await store.reconcile_orphaned_builds() == 0


async def test_latest_builds_by_slug_returns_newest_compact_db_record(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    await store.save_build(
        build_id="older",
        slug="same-project",
        status="completed",
        verdict="go",
        cost_usd=1.0,
        manifest={
            "build_id": "older",
            "slug": "same-project",
            "status": "completed",
            "verdict": "go",
        },
    )
    await store.save_build(
        build_id="newer",
        slug="same-project",
        status="cancelled",
        cost_usd=2.0,
        manifest={
            "build_id": "newer",
            "slug": "same-project",
            "status": "running",
            "extra": {
                "build_profile": "fast",
                "build_cost_usd": 2.0,
                "prompts": [{"stage": "code"}],
            },
        },
    )
    await store.save_build(build_id="other", slug="other-project", status="failed")
    await store.save_build(
        build_id="failed-stale-manifest",
        slug="failed-project",
        status="failed",
        cost_usd=3.0,
        manifest={
            "build_id": "failed-stale-manifest",
            "slug": "failed-project",
            "status": "running",
        },
    )

    rows = await store.latest_builds_by_slug(
        ["same-project", "failed-project", "missing"]
    )

    indexed = {row["slug"]: row for row in rows}
    assert set(indexed) == {"same-project", "failed-project"}
    assert indexed["same-project"]["build_id"] == "newer"
    assert indexed["same-project"]["status"] == "cancelled"
    assert indexed["same-project"]["cost_usd"] == 2.0
    assert indexed["same-project"]["build_profile"] == "fast"
    assert indexed["same-project"]["model_trace"]["prompt_count"] == 1
    assert indexed["failed-project"]["status"] == "failed"
    assert indexed["failed-project"]["cost_usd"] == 3.0
    await store.close()


async def test_reconcile_leaves_live_owner_interrupts_dead(tmp_path):
    """A running row owned by a LIVE process (current server or concurrent
    same-host build) is left; only a dead/unknown owner is interrupted (#25)."""
    import os
    import socket
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    host = socket.gethostname()
    await store.save_build(build_id="live", slug="x", status="running",
                           manifest={"extra": {"owner_pid": os.getpid(), "owner_host": host}})
    await store.save_build(build_id="dead", slug="y", status="running",
                           manifest={"extra": {"owner_pid": 2_147_483_646, "owner_host": host}})

    n = await store.reconcile_orphaned_builds()

    assert n == 1  # only the dead-owner row
    assert (await store.get_build("live"))["status"] == "running"     # live owner preserved
    assert (await store.get_build("dead"))["status"] == "interrupted"


async def test_reconcile_interrupts_same_host_permission_denied_owner(tmp_path, monkeypatch):
    """A same-host owner PID that cannot be signaled is not trustworthy.

    On macOS/sandboxed launches, a dead SkyN3t PID can be reused by a process the
    web server cannot signal. Keeping that row as running creates a phantom
    build forever.
    """
    import os
    import socket

    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    host = socket.gethostname()
    await store.save_build(
        build_id="opaque",
        slug="x",
        status="running",
        manifest={"extra": {"owner_pid": 20485, "owner_host": host}},
    )

    def denied(_pid, _signal):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "kill", denied)

    n = await store.reconcile_orphaned_builds()

    assert n == 1
    assert (await store.get_build("opaque"))["status"] == "interrupted"


async def test_recent_builds_exposes_manifest_classification(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    await store.save_build(
        build_id="b1",
        slug="x",
        brief="a dashboard",
        stack="react",
        status="running",
        manifest={
            "extra": {
                "classification": {"app_type": "dashboard", "engine": "dom"},
                "stack_selection": {"method": "keyword", "stack": "react"},
                "build_profile": "manual",
                "model_override": "openrouter/custom-model",
                "skills_used": ["react-ui"],
                "recall_used": [{"score": 0.91, "text": "prior"}],
                "proof": {"passed": True, "detail": {"build": "passed"}},
            }
        },
        score=91.0,
        verdict="go",
    )

    row = (await store.recent_builds(limit=1))[0]

    assert row["stack"] == "react"
    assert row["app_type"] == "dashboard"
    assert row["engine"] == "dom"
    assert row["stack_selection"]["stack"] == "react"
    assert row["build_profile"] == "manual"
    assert row["model_trace"]["model_override"] == "openrouter/custom-model"
    assert row["quality_scorecard"]["proof_passed"] is True
    assert row["quality_scorecard"]["skills_count"] == 1
    assert row["skills_used"] == ["react-ui"]


async def test_recent_builds_prefers_repaired_disk_manifest(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    project = tmp_path / "Projects" / "golf"
    project.mkdir(parents=True)
    disk_manifest = {
        "build_id": "b2",
        "slug": "golf",
        "brief": "adult beginner golf website",
        "stack": "nextjs",
        "status": "completed",
        "verdict": "go",
        "score": 100.0,
        "cost_usd": 0.12,
        "artifact_dir": str(project),
        "extra": {"proof": {"passed": True, "detail": {"build": "passed"}}},
    }
    (project / MANIFEST_FILENAME).write_text(json.dumps(disk_manifest))
    await store.save_build(
        build_id="b2",
        slug="golf",
        brief="adult beginner golf website",
        stack="nextjs",
        status="completed_no_go",
        verdict="no_go",
        score=49.0,
        cost_usd=0.12,
        artifact_dir=str(project),
        manifest={"build_id": "b2", "slug": "golf", "status": "completed_no_go",
                  "verdict": "no_go", "score": 49.0, "extra": {}},
    )

    row = (await store.recent_builds(limit=1))[0]

    assert row["status"] == "completed"
    assert row["verdict"] == "go"
    assert row["score"] == 100.0
    assert row["quality_scorecard"]["proof_passed"] is True


async def test_recent_builds_keeps_interrupted_status_over_stale_running_manifest(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    project = tmp_path / "Projects" / "zombie"
    project.mkdir(parents=True)
    (project / MANIFEST_FILENAME).write_text(json.dumps({
        "build_id": "b3",
        "slug": "zombie",
        "brief": "old build",
        "stack": "react",
        "status": "running",
        "extra": {"llm_backend": "codex_cli", "build_cost_usd": 0.0},
    }))
    await store.save_build(
        build_id="b3",
        slug="zombie",
        brief="old build",
        stack="react",
        status="interrupted",
        artifact_dir=str(project),
        manifest={
            "build_id": "b3",
            "slug": "zombie",
            "status": "running",
            "extra": {"llm_backend": "codex_cli", "build_cost_usd": 0.0},
        },
    )

    row = (await store.recent_builds(limit=1))[0]

    assert row["status"] == "interrupted"
    assert row["cost_truth"]["llm_cost_known"] is False
    assert row["cost_truth"]["llm_cost_usd"] is None


async def test_recent_builds_keeps_cancelled_status_over_stale_running_manifest(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    project = tmp_path / "Projects" / "cancelled"
    project.mkdir(parents=True)
    (project / MANIFEST_FILENAME).write_text(json.dumps({
        "build_id": "b4",
        "slug": "cancelled",
        "brief": "cancelled build",
        "stack": "react",
        "status": "running",
        "extra": {},
    }))
    await store.save_build(
        build_id="b4",
        slug="cancelled",
        brief="cancelled build",
        stack="react",
        status="cancelled",
        artifact_dir=str(project),
        manifest={"build_id": "b4", "slug": "cancelled", "status": "running", "extra": {}},
    )

    row = (await store.recent_builds(limit=1))[0]

    assert row["status"] == "cancelled"
