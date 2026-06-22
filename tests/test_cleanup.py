# tests/test_cleanup.py
from __future__ import annotations

import json
from pathlib import Path

from skyn3t.studio.cleanup import apply, scan, trash_path


def test_trash_path_moves_with_dedup(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "f.txt").write_text("x")
    trash = tmp_path / ".trash"
    dest = trash_path(src, trash)
    assert dest.exists() and not src.exists() and dest.parent == trash
    # a second item with the same name is de-duplicated, not clobbered
    src2 = tmp_path / "proj"
    src2.mkdir()
    (src2 / "g.txt").write_text("y")
    dest2 = trash_path(src2, trash)
    assert dest2 != dest and dest2.exists() and (dest2 / "g.txt").exists()


def _project(root: Path, slug: str, *, status="completed", created="2026-06-20T00:00:00+00:00"):
    d = root / slug
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "status": status, "created_at": created, "verdict": "go"}))
    (d / "main.py").write_text("print('x')\n")
    return d


def test_scan_buckets(tmp_path):
    projects = tmp_path / "Projects"
    worktrees = tmp_path / ".skyn3t_worktrees"
    projects.mkdir(); worktrees.mkdir()
    _project(projects, "good")
    _project(projects, "broken", status="failed")
    (projects / "no-manifest").mkdir()           # orphaned project
    (projects / "good" / ".preview").mkdir()      # stray preview
    (worktrees / "loose-abcd1234").mkdir()        # orphaned worktree

    report = scan(projects, worktrees, known_worktrees=())
    assert [i.path.name for i in report.failed] == ["broken"]
    assert [i.path.name for i in report.orphaned_projects] == ["no-manifest"]
    assert any(i.path.name == ".preview" for i in report.stray_previews)
    assert [i.path.name for i in report.orphaned_worktrees] == ["loose-abcd1234"]


def test_apply_dry_run_moves_nothing(tmp_path):
    projects = tmp_path / "Projects"; projects.mkdir()
    _project(projects, "broken", status="failed")
    report = scan(projects, tmp_path / "wt", known_worktrees=())
    res = apply(report, trash_dir=tmp_path / "trash", dry_run=True)
    assert res.dry_run and res.moved == [] and (projects / "broken").exists()
    assert res.freed_bytes > 0


def test_apply_moves_to_trash(tmp_path):
    projects = tmp_path / "Projects"; projects.mkdir()
    _project(projects, "broken", status="failed")
    report = scan(projects, tmp_path / "wt", known_worktrees=())
    res = apply(report, trash_dir=tmp_path / "trash", dry_run=False, categories=["failed"])
    assert not (projects / "broken").exists()
    assert res.moved and (tmp_path / "trash").exists()


def test_apply_collision_deduplication(tmp_path):
    projects = tmp_path / "Projects"; projects.mkdir()
    _project(projects, "broken", status="failed")
    trash = tmp_path / "trash"
    (trash / "broken").mkdir(parents=True)  # pre-existing collision
    report = scan(projects, tmp_path / "wt", known_worktrees=())
    res = apply(report, trash_dir=trash, dry_run=False, categories=["failed"])
    assert (trash / "broken").exists() and (trash / "broken.1").exists()
    assert res.moved


def test_active_slug_protects_project_and_worktree(tmp_path):
    projects = tmp_path / "Projects"; worktrees = tmp_path / "wt"
    projects.mkdir(); worktrees.mkdir()
    _project(projects, "live", status="failed")     # would normally be 'failed'
    (worktrees / "live-abcd1234").mkdir()            # the live build's worktree
    report = scan(projects, worktrees, active_slugs=("live",))
    assert all(i.path.name != "live" for i in report.failed)
    assert report.orphaned_worktrees == []


def test_known_worktree_excluded(tmp_path):
    projects = tmp_path / "Projects"; worktrees = tmp_path / "wt"
    projects.mkdir(); worktrees.mkdir()
    live = worktrees / "x-deadbeef"; live.mkdir()
    report = scan(projects, worktrees, known_worktrees=(str(live),))
    assert report.orphaned_worktrees == []


def test_failed_project_preview_not_in_stray_previews(tmp_path):
    projects = tmp_path / "Projects"; projects.mkdir()
    d = _project(projects, "broken", status="failed")
    (d / ".preview").mkdir()  # .preview inside a failed project
    report = scan(projects, tmp_path / "wt", known_worktrees=())
    # the failed dir is already a cleanup target; .preview must NOT appear separately
    assert not any(i.path.name == ".preview" for i in report.stray_previews)
    assert [i.path.name for i in report.failed] == ["broken"]
