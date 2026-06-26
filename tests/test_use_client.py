"""add_use_client_directives — Next.js App Router components that use client-only
features (event handlers, state/effect hooks, browser globals) need a
`"use client"` directive, or `next build` fails static generation ("Event
handlers cannot be passed to Client Component props"). The repair prepends it."""
from __future__ import annotations

from skyn3t.studio.proof_run import add_use_client_directives


def _next(tmp_path):
    (tmp_path / "next.config.js").write_text("module.exports = {}\n", encoding="utf-8")


def test_adds_use_client_for_event_handler(tmp_path):
    _next(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "import X from '@/components/X'\n"
        "export default function P(){return <button onClick={()=>{}}>Go</button>}\n",
        encoding="utf-8")
    changed = add_use_client_directives(tmp_path)
    assert any("app/page.jsx" in c for c in changed)
    assert (tmp_path / "app" / "page.jsx").read_text().startswith('"use client";')


def test_adds_use_client_for_hooks(tmp_path):
    _next(tmp_path)
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "Counter.jsx").write_text(
        "import { useState } from 'react'\n"
        "export default function C(){const [n,s]=useState(0);return <div>{n}</div>}\n",
        encoding="utf-8")
    assert any("Counter" in c for c in add_use_client_directives(tmp_path))


def test_adds_use_client_for_browser_global(tmp_path):
    _next(tmp_path)
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "Theme.jsx").write_text(
        "export default function T(){const v=localStorage.getItem('x');return <i>{v}</i>}\n",
        encoding="utf-8")
    assert any("Theme" in c for c in add_use_client_directives(tmp_path))


def test_adds_use_client_for_styled_jsx(tmp_path):
    # styled-jsx is client-only — a Server Component using <style jsx> fails next build.
    _next(tmp_path)
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "Footer.jsx").write_text(
        "export default function Footer(){\n"
        "  return <footer><style jsx>{`footer{color:red}`}</style>hi</footer>;\n}\n",
        encoding="utf-8")
    assert any("Footer" in c for c in add_use_client_directives(tmp_path))


def test_skips_pure_server_component(tmp_path):
    _next(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "export default function P(){return <main><h1>Static</h1></main>}\n", encoding="utf-8")
    assert add_use_client_directives(tmp_path) == []


def test_idempotent_when_directive_present(tmp_path):
    _next(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "'use client'\nimport {useState} from 'react'\n"
        "export default function P(){const[n]=useState(0);return <i>{n}</i>}\n", encoding="utf-8")
    assert add_use_client_directives(tmp_path) == []


def test_interactive_metadata_page_strips_metadata(tmp_path):
    # A page that exports metadata AND has an event handler can't be a client
    # component with the metadata export — strip metadata, add the directive.
    _next(tmp_path)
    (tmp_path / "app" / "services").mkdir(parents=True)
    (tmp_path / "app" / "services" / "page.jsx").write_text(
        "import { Metadata } from 'next'\n"
        "export const metadata = {\n  title: 'Services',\n"
        "  openGraph: { title: 'Services' },\n};\n"
        "export default function P(){\n"
        "  return <button onClick={() => window.location.href='/contact'}>Go</button>\n}\n",
        encoding="utf-8")
    changed = add_use_client_directives(tmp_path)
    assert any("services/page.jsx" in c for c in changed)
    out = (tmp_path / "app" / "services" / "page.jsx").read_text()
    assert out.startswith('"use client";')
    assert "export const metadata" not in out          # server-only export removed
    assert "onClick" in out                              # interactivity preserved


def test_noop_outside_next_app(tmp_path):
    # Vite/CRA (no next.config, no app/) -> never inject the directive.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text(
        "export default function A(){return <button onClick={()=>{}}/>}\n", encoding="utf-8")
    assert add_use_client_directives(tmp_path) == []
