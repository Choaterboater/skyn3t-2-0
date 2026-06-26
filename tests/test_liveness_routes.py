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
