"""Deleting a build row must not rmtree an artifact_dir another build owns.

Legacy same-slug rows can share one projects/<slug> folder, and a rebuild can
re-claim a trashed project's path while the stale history row still points at
it — in both cases delete_build's disk cleanup used to destroy a live app.
"""

from __future__ import annotations

import json

from skyn3t.config.settings import Settings
from skyn3t.memory.store import MemoryStore
from skyn3t.studio.manifest import MANIFEST_FILENAME


def _project_dir(tmp_path, name, manifest=None):
    pdir = tmp_path / "projects" / name
    pdir.mkdir(parents=True)
    (pdir / "app.py").write_text("print('hi')", encoding="utf-8")
    if manifest is not None:
        (pdir / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return pdir


async def test_delete_build_removes_dir_owned_by_the_deleted_row(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    pdir = _project_dir(tmp_path, "app", manifest={"build_id": "b1", "slug": "app"})
    await store.save_build(build_id="b1", slug="app", status="completed", artifact_dir=str(pdir))

    assert await store.delete_build("b1") is True
    assert not pdir.exists()


async def test_delete_build_removes_manifestless_dir(tmp_path):
    # Best-effort contract: a dir with no manifest is still the row's to clean.
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    pdir = _project_dir(tmp_path, "app")
    await store.save_build(build_id="b1", slug="app", status="completed", artifact_dir=str(pdir))

    assert await store.delete_build("b1") is True
    assert not pdir.exists()


async def test_delete_build_spares_dir_shared_with_a_sibling_row(tmp_path):
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    pdir = _project_dir(tmp_path, "app", manifest={"build_id": "new", "slug": "app"})
    await store.save_build(build_id="old", slug="app", status="failed", artifact_dir=str(pdir))
    await store.save_build(build_id="new", slug="app", status="completed", artifact_dir=str(pdir))

    assert await store.delete_build("old") is True
    assert pdir.exists()  # sibling row still references the dir

    assert await store.delete_build("new") is True
    assert not pdir.exists()  # last referencing row cleans up


async def test_delete_build_spares_dir_reclaimed_by_a_newer_build(tmp_path):
    # delete_project trashed the folder but left the row; a rebuild re-claimed
    # the path and wrote its own manifest. The stale row must not rmtree it.
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    pdir = _project_dir(tmp_path, "app", manifest={"build_id": "rebuilt", "slug": "app"})
    await store.save_build(build_id="stale", slug="app", status="completed", artifact_dir=str(pdir))

    assert await store.delete_build("stale") is True
    assert pdir.exists()
    assert (pdir / "app.py").exists()


async def test_delete_build_spares_dir_whose_legacy_manifest_names_another_slug(tmp_path):
    # Legacy manifests may lack build_id; a mismatched slug still marks the dir
    # as another project's.
    store = MemoryStore(Settings(data_dir=tmp_path / "d", logs_dir=tmp_path / "l"))
    await store.init_db()
    pdir = _project_dir(tmp_path, "app", manifest={"slug": "other-app"})
    await store.save_build(build_id="b1", slug="app", status="completed", artifact_dir=str(pdir))

    assert await store.delete_build("b1") is True
    assert pdir.exists()
