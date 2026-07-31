from __future__ import annotations

import base64
import hashlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import skyn3t.web.app as web_app  # noqa: E402
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
    # Styles are self-only plus the hashed fallback-page <style>; the SPA sets
    # runtime style props via CSSOM, which style-src does not govern.
    assert "style-src 'self' 'sha256-" in csp
    assert "'unsafe-inline'" not in csp
    assert "frame-src 'self' http://127.0.0.1:* http://localhost:*" in csp


def test_minimal_page_inline_style_matches_csp_hash(tmp_path, monkeypatch):
    # Without a built dist the fallback page is served; its inline <style> is
    # the only style the CSP hash must authorize, byte-for-byte.
    monkeypatch.setattr(web_app, "UI_DIST_DIR", tmp_path / "no-dist")

    response = _client(tmp_path).get("/")

    assert response.status_code == 200
    body = response.text
    style = body[body.index("<style>") + len("<style>"): body.index("</style>")]
    digest = base64.b64encode(hashlib.sha256(style.encode("utf-8")).digest()).decode("ascii")
    csp = response.headers["content-security-policy"]
    assert f"'sha256-{digest}'" in csp
    assert "'unsafe-inline'" not in csp


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
