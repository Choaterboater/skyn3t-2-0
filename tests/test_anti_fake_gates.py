"""Anti-fake proof gates (wave-2 items 44/47/48).

Three deterministic, offline gap-producers that feed the existing fix-loop
without flipping the verdict by themselves:

* banned-placeholder scan (Bolt full-file contract) — flags "rest of code",
  "unchanged */", "TODO: implement", "lorem ipsum" in delivered source;
* reality-checker — concrete brief feature words with no trace in the delivered
  source/README become gaps;
* scaffold-originality — a delivered tree that is essentially the pristine
  scaffold is flagged as "shipped the stub".

The gates NEVER auto-delete code and NEVER flip ``passed`` on their own; they add
gaps that the convergence fix-loop consumes.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents._scaffold import scaffold_for
from skyn3t.studio.proof_run import (
    detect_scaffold_stub,
    extract_error_gaps,
    missing_brief_features,
    proof_run,
    scan_placeholder_markers,
)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# --- B7: banned-placeholder scan ----------------------------------------------

def test_scan_placeholder_markers_flags_elided_source(tmp_path) -> None:
    _write(tmp_path, {
        "src/app.js": "function main() {\n  // rest of code...\n}\n",
        "src/util.js": "export const x = 1; // TODO: implement caching later\n",
        "index.html": "<p>Lorem ipsum dolor sit amet</p>\n",
    })
    markers = scan_placeholder_markers(tmp_path)
    joined = " ".join(markers).lower()
    assert any("src/app.js" in m for m in markers)
    assert "rest of code" in joined
    assert "todo: implement" in joined
    assert "lorem ipsum" in joined


def test_scan_placeholder_markers_clean_tree_is_empty(tmp_path) -> None:
    _write(tmp_path, {
        "src/app.js": "function main() {\n  return 42;\n}\n",
        # a bare 'TODO' (not the elision idiom) must NOT trip the scan
        "notes.md": "TODO list feature planned\n",
    })
    assert scan_placeholder_markers(tmp_path) == []


def test_scan_placeholder_markers_never_deletes(tmp_path) -> None:
    _write(tmp_path, {"src/a.js": "// rest of code...\n"})
    scan_placeholder_markers(tmp_path)
    assert (tmp_path / "src/a.js").exists()  # gaps only, never auto-delete


# --- B8: reality-checker ------------------------------------------------------

def test_missing_brief_features_flags_absent_feature(tmp_path) -> None:
    _write(tmp_path, {
        "src/app.js": "// a recipe box that stores recipes\nconst recipes = [];\n",
        "README.md": "# Recipe box\nStores your recipes.\n",
    })
    missing = missing_brief_features(
        tmp_path, "a recipe box that stores recipes and generates a shopping list")
    low = [m.lower() for m in missing]
    assert "shopping" in low          # asked for, nowhere in the delivered code
    assert "recipes" not in low       # present in the source, not flagged


def test_missing_brief_features_skips_generic_words(tmp_path) -> None:
    _write(tmp_path, {"src/app.js": "const x = 1;\n"})
    missing = [m.lower() for m in missing_brief_features(
        tmp_path, "build a simple application website page for users")]
    # generic scaffolding words are stoplisted, never reported as missing features
    for generic in ("build", "simple", "application", "website", "page", "users"):
        assert generic not in missing


# --- B9: scaffold-originality -------------------------------------------------

def test_detect_scaffold_stub_flags_untouched_scaffold(tmp_path) -> None:
    brief = "a task manager"
    for rel, content in scaffold_for("react_vite", "demo", brief).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    note = detect_scaffold_stub(tmp_path, "react_vite", brief)
    assert note is not None
    assert "scaffold" in note.lower()


def test_detect_scaffold_stub_none_for_real_app(tmp_path) -> None:
    brief = "a task manager"
    files = dict(scaffold_for("react_vite", "demo", brief))
    # replace the demo App with a substantial real component + add real modules
    files["src/App.jsx"] = (
        "import { useState } from 'react'\n"
        + "export default function App(){\n"
        + "\n".join(f"  const [v{i}, setV{i}] = useState({i})" for i in range(40))
        + "\n  return <main>real task manager UI</main>\n}\n"
    )
    files["src/store.js"] = "export const store = {" + ",".join(
        f"k{i}:{i}" for i in range(80)) + "}\n"
    _write(tmp_path, files)
    assert detect_scaffold_stub(tmp_path, "react_vite", brief) is None


# --- surfacing through extract_error_gaps + proof_run integration -------------

def test_extract_error_gaps_surfaces_anti_fake_detail() -> None:
    detail = {
        "placeholder_markers": ["src/a.js: rest of code"],
        "missing_features": ["shopping"],
        "scaffold_stub": "delivered tree is essentially the react_vite scaffold",
    }
    gaps = " ".join(extract_error_gaps(detail, [])).lower()
    assert "rest of code" in gaps
    assert "shopping" in gaps
    assert "scaffold" in gaps


def test_proof_run_records_anti_fake_gaps_without_flipping_pass(tmp_path) -> None:
    # A runnable static app that nonetheless contains a placeholder marker: proof
    # still PASSES (the marker never flips the verdict), but the gap is recorded.
    _write(tmp_path, {
        "index.html": (
            "<!doctype html><html><head><title>Timer</title></head>"
            "<body><h1>Timer</h1><script src='./app.js'></script>"
            "<!-- rest of code... --></body></html>\n"
        ),
        "app.js": "console.log('timer');\n",
    })
    res = proof_run(tmp_path, stack="static_html", brief="a countdown timer")
    assert res.passed is True
    assert res.detail.get("placeholder_markers")
    assert any("rest of code" in g.lower() for g in res.error_gaps())
