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
from skyn3t.web.app import create_app  # noqa: E402
from skyn3t.web.deps import AppState  # noqa: E402

SPA_GETS = [
    "/api/health", "/api/brain", "/api/settings", "/api/builds", "/api/agents",
    "/api/skills", "/api/agent-catalog", "/api/cortex/proposals", "/api/llm/secrets", "/api/status",
]


@pytest.fixture()
def client():
    app = create_app(state=AppState(settings=Settings(llm_backend="stub")))
    return TestClient(app)


@pytest.mark.parametrize("path", SPA_GETS)
def test_spa_get_endpoint_exists(client, path):
    assert client.get(path).status_code == 200


def test_spa_post_build_alias(client):
    assert client.post("/api/builds", json={"brief": "a todo app"}).status_code == 200


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
    app = create_app(state=AppState(settings=Settings(llm_backend="stub"), skills=skills))
    local_client = TestClient(app)

    preview = local_client.get("/api/agent-catalog", params={"path": str(catalog)})
    assert preview.status_code == 200
    assert preview.json()["summary"]["entries"] == 1

    imported = local_client.post("/api/agent-catalog/import", json={"path": str(catalog)})
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1
    assert local_client.get("/api/skills").json()["skills"][0]["title"] == "UI Builder"
