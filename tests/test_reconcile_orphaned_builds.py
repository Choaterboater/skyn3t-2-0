"""A build can't survive a server restart — any row left 'running' by a dead
server process must be reconciled to a terminal status on startup, else it
lingers as a phantom 'running' build forever (observed: abfe... ran 3.5h)."""

from __future__ import annotations

import json

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore
from skyn3t.studio.manifest import MANIFEST_FILENAME


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
        "extra": {},
    }))
    await store.save_build(
        build_id="b3",
        slug="zombie",
        brief="old build",
        stack="react",
        status="interrupted",
        artifact_dir=str(project),
        manifest={"build_id": "b3", "slug": "zombie", "status": "running", "extra": {}},
    )

    row = (await store.recent_builds(limit=1))[0]

    assert row["status"] == "interrupted"
