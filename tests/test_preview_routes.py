import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from skyn3t.config.settings import Settings  # noqa: E402
from skyn3t.web.app import create_app  # noqa: E402
from skyn3t.web.deps import AppState  # noqa: E402


def _client(tmp_path, token=""):
    state = AppState(settings=Settings(projects_dir=tmp_path, auth_token=token))
    proj = tmp_path / "demo" / ".preview"
    proj.mkdir(parents=True)
    (proj / "index.html").write_text("<h1>hi</h1>")
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

    image = client.get("/api/projects/demo/assets/golf.svg")
    assert image.status_code == 200
    assert "<svg" in image.text


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


def test_preview_slug_traversal_rejected(tmp_path):
    client = _client(tmp_path)
    # Encoded '..' as the slug must not escape projects_dir into a parent listing.
    res = client.get("/api/preview/%2e%2e")
    assert res.status_code in (400, 404)  # never a 200 directory listing
    res2 = client.get("/api/projects/%2e%2e/skyn3t/config/settings.py")
    assert res2.status_code in (400, 404)
    assert "projects_dir" not in res.text
