from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from skyn3t.config.settings import Settings  # noqa: E402
from skyn3t.studio.manifest import BuildManifest  # noqa: E402
from skyn3t.web.app import create_app  # noqa: E402
from skyn3t.web.deps import AppState  # noqa: E402


def _client(tmp_path) -> TestClient:
    state = AppState(settings=Settings(projects_dir=tmp_path, auth_token=""))
    return TestClient(create_app(state=state))


@pytest.mark.parametrize("path", ["/", "/studio", "/api/status"])
def test_control_plane_responses_have_browser_security_headers(tmp_path, path):
    response = _client(tmp_path).get(path)

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["x-permitted-cross-domain-policies"] == "none"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "frame-src 'self' http://127.0.0.1:* http://localhost:*" in csp


def test_generated_project_keeps_its_stricter_sandbox_csp(tmp_path):
    preview = tmp_path / "demo" / ".preview"
    preview.mkdir(parents=True)
    (preview / "index.html").write_text("<h1>preview</h1>", encoding="utf-8")
    BuildManifest(
        slug="demo", brief="demo", stack="static", status="completed", verdict="go"
    ).save(tmp_path / "demo")
    client = _client(tmp_path)

    response = client.get("/api/projects/demo/index.html")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert csp.startswith("sandbox ")
    assert "allow-same-origin" not in csp
    assert "default-src" not in csp
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
