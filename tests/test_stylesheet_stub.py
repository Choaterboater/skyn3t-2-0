"""Fix-loop stubs a dangling local stylesheet a slice left unwritten.

A sliced build can ship `main.jsx` importing `./styles/app.css` that no slice
actually wrote (the code-improver only rewrites EXISTING files, so it can't
create the missing target). _stub_dangling_stylesheets writes an empty stub so
the import resolves — the same safety net the monolithic path already has.
Only LOCAL stylesheets are stubbed; a dangling .js is left alone (it's a real
missing module, not something to silently empty-stub). Offline (inline).
"""

from __future__ import annotations

import types

from skyn3t.studio.proof_run import proof_run
from skyn3t.studio.runner import StudioRunner


def test_stub_dangling_stylesheet_resolves_the_import(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "package.json").write_text(
        '{"name":"x","scripts":{"build":"vite build"}}', encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<div id="root"></div><script type="module" src="/src/main.jsx"></script>',
        encoding="utf-8")
    # main.jsx imports a stylesheet that was never written (the v2 failure).
    (src / "main.jsx").write_text(
        "import './styles/app.css';\nexport default function App(){ return null }\n",
        encoding="utf-8")
    (src / "App.jsx").write_text(
        "export default function App(){ return null }\n", encoding="utf-8")

    proof = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert not proof.passed
    assert any("app.css" in g for g in proof.detail.get("unresolved_imports", []))

    n = StudioRunner._stub_dangling_stylesheets(str(tmp_path), proof)
    assert n == 1
    assert (tmp_path / "src" / "styles" / "app.css").exists()

    # Re-proof: the stylesheet import now resolves.
    proof2 = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert not any("app.css" in g for g in proof2.detail.get("unresolved_imports", []))


def test_dangling_js_import_is_not_stubbed(tmp_path):
    # A missing .js is a real broken module — must NOT be silently empty-stubbed.
    proof = types.SimpleNamespace(
        detail={"unresolved_imports": ["src/main.jsx -> ./missing-module.js"]})
    assert StudioRunner._stub_dangling_stylesheets(str(tmp_path), proof) == 0
    assert not (tmp_path / "src" / "missing-module.js").exists()


def test_no_unresolved_imports_stubs_nothing(tmp_path):
    proof = types.SimpleNamespace(detail={})
    assert StudioRunner._stub_dangling_stylesheets(str(tmp_path), proof) == 0
