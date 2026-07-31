from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.web_polish_check import check_web_polish


def test_web_polish_flags_thin_page(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>hello</body></html>", encoding="utf-8")

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is False
    assert len(verdict["issues"]) >= 2


def test_web_polish_accepts_structured_page(tmp_path):
    (tmp_path / "index.html").write_text(
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )

    assert check_web_polish(tmp_path, "static")["ok"] is True


def test_web_polish_flags_decorative_emoji_ui(tmp_path):
    # Emoji are a weak, brief-dependent signal: advisory warning, never a
    # blocking issue (a single glyph must not fail an otherwise polished UI).
    (tmp_path / "index.html").write_text(
        "<main class='grid hero'><h1>Planner</h1><button>" + chr(0x1F3CC) + " Start</button></main>",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert verdict["issues"] == []
    assert any("decorative emoji" in w for w in verdict["warnings"])


def test_web_polish_emoji_warning_counts_glyphs_and_ignores_css(tmp_path):
    (tmp_path / "index.html").write_text(
        "<main class='grid hero'><h1>Planner</h1>"
        + "<button>" + chr(0x2605) + chr(0x1F3CC) + " Start</button></main>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(
        "body::before { content: '" + chr(0x2699) + "'; }\n", encoding="utf-8"
    )
    (tmp_path / "app.js").write_text(
        "document.querySelector('link[rel=stylesheet]');\n"
        "// wired via <link rel=\"stylesheet\" href=\"style.css\">\n",
        encoding="utf-8",
    )
    (tmp_path / "page.html").write_text(
        "<link rel='stylesheet' href='style.css'><main><h1>Planner</h1></main>",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    # CSS content is excluded from the emoji scan; markup glyphs are counted.
    assert verdict["ok"] is True
    assert "decorative emoji glyphs detected in UI source (2)" in verdict["warnings"]


def test_web_polish_flags_unwired_stylesheet(tmp_path):
    styles = tmp_path / "src" / "styles"
    styles.mkdir(parents=True)
    (styles / "global.css").write_text("body { color: #123; }\n", encoding="utf-8")
    layout = tmp_path / "src" / "layouts"
    layout.mkdir()
    (layout / "BaseLayout.astro").write_text(
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "astro")

    assert verdict["ok"] is False
    assert "stylesheets exist but no CSS import or stylesheet link was found" in verdict["issues"]


def test_web_polish_accepts_imported_stylesheet(tmp_path):
    styles = tmp_path / "src" / "styles"
    styles.mkdir(parents=True)
    (styles / "global.css").write_text("body { color: #123; }\n", encoding="utf-8")
    (tmp_path / "Layout.astro").write_text(
        "---\nimport './src/styles/global.css';\n---\n"
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )

    assert check_web_polish(tmp_path, "astro")["ok"] is True


def test_web_polish_covers_web_stack_alias_component_files(tmp_path):
    (tmp_path / "App.svelte").write_text(
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "svelte")

    assert verdict["skipped"] is False
    assert verdict["ok"] is True


def test_web_polish_skips_phaser_canvas_games(tmp_path):
    (tmp_path / "index.html").write_text(
        "<div id='game-container'></div><script type='module' src='/src/main.js'></script>",
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.js").write_text("new Phaser.Game({ parent: 'game-container' })", encoding="utf-8")

    verdict = check_web_polish(tmp_path, "phaser")

    assert verdict["skipped"] is True
    assert verdict["ok"] is True


def test_web_polish_gate_downgrades_thin_ui(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>hello</body></html>", encoding="utf-8")
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

    assert verdict == "no_go"
    assert score == 49.0
    assert man.extra["web_polish"]["ok"] is False
