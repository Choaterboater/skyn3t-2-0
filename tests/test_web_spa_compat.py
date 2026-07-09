"""Guard the SPA <-> API contract: every endpoint the dashboard calls exists.

The React SPA and FastAPI routes were built by separate agents; this pins the
endpoint names the frontend fetches so they can't silently drift again.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from skyn3t.config.settings import Settings  # noqa: E402
from skyn3t.intelligence.skill_library import SkillLibrary  # noqa: E402
from skyn3t.web import app as web_app  # noqa: E402
from skyn3t.web.deps import AppState, BuildRecord  # noqa: E402

SPA_GETS = [
    "/api/health", "/api/brain", "/api/settings", "/api/builds", "/api/agents",
    "/api/skills", "/api/agent-catalog", "/api/cortex/proposals", "/api/llm/secrets", "/api/status",
]


@pytest.fixture()
def client():
    app = web_app.create_app(state=AppState(settings=Settings(llm_backend="stub")))
    return TestClient(app)


@pytest.mark.parametrize("path", SPA_GETS)
def test_spa_get_endpoint_exists(client, path):
    assert client.get(path).status_code == 200


def test_spa_post_build_alias(client):
    assert client.post("/api/builds", json={"brief": "a todo app"}).status_code == 200


def test_spa_post_studio_approve_endpoint_is_mounted():
    state = AppState(settings=Settings(llm_backend="stub"))
    state.builds["build-123"] = BuildRecord(
        build_id="build-123", brief="gate me", status="running"
    )
    app = web_app.create_app(state=state)
    local_client = TestClient(app)

    response = local_client.post(
        "/api/studio/approve",
        json={"build_id": "build-123", "approved": True, "reason": "ok"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["decision"] == "approved"


def test_spa_get_model_routing_preview_is_available(client):
    response = client.get("/api/models/routing-preview?build_profile=balanced")
    assert response.status_code == 200
    body = response.json()
    assert "tiers" in body
    assert body["build_profile"] == "balanced"


def test_spa_model_catalog_endpoint_is_available(client):
    response = client.get("/api/models/catalog?q=gpt&sort=provider&order=asc&limit=5")
    assert response.status_code == 200
    body = response.json()
    assert "items" in body
    assert "count" in body
    assert "returned" in body


def test_spa_cleanup_endpoints_are_mounted():
    state = AppState(settings=Settings(llm_backend="stub"))
    state.builds["cleanup-terminal"] = BuildRecord(
        build_id="cleanup-terminal",
        brief="cleanup done",
        status="completed",
    )
    state.builds["cleanup-active"] = BuildRecord(
        build_id="cleanup-active",
        brief="cleanup busy",
        status="running",
    )
    app = web_app.create_app(state=state)
    local_client = TestClient(app)

    response = local_client.post("/api/builds/cleanup", json={"all_terminal": True})
    assert response.status_code == 200

    state.builds["cleanup-terminal-alias"] = BuildRecord(
        build_id="cleanup-terminal-alias",
        brief="alias path",
        status="completed",
    )

    response_alias = local_client.post(
        "/api/studio/builds/cleanup",
        json={"build_id": "cleanup-terminal-alias"},
    )
    assert response_alias.status_code == 200
    body = response_alias.json()
    assert body == {"build_id": "cleanup-terminal-alias", "deleted": True}


def test_spa_fallback_reloads_index_after_frontend_rebuild(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    index = dist / "index.html"
    index.write_text(
        '<!doctype html><script type="module" src="/assets/old.js"></script>',
        encoding="utf-8",
    )
    (assets / "old.js").write_text("console.log('old')", encoding="utf-8")
    monkeypatch.setattr(web_app, "UI_DIST_DIR", dist)

    app = web_app.create_app(state=AppState(settings=Settings(llm_backend="stub")))
    local_client = TestClient(app)
    assert "/assets/old.js" in local_client.get("/agents").text

    (assets / "old.js").unlink()
    (assets / "new.js").write_text("console.log('new')", encoding="utf-8")
    index.write_text(
        '<!doctype html><script type="module" src="/assets/new.js"></script>',
        encoding="utf-8",
    )

    assert "/assets/new.js" in local_client.get("/agents").text


def test_spa_fallback_rejects_encoded_traversal(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    outside = tmp_path / "package.json"
    outside.write_text('{"private": true}', encoding="utf-8")
    monkeypatch.setattr(web_app, "UI_DIST_DIR", dist)

    app = web_app.create_app(state=AppState(settings=Settings(llm_backend="stub")))
    local_client = TestClient(app)

    response = local_client.get("/%2e%2e/package.json")

    assert response.status_code == 404
    assert "private" not in response.text


def test_spa_cortex_decide_alias(client):
    r = client.post("/api/cortex/proposals/none/decide", json={"decision": "reject"})
    assert r.status_code == 200


def test_health_has_fields_overview_reads(client):
    d = client.get("/api/health").json()
    assert "backend" in d and "active_builds" in d and "agents" in d


def test_agent_catalog_import_endpoint(tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "ui.md").write_text(
        "---\nname: UI Builder\ndescription: Builds accessible React UI.\n---\nbody\n",
        encoding="utf-8",
    )
    skills = SkillLibrary(tmp_path / "skills")
    app = web_app.create_app(state=AppState(settings=Settings(llm_backend="stub"), skills=skills))
    local_client = TestClient(app)

    preview = local_client.get("/api/agent-catalog", params={"path": str(catalog)})
    assert preview.status_code == 200
    assert preview.json()["summary"]["entries"] == 1

    imported = local_client.post("/api/agent-catalog/import", json={"path": str(catalog)})
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1
    assert local_client.get("/api/skills").json()["skills"][0]["title"] == "UI Builder"
