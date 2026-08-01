from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.grounding_check import check_grounding
from skyn3t.studio.improve import _grounding_fix_hints
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner


def test_grounding_flags_undefined_var_in_markup_and_css(tmp_path):
    (tmp_path / "index.html").write_text(
        "<main style='color: var(--brand-ink)'><h1>Planner</h1></main>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(
        "body { background: var(--surface-bg); }\n", encoding="utf-8"
    )

    verdict = check_grounding(tmp_path, "static")

    # Advisory only: never blocking, always a recorded warning.
    assert verdict["ok"] is True
    assert verdict["issues"] == []
    assert any("var(--brand-ink)" in w and "index.html:1" in w for w in verdict["warnings"])
    assert any("var(--surface-bg)" in w and "style.css:1" in w for w in verdict["warnings"])


def test_grounding_var_with_fallback_is_clean(tmp_path):
    (tmp_path / "index.html").write_text(
        "<main style='color: var(--brand-ink, #1c1917)'><h1>Planner</h1></main>",
        encoding="utf-8",
    )

    verdict = check_grounding(tmp_path, "static")

    assert verdict["ok"] is True
    assert verdict["warnings"] == []


def test_grounding_var_defined_in_style_block_is_clean(tmp_path):
    (tmp_path / "index.html").write_text(
        "<style>:root { --brand-ink: #1c1917; }</style>"
        "<main style='color: var(--brand-ink)'><h1>Planner</h1></main>",
        encoding="utf-8",
    )

    verdict = check_grounding(tmp_path, "static")

    assert verdict["ok"] is True
    assert verdict["warnings"] == []


def test_grounding_var_defined_in_css_used_in_markup_is_clean(tmp_path):
    (tmp_path / "style.css").write_text(
        ":root { --brand-ink: #1c1917; }\n", encoding="utf-8"
    )
    (tmp_path / "App.jsx").write_text(
        "export default () => <main style={{ color: 'var(--brand-ink)' }}>hi</main>;",
        encoding="utf-8",
    )

    verdict = check_grounding(tmp_path, "static")

    assert verdict["warnings"] == []


def test_grounding_allowlisted_framework_vars_are_clean(tmp_path):
    (tmp_path / "index.astro").write_text(
        "<main style='height: var(--vh); width: var(--vw); "
        "color: var(--astro-content-color); margin: var(--next-gap)'>x</main>",
        encoding="utf-8",
    )

    verdict = check_grounding(tmp_path, "astro")

    assert verdict["warnings"] == []


def test_grounding_flags_phantom_local_import(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text(
        "import Modal from './components/Modal';\n"
        "export default () => <main><Modal /></main>;\n",
        encoding="utf-8",
    )

    verdict = check_grounding(tmp_path, "static")

    assert verdict["ok"] is True
    assert any(
        "phantom local import './components/Modal'" in w and "src/App.jsx:1" in w
        for w in verdict["warnings"]
    )


def test_grounding_existing_import_is_clean(tmp_path):
    src = tmp_path / "src"
    (src / "components").mkdir(parents=True)
    (src / "components" / "Modal.jsx").write_text(
        "export default () => <div />;\n", encoding="utf-8"
    )
    (src / "App.jsx").write_text(
        "import Modal from './components/Modal';\n"
        "import '../styles/global.css';\n"
        "export default () => <main><Modal /></main>;\n",
        encoding="utf-8",
    )
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "global.css").write_text("body { color: #123; }\n", encoding="utf-8")

    verdict = check_grounding(tmp_path, "static")

    assert verdict["warnings"] == []


def test_grounding_directory_index_import_is_clean(tmp_path):
    src = tmp_path / "src"
    (src / "components" / "Modal").mkdir(parents=True)
    (src / "components" / "Modal" / "index.tsx").write_text(
        "export default () => <div />;\n", encoding="utf-8"
    )
    (src / "App.tsx").write_text(
        "import Modal from './components/Modal';\nexport default Modal;\n",
        encoding="utf-8",
    )

    verdict = check_grounding(tmp_path, "static")

    assert verdict["warnings"] == []


def test_grounding_skips_phaser_and_non_ui_stacks(tmp_path):
    (tmp_path / "index.html").write_text(
        "<main style='color: var(--nope)'><h1>x</h1></main>", encoding="utf-8"
    )

    assert check_grounding(tmp_path, "phaser")["skipped"] is True
    assert check_grounding(tmp_path, "rag")["skipped"] is True
    assert check_grounding(tmp_path / "missing", "static")["skipped"] is True


def test_grounding_ignores_node_modules_and_css_at_imports(tmp_path):
    vendor = tmp_path / "node_modules" / "pkg"
    vendor.mkdir(parents=True)
    (vendor / "broken.jsx").write_text(
        "import x from './missing';\nexport default x;\n", encoding="utf-8"
    )
    (tmp_path / "style.css").write_text(
        "@import './vendor/theme.css';\n"
        "body { background: url('./img/bg.png'); }\n",
        encoding="utf-8",
    )

    verdict = check_grounding(tmp_path, "static")

    assert verdict["warnings"] == []
    assert not any("node_modules" in c for c in verdict["checked"])


def test_grounding_gate_records_on_manifest_and_merges_warnings(tmp_path):
    (tmp_path / "index.html").write_text(
        "<link rel='stylesheet' href='style.css'>"
        "<main class='grid hero' style='color: var(--brand-ink)'>"
        "<h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(
        "body { font-family: Georgia, serif; }\n", encoding="utf-8"
    )
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"),
        memory=None,
    )
    man = BuildManifest(slug="x", brief="site", stack="static")

    score, verdict = runner._run_web_polish_gate(
        man, str(tmp_path), SimpleNamespace(stack="static"), 88.0, "go"
    )

    # Grounding is advisory: score and verdict are untouched.
    assert verdict == "go"
    assert score == 88.0
    grounding = man.extra["grounding"]
    assert grounding["ok"] is True
    assert grounding["skipped"] is False
    assert any("var(--brand-ink)" in w for w in grounding["warnings"])
    # ... and merged into the polish record so the operator sees one list.
    assert any("var(--brand-ink)" in w for w in man.extra["web_polish"]["warnings"])


def test_improve_grounding_fix_hints_are_advisory(tmp_path):
    (tmp_path / "index.html").write_text(
        "<main style='color: var(--brand-ink)'><h1>Planner</h1></main>",
        encoding="utf-8",
    )

    hints = _grounding_fix_hints(tmp_path, "static")

    assert any("var(--brand-ink)" in h for h in hints)
    # Never raises on a bogus tree, just yields no hints.
    assert _grounding_fix_hints(tmp_path / "missing", "static") == []
