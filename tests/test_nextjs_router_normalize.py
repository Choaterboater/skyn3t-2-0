"""Next.js router normalization (swarm finding #7).

Generated Next.js apps shipped BOTH App Router (app/page) and Pages Router
(pages/index) files resolving to the same '/' route, which fails `next build`
with 'Conflicting app and page file'. The normalizer resolves only TRUE route
collisions (same pathname in both routers) and keeps the LARGER file so real
content is never dropped for a scaffold stub. Non-colliding routes are untouched.
"""
from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent


def _agent():
    return CodeAgent.__new__(CodeAgent)


def test_root_route_collision_keeps_larger_pages():
    files = {
        "app/page.jsx": "x",              # tiny scaffold stub
        "app/layout.jsx": "layout",
        "pages/index.tsx": "y" * 500,     # the REAL home page
        "pages/about.tsx": "about" * 50,  # /about — no collision, must survive
    }
    out = _agent()._normalize_nextjs_router(dict(files))
    assert "pages/index.tsx" in out and "app/page.jsx" not in out  # larger wins
    assert "pages/about.tsx" in out                                # non-colliding kept


def test_root_route_collision_keeps_larger_app():
    files = {"app/page.jsx": "real" * 200, "pages/index.tsx": "stub"}
    out = _agent()._normalize_nextjs_router(dict(files))
    assert "app/page.jsx" in out and "pages/index.tsx" not in out


def test_no_collision_left_untouched():
    files = {"app/page.jsx": "home", "app/about/page.jsx": "about", "pages/api/x.js": "api"}
    out = _agent()._normalize_nextjs_router(dict(files))
    assert set(out) == set(files)


def test_repair_entrypoints_wires_normalizer_for_nextjs():
    files = {"app/page.jsx": "x", "pages/index.tsx": "y" * 300}
    out = _agent()._repair_entrypoints("nextjs", dict(files))
    assert ("app/page.jsx" in out) ^ ("pages/index.tsx" in out)  # no '/' collision left
