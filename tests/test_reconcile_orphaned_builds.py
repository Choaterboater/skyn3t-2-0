"""A build can't survive a server restart — any row left 'running' by a dead
server process must be reconciled to a terminal status on startup, else it
lingers as a phantom 'running' build forever (observed: abfe... ran 3.5h)."""

from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore


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
