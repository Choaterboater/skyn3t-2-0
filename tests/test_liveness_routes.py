"""enumerate_routes must derive routes from the app's own pages, NEVER from build
output. Next.js prerender artifacts under .next/ were being globbed as routes that
then 404'd — tanking liveness_health and capping the build score."""
from __future__ import annotations

from skyn3t.studio.liveness import enumerate_routes


def test_ignores_next_build_artifacts(tmp_path):
    # Next build output — must NOT become routes.
    (tmp_path / ".next" / "server" / "app").mkdir(parents=True)
    (tmp_path / ".next" / "server" / "app" / "about.html").write_text("<html></html>")
    (tmp_path / ".next" / "server" / "pages").mkdir(parents=True)
    (tmp_path / ".next" / "server" / "pages" / "404.html").write_text("<html></html>")
    # a real delivered static page
    (tmp_path / "index.html").write_text("<html></html>")

    routes = [r.path for r in enumerate_routes(str(tmp_path), "nextjs")]
    assert not any(".next" in r for r in routes), routes
    assert "/" in routes


def test_ignores_other_build_dirs(tmp_path):
    for d in ("dist", "out", ".svelte-kit", "node_modules"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "x.html").write_text("<html></html>")
    routes = [r.path for r in enumerate_routes(str(tmp_path), "")]
    assert all(p not in r for r in routes for p in ("dist", "out", ".svelte-kit", "node_modules"))


def test_ancestor_ignore_dir_does_not_suppress_discovery(tmp_path):
    # The workspace itself lives under a dir named "build": only parts BELOW the
    # project root may match the ignore set, or every route vanishes and
    # liveness reports false-healthy (root-only probing).
    project = tmp_path / "build" / "myapp"
    project.mkdir(parents=True)
    (project / "main.py").write_text(
        '@app.get("/")\ndef home(): ...\n@app.get("/about")\ndef about(): ...\n')
    (project / "about.html").write_text("<html></html>")

    routes = {r.path for r in enumerate_routes(project, "fastapi")}
    assert {"/", "/about", "/about.html"} <= routes


def test_ignores_routes_defined_in_test_fixtures(tmp_path):
    # Routes minted by test files/fixtures are never served by the delivered
    # app; probing them 404s and counts as dead, triggering bogus repairs.
    (tmp_path / "main.py").write_text('@app.get("/")\ndef home(): ...\n')
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (tmp_path / "tests" / "conftest.py").write_text(
        '@app.get("/pytest-fixture-route")\ndef r(): ...\n')
    (tmp_path / "tests" / "test_server.js").write_text("app.get('/only-in-tests', h);\n")
    (fixtures / "sample.html").write_text("<html></html>")

    routes = {r.path for r in enumerate_routes(tmp_path, "fastapi")}
    assert routes == {"/"}
