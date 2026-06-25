# tests/test_iter7_fixes.py
"""Iteration-7 fixes: the /projects endpoints must map a filesystem OSError to a
controlled HTTP error (not leak a 500 with internals); RepoScout._stop is
explicitly initialized."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from skyn3t.config.settings import Settings  # noqa: E402
from skyn3t.web import routes as routes_mod  # noqa: E402
from skyn3t.web.app import create_app  # noqa: E402
from skyn3t.web.deps import AppState  # noqa: E402


def _client(tmp_path):
    state = AppState(settings=Settings(projects_dir=tmp_path, auth_token=""))
    return TestClient(app=create_app(state=state), raise_server_exceptions=False)


def test_list_projects_oserror_maps_to_controlled_error(tmp_path, monkeypatch):
    async def boom(state):
        raise PermissionError("projects_dir unreadable")
    monkeypatch.setattr(routes_mod, "list_projects", boom)
    res = _client(tmp_path).get("/api/projects")
    assert res.status_code == 500
    assert "unable to read projects" in res.text
    assert "PermissionError" not in res.text  # internals not leaked


def test_delete_project_oserror_maps_to_controlled_error(tmp_path, monkeypatch):
    async def boom(state, slug):
        raise PermissionError("trash dir not writable")
    monkeypatch.setattr(routes_mod, "delete_project", boom)
    res = _client(tmp_path).delete("/api/projects/whatever")
    assert res.status_code == 500
    assert "failed to trash" in res.text
    assert "PermissionError" not in res.text


def test_delete_project_notfound_still_404(tmp_path, monkeypatch):
    async def nope(state, slug):
        raise FileNotFoundError()
    monkeypatch.setattr(routes_mod, "delete_project", nope)
    res = _client(tmp_path).delete("/api/projects/whatever")
    assert res.status_code == 404  # FileNotFoundError must NOT be swallowed by OSError


def test_repo_scout_stop_initialized():
    from skyn3t.core.events import EventBus
    from skyn3t.cortex.repo_scout import RepoScout

    scout = RepoScout(event_bus=EventBus())
    assert scout._stop is False  # explicit init, not lazy
