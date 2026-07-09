import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from skyn3t.config.settings import Settings  # noqa: E402
from skyn3t.studio.manifest import BuildManifest  # noqa: E402
from skyn3t.web.app import create_app  # noqa: E402
from skyn3t.web.deps import AppState  # noqa: E402


def _client(tmp_path, token=""):
    state = AppState(settings=Settings(projects_dir=tmp_path, auth_token=token))
    proj = tmp_path / "demo" / ".preview"
    proj.mkdir(parents=True)
    (proj / "index.html").write_text("<h1>hi</h1>")
    BuildManifest(
        slug="demo", brief="demo", stack="static", status="completed", verdict="go"
    ).save(tmp_path / "demo")
    app = create_app(state=state)
    return TestClient(app)


def test_preview_lists_files(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/preview/demo")
    assert res.status_code == 200
    assert "index.html" in res.json()["files"]


def test_project_file_serves_content(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/projects/demo/index.html")
    assert res.status_code == 200
    assert "<h1>hi</h1>" in res.text
    csp = res.headers["content-security-policy"]
    assert "sandbox" in csp
    assert "allow-scripts" in csp
    assert "allow-same-origin" not in csp
    assert "allow-top-navigation" not in csp
    assert "allow-popups" not in csp
    assert "allow-forms" not in csp
    assert res.headers["cross-origin-opener-policy"] == "same-origin"
    assert res.headers["x-content-type-options"] == "nosniff"


def test_project_file_scopes_absolute_asset_urls(tmp_path):
    state = AppState(settings=Settings(projects_dir=tmp_path))
    proj = tmp_path / "demo" / ".preview"
    assets = proj / "assets"
    assets.mkdir(parents=True)
    (proj / "index.html").write_text(
        '<img src="/assets/golf.svg">'
        '<script src="/assets/app.js"></script>'
        "<style>.hero{background:url('/assets/bg.svg')}</style>"
    )
    (assets / "golf.svg").write_text("<svg />")
    (assets / "app.js").write_text('const img = "/assets/golf.svg";')
    (assets / "atlas.xml").write_text("<TextureAtlas />")
    (assets / "model.glb").write_bytes(b"glTF")
    BuildManifest(
        slug="demo", brief="demo", stack="static", status="completed", verdict="go"
    ).save(tmp_path / "demo")
    app = create_app(state=state)
    client = TestClient(app)

    html = client.get("/api/projects/demo/index.html")
    assert html.status_code == 200
    assert 'src="/api/projects/demo/assets/golf.svg"' in html.text
    assert 'src="/api/projects/demo/assets/app.js"' in html.text
    assert "url('/api/projects/demo/assets/bg.svg')" in html.text

    js = client.get("/api/projects/demo/assets/app.js")
    assert js.status_code == 200
    assert '"/api/projects/demo/assets/golf.svg"' in js.text
    assert js.headers["access-control-allow-origin"] == "null"
    assert js.headers["cross-origin-resource-policy"] == "cross-origin"

    image = client.get("/api/projects/demo/assets/golf.svg")
    assert image.status_code == 200
    assert "<svg" in image.text
    # SVG is active content when opened as a document, so it must carry the
    # same server-enforced sandbox as HTML.
    assert "sandbox" in image.headers["content-security-policy"]
    assert "allow-same-origin" not in image.headers["content-security-policy"]

    atlas = client.get("/api/projects/demo/assets/atlas.xml")
    assert atlas.status_code == 200
    assert "<TextureAtlas" in atlas.text
    assert "sandbox" in atlas.headers["content-security-policy"]

    model = client.get("/api/projects/demo/assets/model.glb")
    assert model.status_code == 200
    assert model.content == b"glTF"


def test_sandboxed_preview_origin_cannot_call_control_api(tmp_path):
    client = _client(tmp_path)
    opaque = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}

    denied = client.get("/api/status", headers=opaque)
    assert denied.status_code == 403

    # Project subresources remain usable by the opaque-origin document.
    project = client.get("/api/projects/demo/index.html", headers=opaque)
    assert project.status_code == 200
    assert project.headers["access-control-allow-origin"] == "null"


def test_project_route_never_serves_private_or_source_files(tmp_path):
    client = _client(tmp_path)
    project = tmp_path / "demo" / ".preview"
    (project / ".env").write_text("OPENAI_API_KEY=do-not-serve")
    (project / "server.py").write_text("SECRET = 'do-not-serve'")
    (project / "package.json").write_text('{"private": true}')
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[remote]\nurl=private")

    for path in (".env", "server.py", "package.json", ".git/config"):
        response = client.get(
            f"/api/projects/demo/{path}",
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 404
        assert "do-not-serve" not in response.text
        assert "private" not in response.text

    # Public preview HTML opts into only the opaque sandbox origin; a normal
    # hostile web origin does not receive CORS access.
    html = client.get(
        "/api/projects/demo/index.html",
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert html.status_code == 200
    assert html.headers["access-control-allow-origin"] == "null"


def test_dns_rebinding_host_cannot_inherit_loopback_control_access(tmp_path):
    client = _client(tmp_path)
    same_origin_rebind = {
        "Host": "attacker.example",
        "Origin": "http://attacker.example",
        "Sec-Fetch-Site": "same-origin",
    }
    assert client.get("/api/status", headers=same_origin_rebind).status_code == 403
    assert client.get("/api/status", headers={"Host": "attacker.example"}).status_code == 403


def test_cross_origin_control_request_requires_valid_bearer(tmp_path):
    client = _client(tmp_path, token="secret")
    opaque = {
        "Origin": "null",
        "Sec-Fetch-Site": "cross-site",
        "Authorization": "Bearer secret",
    }
    assert client.get("/api/status", headers=opaque).status_code == 200


def test_project_file_traversal_rejected(tmp_path):
    client = _client(tmp_path)
    # Encode the slashes so the client does NOT normalize the `..` away before
    # the request reaches the handler; the resolver must reject the escape and
    # never serve outside the preview root.
    res = client.get("/api/projects/demo/..%2f..%2f..%2f..%2fetc%2fpasswd")
    assert res.status_code in (400, 404)
    assert "root:" not in res.text


def test_preview_requires_auth_when_token_set(tmp_path):
    client = _client(tmp_path, token="secret")
    assert client.get(
        "/api/preview/demo", headers={"Authorization": "Bearer secret"}
    ).status_code == 200
    assert client.get(
        "/api/preview/demo", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401


def test_token_authenticated_preview_mints_read_only_capability(tmp_path):
    client = _client(tmp_path, token="secret")
    project = tmp_path / "demo" / ".preview"
    assets = project / "assets"
    assets.mkdir()
    (project / "index.html").write_text('<script src="/assets/app.js"></script>')
    (assets / "app.js").write_text('const logo = "/assets/logo.svg";')
    (assets / "logo.svg").write_text("<svg />")
    (project / ".env").write_text("DO_NOT_SERVE=secret")

    # The control-plane route remains bearer-only.
    assert client.get("/api/projects/demo/index.html").status_code == 401
    minted = client.get(
        "/api/preview/demo", headers={"Authorization": "Bearer secret"}
    )
    assert minted.status_code == 200
    preview_url = minted.json()["preview_url"]
    assert "/api/project-previews/" in preview_url
    assert "secret" not in preview_url

    # Browser navigation cannot attach Authorization, so the scoped capability
    # authenticates the iframe and all relative/rewritten subresources.
    html = client.get(preview_url)
    assert html.status_code == 200
    prefix = preview_url.removesuffix("index.html")
    assert f'src="{prefix}assets/app.js"' in html.text
    script = client.get(f"{prefix}assets/app.js", headers={"Origin": "null"})
    assert script.status_code == 200
    assert f'"{prefix}assets/logo.svg"' in script.text
    assert client.get(f"{prefix}.env").status_code == 404

    forged = preview_url.split("/")
    forged[3] = "A" * len(forged[3])
    assert client.get("/".join(forged)).status_code == 404

    other = tmp_path / "other" / ".preview"
    other.mkdir(parents=True)
    (other / "index.html").write_text("<h1>other</h1>")
    wrong_slug = preview_url.replace("/demo/", "/other/")
    assert client.get(wrong_slug).status_code == 404


def test_manifestless_build_cannot_mint_or_serve_preview_capability(tmp_path):
    state = AppState(settings=Settings(projects_dir=tmp_path))
    preview = tmp_path / "building" / ".preview"
    preview.mkdir(parents=True)
    (preview / "index.html").write_text("<h1>partial</h1>")
    app = create_app(state=state)
    client = TestClient(app)

    listing = client.get("/api/projects")
    assert listing.status_code == 200
    row = listing.json()["projects"][0]
    assert row["status"] == "incomplete"
    assert row["delivery_state"] == "incomplete"
    assert row["is_complete"] is False
    assert row["size_bytes"] == 0
    assert row["preview_url"] == ""
    assert row["has_serve"] is False

    assert client.get("/api/preview/building").status_code == 409
    assert client.get("/api/projects/building/index.html").status_code == 404
    serve = client.post("/api/studio/serve", json={"slug": "building"})
    assert serve.status_code == 409
    assert serve.json()["detail"] == "project build is not complete"

    # Even a correctly signed capability cannot bypass the delivery boundary.
    from skyn3t.web.routes import _project_preview_url

    capability = _project_preview_url(state, "building")
    assert client.get(capability).status_code == 404


def test_preview_slug_traversal_rejected(tmp_path):
    client = _client(tmp_path)
    # Encoded '..' as the slug must not escape projects_dir into a parent listing.
    res = client.get("/api/preview/%2e%2e")
    assert res.status_code in (400, 404)  # never a 200 directory listing
    res2 = client.get("/api/projects/%2e%2e/skyn3t/config/settings.py")
    assert res2.status_code in (400, 404)
    assert "projects_dir" not in res.text
