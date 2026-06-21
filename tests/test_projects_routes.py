# tests/test_projects_routes.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.web.routes import delete_project, list_projects


def _state(tmp_path, builds=None):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        settings=SimpleNamespace(projects_dir=projects),
        builds=builds or {},
    )


def _project(root, slug, *, status="completed", score=92.0):
    d = root / slug
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "stack": "python", "status": status, "verdict": "go",
         "score": score, "created_at": "2026-06-21T00:00:00+00:00"}))
    (d / "main.py").write_text("print('x')\n")
    return d


def test_list_projects_reads_manifests(tmp_path):
    state = _state(tmp_path)
    _project(state.settings.projects_dir, "alpha")
    _project(state.settings.projects_dir, "beta", status="failed", score=10.0)
    (state.settings.projects_dir / "no-manifest").mkdir()  # orphan dir still listed
    out = asyncio.run(list_projects(state))
    rows = {p["slug"]: p for p in out["projects"]}
    assert rows["alpha"]["status"] == "completed" and rows["alpha"]["score"] == 92.0
    assert rows["alpha"]["size_bytes"] > 0 and rows["alpha"]["has_manifest"] is True
    assert rows["no-manifest"]["has_manifest"] is False
    assert "beta" in rows and rows["beta"]["status"] == "failed"


def test_delete_project_moves_to_trash(tmp_path):
    state = _state(tmp_path)
    proj = _project(state.settings.projects_dir, "gamma")
    out = asyncio.run(delete_project(state, "gamma"))
    assert not proj.exists()
    trash = state.settings.projects_dir.parent / ".skyn3t_trash"
    assert Path(out["trashed_to"]).exists() and trash in Path(out["trashed_to"]).parents


def test_delete_project_rejects_traversal(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "../secrets"))


def test_delete_project_missing_is_filenotfound(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(FileNotFoundError):
        asyncio.run(delete_project(state, "nope"))


def test_delete_project_refuses_running_build(tmp_path):
    state = _state(tmp_path, builds={"b1": SimpleNamespace(slug="live", status="running")})
    _project(state.settings.projects_dir, "live")
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "live"))
