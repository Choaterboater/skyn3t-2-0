"""Dangling local stylesheet imports get stubbed so the build resolves.

Was a dedicated `_stub_dangling_stylesheets` mechanism (StudioRunner) working off
proof_run's unresolved-imports detector, kept separate from `scaffold_missing_imports`
because that function could only see NAMED imports (`import X from '...'`) and a
stylesheet is almost always imported as a bare side-effect statement
(`import './app.css';`, no `from` clause) — invisible to it.

Retired: `scaffold_missing_imports` now runs a SECOND detection pass using the same
permissive regex proof_run's own checker uses (catching bare imports generally, not
just stylesheets), and picks stylesheet-appropriate stub CONTENT (a CSS comment, not
JS) whenever the target extension is `.css`/`.scss`/`.sass`/`.less`. One mechanism,
not two, and it's exercised on every `_deterministic_repairs()` call already wired
throughout the build pipeline (see runner.py's fix-loop, the delivery-recovery pass,
and `_final_consistency_check`) — not a separate call site to keep in sync.
"""

from __future__ import annotations

from skyn3t.studio.proof_run import proof_run, scaffold_missing_imports


def test_dangling_stylesheet_resolves_the_import(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "package.json").write_text(
        '{"name":"x","scripts":{"build":"vite build"}}', encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<div id="root"></div><script type="module" src="/src/main.jsx"></script>',
        encoding="utf-8")
    # main.jsx imports a stylesheet that was never written (the v2 failure) as a
    # BARE side-effect import — the common, realistic way CSS gets imported, and
    # the exact case a NAMED-import-only detector can't see.
    (src / "main.jsx").write_text(
        "import './styles/app.css';\nexport default function App(){ return null }\n",
        encoding="utf-8")
    (src / "App.jsx").write_text(
        "export default function App(){ return null }\n", encoding="utf-8")

    proof = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert not proof.passed
    assert any("app.css" in g for g in proof.detail.get("unresolved_imports", []))

    written = scaffold_missing_imports(tmp_path, stack="react")
    assert "src/styles/app.css" in written
    assert (tmp_path / "src" / "styles" / "app.css").exists()
    content = (tmp_path / "src" / "styles" / "app.css").read_text()
    assert "react" not in content.lower()  # valid CSS stub, not a JS/React stub

    # Re-proof: the stylesheet import now resolves.
    proof2 = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert not any("app.css" in g for g in proof2.detail.get("unresolved_imports", []))


def test_dangling_js_import_is_stubbed_too_not_silently_ignored(tmp_path):
    # Unlike the old dedicated stylesheet-only mechanism, the general repair
    # path DOES also stub a missing .js — a boot-capable placeholder beats a
    # hard crash (the fix-loop's LLM repair, when available, is the path that
    # produces a REAL implementation; this is the deterministic backstop).
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.jsx").write_text(
        "import './missing-module.js';\nexport default function App(){return null}\n",
        encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert "src/missing-module.js" in written
    assert (tmp_path / "src" / "missing-module.js").exists()


def test_no_unresolved_imports_stubs_nothing(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.jsx").write_text(
        "export default function App(){ return null }\n", encoding="utf-8")
    assert scaffold_missing_imports(tmp_path) == []
