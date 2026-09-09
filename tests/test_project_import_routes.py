from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.studio.manifest import BuildManifest
from skyn3t.web import routes
from skyn3t.web.app import create_app
from skyn3t.web.deps import AppState


def _source(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "main.py").write_text("print('existing application')\n")
    return source


def _state(tmp_path):
    return SimpleNamespace(
        settings=SimpleNamespace(projects_dir=tmp_path / "Projects"),
        event_bus=EventBus(), builds={}, orchestrator=None,
    )


def test_import_list_and_improve_eligibility_without_delivery(tmp_path):
    source = _source(tmp_path)
    state = _state(tmp_path)
    result = asyncio.run(routes.import_existing_project(state, str(source)))
    row = asyncio.run(routes.list_projects(state))["projects"][0]
    assert result["slug"] == row["slug"] == "legacy"
    assert row["status"] == row["delivery_state"] == "imported"
    assert row["score"] is None and row["verdict"] == ""
    assert row["is_complete"] is False and row["can_improve"] is True
    assert not row["has_preview"] and not row["has_serve"]
    assert row["source"]["original_path"] == str(source)
    assert "snapshot" not in row["source"]
    assert row["deployments"] == [] and row["live_url"] == ""
    assert not row["can_reverify"]
    # Reaches the missing-orchestrator check rather than the delivery guard.
    assert asyncio.run(routes.improve_project(state, "legacy", "fix it"))["accepted"] is False
    with pytest.raises(routes.ProjectNotDeliveredError):
        routes._require_delivered_project(state, "legacy")


def test_import_api_auth_validation_and_conflict(tmp_path):
    source = _source(tmp_path)
    state = AppState(settings=Settings(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
        auth_token="test-import-token",
    ))
    client = TestClient(create_app(state=state))
    assert client.post("/api/projects/import", json={"path": str(source)}).status_code == 401
    client.headers["Authorization"] = "Bearer test-import-token"
    for body in ({}, {"path": None}, {"path": str(source), "stack": []}, {"path": "."}):
        assert client.post("/api/projects/import", json=body).status_code == 422
    assert client.post("/api/projects/import", json={"path": str(tmp_path / "missing")}).status_code == 404
    response = client.post("/api/projects/import", json={"path": str(source)})
    assert response.status_code == 200
    assert response.json()["status"] == "imported"
    assert client.post("/api/projects/import", json={"path": str(source)}).status_code == 409
    assert client.post("/api/studio/serve", json={"slug": "legacy"}).status_code == 409


def test_imported_improve_dispatches_existing_engine(tmp_path, monkeypatch):
    source = _source(tmp_path)
    state = _state(tmp_path)
    state.orchestrator = object()
    submitted = []
    async def fake_improve(self, project, goal, **kwargs):
        submitted.append((project, goal))
    monkeypatch.setattr("skyn3t.studio.improve.ImproveEngine.improve", fake_improve)
    monkeypatch.setattr(routes, "_enforce_build_routing", lambda state: None)
    monkeypatch.setattr(routes, "_submission_routing_snapshot", lambda *args: {})

    async def run():
        await routes.import_existing_project(state, str(source))
        result = await routes.improve_project(state, "legacy", "  refactor the parser  ")
        assert result["accepted"] is True
        await asyncio.gather(*list(routes._IMPROVE_TASKS))
    asyncio.run(run())
    assert submitted == [("legacy", "refactor the parser")]


def test_unowned_incomplete_project_is_not_improvable(tmp_path):
    state = _state(tmp_path)
    project = Path(state.settings.projects_dir) / "incomplete"
    BuildManifest(slug="incomplete", brief="pending", status="pending").save(project)
    with pytest.raises(routes.ProjectNotDeliveredError):
        asyncio.run(routes.improve_project(state, "incomplete", "finish"))
    assert asyncio.run(routes.list_projects(state))["projects"][0]["can_improve"] is False
