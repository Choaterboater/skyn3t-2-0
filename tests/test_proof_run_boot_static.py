"""Offline static boot-readiness gate in proof_run.

A delivered web app whose JS imports point at files that were never created
(e.g. `main.jsx` doing `import './styles/global.css'` with no such file) will
500 the moment a bundler touches it — it has NOT been proven. The real
`_run_node_build` gate soft-skips offline (no npm), so without a deterministic
static check these non-booting builds passed the proof and could be approved.

All tests are offline (no npm/docker/network); execution_backend="inline".
"""

from __future__ import annotations

from skyn3t.studio.proof_run import proof_run


def _scaffold_react(root, *, main_jsx: str):
    (root / "package.json").write_text(
        '{"name":"x","scripts":{"build":"vite build"}}', encoding="utf-8"
    )
    (root / "index.html").write_text(
        '<!doctype html><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script>',
        encoding="utf-8",
    )
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / "main.jsx").write_text(main_jsx, encoding="utf-8")
    (src / "App.jsx").write_text(
        "export default function App(){ return null }\n", encoding="utf-8"
    )


def test_proof_fails_on_unresolved_relative_import(tmp_path):
    # main.jsx imports a stylesheet that was never created -> Vite 500.
    _scaffold_react(
        tmp_path,
        main_jsx="import App from './App.jsx'\nimport './styles/global.css'\n",
    )
    res = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert res.passed is False
    assert "global.css" in str(res.missing) + str(res.detail)


def test_proof_passes_when_relative_imports_resolve(tmp_path):
    # Same app, but the imported stylesheet exists -> no boot blocker.
    _scaffold_react(
        tmp_path,
        main_jsx="import App from './App.jsx'\nimport './styles/global.css'\n",
    )
    styles = tmp_path / "src" / "styles"
    styles.mkdir(parents=True)
    (styles / "global.css").write_text("body{margin:0}", encoding="utf-8")
    res = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert res.passed is True


def test_extensionless_and_index_imports_resolve(tmp_path):
    # `./App` (no ext) and `./util` (dir w/ index) must NOT be flagged.
    _scaffold_react(tmp_path, main_jsx="import App from './App'\nimport './util'\n")
    util = tmp_path / "src" / "util"
    util.mkdir(parents=True)
    (util / "index.js").write_text("export const x = 1\n", encoding="utf-8")
    res = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert res.passed is True


def test_bare_and_aliased_imports_are_not_flagged(tmp_path):
    # Package imports (react) and alias imports (@/foo) are not relative paths
    # and must never be treated as unresolved local files.
    _scaffold_react(
        tmp_path,
        main_jsx="import React from 'react'\nimport x from '@/foo'\nimport App from './App.jsx'\n",
    )
    res = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert res.passed is True


def test_proof_ignores_generated_astro_metadata(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name":"golf","scripts":{"build":"astro build"},'
        '"dependencies":{"astro":"^5.0.0"}}',
        encoding="utf-8",
    )
    pages = tmp_path / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "index.astro").write_text(
        "<html><body><main>Golf lessons</main></body></html>", encoding="utf-8"
    )

    # Astro sync/build writes this generated declaration even when the project
    # has no authored content config. It is tool output, not a broken app import.
    metadata = tmp_path / ".astro"
    metadata.mkdir()
    (metadata / "content.d.ts").write_text(
        'export type ContentConfig = typeof import("./../src/content.config.mjs");\n',
        encoding="utf-8",
    )

    res = proof_run(tmp_path, stack="astro", execution_backend="inline")
    assert res.passed is True
    assert "unresolved_imports" not in res.detail
    assert res.files_total == 2


# --- gate 2: unwired scaffold-stub entry -------------------------------------

_STUB_APP = (
    "export default function App(){ return <main>"
    "A runnable Vite + React starter generated offline by SkyN3t</main> }\n"
)


def test_proof_fails_when_entry_is_unwired_scaffold_stub(tmp_path):
    # App.jsx is still the scaffold stub (every import resolves), yet real
    # components were generated and left unwired -> the app renders the stub,
    # not the app. Gate 1 can't catch this (imports are fine).
    _scaffold_react(tmp_path, main_jsx="import App from './App.jsx'\n")
    (tmp_path / "src" / "App.jsx").write_text(_STUB_APP, encoding="utf-8")
    comp = tmp_path / "src" / "components"
    comp.mkdir(parents=True)
    (comp / "Gallery.jsx").write_text(
        "export default function Gallery(){ return <ul/> }\n", encoding="utf-8"
    )
    (comp / "Workspace.jsx").write_text(
        "export default function Workspace(){ return <div/> }\n", encoding="utf-8"
    )
    res = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert res.passed is False
    assert "unwired" in str(res.detail).lower() or "component" in str(res.detail).lower()


def test_marker_in_wired_entry_passes(tmp_path):
    # Regression for a real false positive (dbg-mobile): the entry retains the
    # scaffold subtitle marker BUT actually imports/wires the component. The
    # signal is reachability, not the marker — this must pass.
    _scaffold_react(tmp_path, main_jsx="import App from './App.jsx'\n")
    (tmp_path / "src" / "App.jsx").write_text(
        "import { Counter } from './components/Counter'\n"
        "export default function App(){ return <main>"
        "A runnable Vite + React starter generated offline by SkyN3t"
        "<Counter/></main> }\n",
        encoding="utf-8",
    )
    comp = tmp_path / "src" / "components"
    comp.mkdir(parents=True)
    (comp / "Counter.jsx").write_text(
        "export function Counter(){ return <button>0</button> }\n", encoding="utf-8"
    )
    res = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert res.passed is True


def test_real_entry_that_wires_components_passes(tmp_path):
    # App is REAL (no scaffold marker) and imports the component -> fine.
    _scaffold_react(tmp_path, main_jsx="import App from './App.jsx'\n")
    (tmp_path / "src" / "App.jsx").write_text(
        "import Gallery from './components/Gallery.jsx'\n"
        "export default function App(){ return <Gallery/> }\n",
        encoding="utf-8",
    )
    comp = tmp_path / "src" / "components"
    comp.mkdir(parents=True)
    (comp / "Gallery.jsx").write_text(
        "export default function Gallery(){ return <ul/> }\n", encoding="utf-8"
    )
    res = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert res.passed is True


def test_pure_scaffold_stub_without_components_is_not_flagged(tmp_path):
    # A legit pure offline scaffold (stub backend) has NO extra components; proof
    # must not start failing minimal scaffolds — only the half-wired case.
    _scaffold_react(tmp_path, main_jsx="import App from './App.jsx'\n")
    (tmp_path / "src" / "App.jsx").write_text(_STUB_APP, encoding="utf-8")
    res = proof_run(tmp_path, stack="react", execution_backend="inline")
    assert res.passed is True
