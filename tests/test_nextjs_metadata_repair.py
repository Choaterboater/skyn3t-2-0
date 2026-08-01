"""ensure_nextjs_metadata: a Next.js App Router project always ends up with a
metadata export from SOME server component (the artdeco golden case — a
'use client' layout with a manual <head> and zero metadata — failed seo 3/3).
"""

from __future__ import annotations

import json

from skyn3t.studio.proof_run import apply_deterministic_repairs, ensure_nextjs_metadata


def _pkg(tmp_path, name="aurum-hotel", description="An art-deco boutique hotel."):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": name, "description": description, "private": True}),
        encoding="utf-8",
    )


def test_client_layout_is_skipped_metadata_lands_on_page(tmp_path):
    _pkg(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    (app / "layout.jsx").write_text(
        '"use client";\n\nexport default function RootLayout({ children }) {\n'
        "  return <html lang=\"en\"><body>{children}</body></html>;\n}\n",
        encoding="utf-8",
    )
    (app / "page.jsx").write_text(
        "export default function Home() {\n  return <main><h1>Aurum</h1></main>;\n}\n",
        encoding="utf-8",
    )

    changed = ensure_nextjs_metadata(tmp_path)

    assert changed == ["app/page.jsx"]
    layout_text = (app / "layout.jsx").read_text(encoding="utf-8")
    assert "export const metadata" not in layout_text  # invalid from client components
    page_text = (app / "page.jsx").read_text(encoding="utf-8")
    assert page_text.startswith('export const metadata = { title: "aurum-hotel",')
    assert '"use client"' not in page_text


def test_server_layout_gets_metadata_first(tmp_path):
    _pkg(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    (app / "layout.jsx").write_text(
        "import './globals.css';\n\n"
        "export default function RootLayout({ children }) {\n"
        "  return <html lang=\"en\"><body>{children}</body></html>;\n}\n",
        encoding="utf-8",
    )
    (app / "page.jsx").write_text(
        "export default function Home() {\n  return <main><h1>Aurum</h1></main>;\n}\n",
        encoding="utf-8",
    )

    assert ensure_nextjs_metadata(tmp_path) == ["app/layout.jsx"]
    assert (app / "layout.jsx").read_text(encoding="utf-8").startswith(
        'export const metadata = { title: "aurum-hotel",'
    )
    # idempotent
    assert ensure_nextjs_metadata(tmp_path) == []


def test_existing_metadata_is_respected_and_non_nextjs_skipped(tmp_path):
    _pkg(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    (app / "layout.jsx").write_text(
        "export const metadata = { title: 'Aurum' };\n\n"
        "export default function RootLayout({ children }) {\n"
        "  return <html lang=\"en\"><body>{children}</body></html>;\n}\n",
        encoding="utf-8",
    )
    assert ensure_nextjs_metadata(tmp_path) == []

    # no app dir at all -> nothing to do
    (tmp_path / "app").rename(tmp_path / "not_app")
    assert ensure_nextjs_metadata(tmp_path) == []


def test_apply_deterministic_repairs_wires_metadata(tmp_path):
    _pkg(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    (app / "page.jsx").write_text(
        "export default function Home() {\n  return <main><h1>Aurum</h1></main>;\n}\n",
        encoding="utf-8",
    )

    out = apply_deterministic_repairs(tmp_path, stack="nextjs")

    assert out["nextjs_metadata_added"] == ["app/page.jsx"]
