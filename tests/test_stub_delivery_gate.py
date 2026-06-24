"""Verifier must reject a delivered scaffold stub + not be fooled by node_modules.

A real-backend build that ships the offline scaffold counter (codegen produced
nothing over it) previously scored 100/go, because (a) _largest_source_bytes
summed node_modules .js (MBs) so the substance gate always passed, and (b) a
pure stub has no orphaned components for the reachability gate to catch.
"""

from __future__ import annotations

from skyn3t.studio.runner import StudioRunner

_STUB_APP = (
    "import { useState } from 'react'\n\n"
    "export default function App() {\n"
    "  const [count, setCount] = useState(0)\n"
    "  return (<main><h1>My app</h1>"
    "<p>A runnable Vite + React starter generated offline by SkyN3t.</p>"
    "<button onClick={() => setCount(c => c + 1)}>count is {count}</button></main>)\n"
    "}\n"
)


def _runner():
    # The gate methods only touch class attrs (_SOURCE_EXTS, _NON_SOURCE_DIRS),
    # so bypass the heavy __init__.
    return StudioRunner.__new__(StudioRunner)


# --- fix 1: substance signal excludes installed/built dirs -------------------

def test_largest_source_excludes_node_modules_and_dist(tmp_path):
    r = _runner()
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text("x" * 500, encoding="utf-8")
    nm = tmp_path / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("y" * 200_000, encoding="utf-8")  # 200KB dep
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("z" * 100_000, encoding="utf-8")
    # Only the real app source counts — deps/build output excluded.
    assert r._largest_source_bytes(str(tmp_path)) == 500


# --- fix 2: scaffold-stub-delivery detection --------------------------------

def test_pure_scaffold_stub_is_flagged(tmp_path):
    r = _runner()
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text(_STUB_APP, encoding="utf-8")
    (src / "main.jsx").write_text("import App from './App.jsx'\n", encoding="utf-8")
    assert r._delivered_scaffold_stub(str(tmp_path)) is True


def test_wired_app_keeping_marker_is_not_flagged(tmp_path):
    # A real app that kept the marker subtitle but imports a real component.
    r = _runner()
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text(
        "import Counter from './components/Counter.jsx'\n"
        "export default function App(){ return <main>generated offline by SkyN3t"
        "<Counter/></main> }\n",
        encoding="utf-8",
    )
    comp = src / "components"
    comp.mkdir()
    (comp / "Counter.jsx").write_text(
        "export default function Counter(){ return <button>tap</button> }\n" * 3,
        encoding="utf-8",
    )
    assert r._delivered_scaffold_stub(str(tmp_path)) is False


def test_real_app_without_marker_is_not_flagged(tmp_path):
    r = _runner()
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text(
        "import Gallery from './components/Gallery.jsx'\n"
        "export default function App(){ return <Gallery/> }\n",
        encoding="utf-8",
    )
    comp = src / "components"
    comp.mkdir()
    (comp / "Gallery.jsx").write_text(
        "export default function Gallery(){ return <ul/> }\n" * 3, encoding="utf-8"
    )
    assert r._delivered_scaffold_stub(str(tmp_path)) is False
