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


def test_ai_look_warnings_are_advisory_never_blocking(tmp_path):
    (tmp_path / "index.html").write_text(
        "<link rel='stylesheet' href='style.css'>"
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(
        "body { font-family: Inter, sans-serif; "
        "background: linear-gradient(135deg, #6366f1, #8b5cf6); }\n"
        ".card { backdrop-filter: blur(8px); border-radius: 1.5rem; "
        "box-shadow: 0 25px 50px rgba(0,0,0,.2); }\n"
        ".panel { backdrop-filter: blur(4px); border-radius: 24px; "
        "box-shadow: 0 20px 40px rgba(0,0,0,.2); }\n",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    # Taste is advisory: never a blocking issue, always a recorded warning.
    assert verdict["ok"] is True
    assert verdict["issues"] == []
    joined = " ".join(verdict["warnings"])
    assert "indigo/violet gradient" in joined
    assert "Inter-first typography" in joined
    assert "glassmorphism" in joined


def test_ai_look_detects_tailwind_indigo_gradient(tmp_path):
    (tmp_path / "App.jsx").write_text(
        "export default () => (<main className='bg-gradient-to-r from-indigo-500 "
        "to-purple-600'><h1 className='text-4xl'>Planner</h1>"
        "<a href='/start'>Start</a></main>);",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert any("indigo/violet gradient" in w for w in verdict["warnings"])


def test_ai_look_clean_page_has_no_ai_warnings(tmp_path):
    (tmp_path / "index.html").write_text(
        "<link rel='stylesheet' href='style.css'>"
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(
        "body { font-family: Georgia, serif; background: #f8f7f5; color: #1c1917; }\n",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert verdict["warnings"] == []


def test_ai_look_flags_full_viewport_hero(tmp_path):
    # Advisory only: full-bleed heroes are a legitimate choice, so this never
    # blocks — it records the smell for the operator.
    (tmp_path / "index.html").write_text(
        "<header class='hero min-h-screen grid'><h1>Planner</h1>"
        "<a href='/start'>Start</a></header>",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert verdict["issues"] == []
    assert any("full-viewport hero detected" in w for w in verdict["warnings"])


def test_ai_look_flags_identical_card_grid(tmp_path):
    cards = "".join(
        f"<div class='card rounded shadow'><h2>Plan {i}</h2><a href='/p{i}'>Open</a></div>"
        for i in range(3)
    )
    (tmp_path / "index.html").write_text(
        "<main class='grid'><h1>Planner</h1>" + cards + "</main>",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert any("identical repeated card grid" in w for w in verdict["warnings"])


def test_ai_look_flags_placeholder_copy(tmp_path):
    (tmp_path / "index.html").write_text(
        "<main class='grid'><h1>Team</h1><a href='/start'>Start</a>"
        "<p>John Doe, CEO of Acme Corp</p></main>",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert any(
        "placeholder copy detected ('John Doe')" in w for w in verdict["warnings"]
    )


def test_ai_look_flags_bounce_easing(tmp_path):
    (tmp_path / "index.html").write_text(
        "<link rel='stylesheet' href='style.css'>"
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(
        ".card { transition: transform .3s cubic-bezier(0.68, -0.55, 0.265, 1.55); }\n",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert any("bounce/elastic easing" in w for w in verdict["warnings"])


def test_ai_look_flags_styled_button_outside_scaffold_primitives(tmp_path):
    src = tmp_path / "src" / "components"
    src.mkdir(parents=True)
    (src / "ui.tsx").write_text(
        "export const Button = (p: any) => <button className='btn' {...p} />;\n",
        encoding="utf-8",
    )
    (src / "App.tsx").write_text(
        "export default () => (<main className='grid'><h1 className='text-4xl'>Planner</h1>"
        "<button className='bg-teal-600 rounded-lg px-4 py-2' onClick={go}>Start</button>"
        "</main>);",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert any("hand-rolled styled <button>" in w for w in verdict["warnings"])


def test_ai_look_well_built_page_has_none_of_the_new_warnings(tmp_path):
    # Varied cards, content-sized hero, real copy, ease-out transitions, and
    # composed scaffold primitives must produce NONE of the five new warnings.
    src = tmp_path / "src" / "components"
    src.mkdir(parents=True)
    (src / "ui.tsx").write_text(
        "export const Button = (p: any) => <button className='btn' {...p} />;\n",
        encoding="utf-8",
    )
    (src / "App.tsx").write_text(
        "import { Button } from './ui';\n"
        "export default () => (<section className='grid'><Button>Start</Button></section>);\n",
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(
        "<link rel='stylesheet' href='style.css'>"
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a>"
        "<section>"
        "<div class='card card-featured'><h2>Focus</h2><p>Daily plan for Amara Osei.</p></div>"
        "<div class='card card-compact'><h2>Notes</h2><p>Kickoff notes from Meridian Studio.</p></div>"
        "<div class='card-wide card'><h2>Stats</h2><p>Streak: 12 days.</p></div>"
        "</section></main>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(
        "body { font-family: Georgia, serif; background: #f8f7f5; color: #1c1917; }\n"
        ".card { transition: transform 200ms ease-out; }\n",
        encoding="utf-8",
    )

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is True
    assert verdict["warnings"] == []


def _y2k_fixture(tmp_path):
    """A page that is playful + gallery-gridded BY DESIGN (the y2k brief)."""
    (tmp_path / "index.html").write_text(
        "<link rel='stylesheet' href='style.css'>"
        "<main class='grid hero'><h1>My Works</h1>"
        "<section class='gallery'>"
        "<div class='card'>piece one " + chr(0x1F308) + "</div>"
        "<div class='card'>piece two " + chr(0x2B50) + "</div>"
        "<div class='card'>piece three " + chr(0x1F3A8) + "</div>"
        "</section>"
        "<a href='/guestbook'>Sign my guestbook</a></main>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(
        "body { font-family: Georgia, serif; background: #fdf2f8; color: #1c1917; }\n",
        encoding="utf-8",
    )


def test_invited_playful_and_grid_produce_no_warnings(tmp_path):
    _y2k_fixture(tmp_path)
    brief = ("a playful Y2K-era personal portfolio with pastel sticker badges "
             "and a gallery grid of six works")

    verdict = check_web_polish(tmp_path, "static", brief)

    assert verdict["ok"] is True
    assert verdict["warnings"] == []


def test_uninvited_playful_and_grid_still_warn(tmp_path):
    _y2k_fixture(tmp_path)

    # Same page, neutral brief -> both detectors fire as before.
    verdict = check_web_polish(tmp_path, "static", "a contractor invoice tool")
    assert any("decorative emoji" in w for w in verdict["warnings"])
    assert any("identical repeated card grid" in w for w in verdict["warnings"])

    # And with no brief at all (legacy callers) behavior is unchanged.
    verdict = check_web_polish(tmp_path, "static")
    assert any("decorative emoji" in w for w in verdict["warnings"])
    assert any("identical repeated card grid" in w for w in verdict["warnings"])
