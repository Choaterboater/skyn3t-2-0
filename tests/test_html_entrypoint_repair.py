"""index.html entrypoint-clobber safety net.

A weak codegen model can ship an index.html whose only ``<script>`` is an inline
toy page that never imports the app, so the real game never loads and entities
stay primitive rectangles. ``_repair_html_entrypoint`` restores the scaffold's
known-good index.html in that case — but NEVER when the delivered file already
loads a real entrypoint, and NEVER when the scaffold's entrypoint was legitimately
renamed away (which would otherwise wire in a dead loader).
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus

_GOOD_INDEX = (
    '<!doctype html><html><body><div id="game-container"></div>'
    '<script type="module" src="/src/main.js"></script></body></html>'
)
_CLOBBERED_INDEX = (
    '<!doctype html><html><body><canvas id="gameCanvas"></canvas>'
    '<script>const ctx = document.querySelector("canvas").getContext("2d");</script>'
    "</body></html>"
)
_SCAFFOLD = {"index.html": _GOOD_INDEX, "src/main.js": "// scaffold entry\n"}


def _agent() -> CodeAgent:
    return CodeAgent(event_bus=EventBus())


def test_intact_detects_valid_module_entrypoint():
    files = {"index.html": _GOOD_INDEX, "src/main.js": "new Phaser.Game({})\n"}
    assert _agent()._html_entrypoint_intact(_GOOD_INDEX, files) is True


def test_intact_false_when_only_inline_script():
    files = {"index.html": _CLOBBERED_INDEX, "src/main.js": "new Phaser.Game({})\n"}
    assert _agent()._html_entrypoint_intact(_CLOBBERED_INDEX, files) is False


def test_intact_false_when_module_src_missing():
    # module script points at a file that was never written -> not wired
    html = (
        '<!doctype html><html><body><div id="game-container"></div>'
        '<script type="module" src="/src/ghost.js"></script></body></html>'
    )
    files = {"index.html": html, "src/main.js": "new Phaser.Game({})\n"}
    assert _agent()._html_entrypoint_intact(html, files) is False


def test_repair_restores_scaffold_when_clobbered(tmp_path):
    a = _agent()
    (tmp_path / "src").mkdir()
    (tmp_path / "index.html").write_text(_CLOBBERED_INDEX)
    (tmp_path / "src" / "main.js").write_text("new Phaser.Game({})\n")
    files = {"index.html": _CLOBBERED_INDEX, "src/main.js": "new Phaser.Game({})\n"}
    out = a._repair_html_entrypoint(tmp_path, files, "phaser", _SCAFFOLD)
    assert out["index.html"] == _GOOD_INDEX
    assert (tmp_path / "index.html").read_text() == _GOOD_INDEX
    assert a.metadata.get("index_html_repaired") is True


def test_repair_noop_when_entrypoint_intact(tmp_path):
    a = _agent()
    files = {"index.html": _GOOD_INDEX, "src/main.js": "new Phaser.Game({})\n"}
    out = a._repair_html_entrypoint(tmp_path, files, "phaser", _SCAFFOLD)
    assert out["index.html"] == _GOOD_INDEX
    assert a.metadata.get("index_html_repaired") is not True


def test_repair_restores_phaser_entry_when_html_loads_react_shell(tmp_path):
    # A stale React Vite shell can coexist with a real Phaser src/main.js. For
    # Phaser, "module src exists" is not enough: index.html must load the Phaser
    # boot file, or the delivered app is a website with dead game code.
    a = _agent()
    react_index = (
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script></body></html>'
    )
    files = {
        "index.html": react_index,
        "src/main.jsx": "import React from 'react';\n",
        "src/main.js": "class MainScene extends Phaser.Scene {}\nnew Phaser.Game({ scene: [MainScene] })\n",
    }
    (tmp_path / "src").mkdir()
    (tmp_path / "index.html").write_text(react_index)
    (tmp_path / "src" / "main.js").write_text(files["src/main.js"])

    out = a._repair_html_entrypoint(tmp_path, files, "phaser", _SCAFFOLD)

    assert out["index.html"] == _GOOD_INDEX
    assert (tmp_path / "index.html").read_text() == _GOOD_INDEX
    assert a.metadata.get("index_html_repaired") is True


def test_deterministic_repairs_restore_phaser_entrypoint_on_disk(tmp_path):
    from skyn3t.studio.proof_run import apply_deterministic_repairs

    react_index = (
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script></body></html>'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "index.html").write_text(react_index)
    (tmp_path / "src" / "main.jsx").write_text("import React from 'react';\n")
    (tmp_path / "src" / "main.js").write_text(
        "class MainScene extends Phaser.Scene {}\nnew Phaser.Game({ scene: [MainScene] })\n"
    )

    repairs = apply_deterministic_repairs(tmp_path, stack="phaser")

    assert repairs["phaser_entrypoint_repaired"] == ["index.html"]
    assert 'src="/src/main.js"' in (tmp_path / "index.html").read_text()


def test_repair_skips_when_scaffold_entry_renamed(tmp_path):
    # Delivered index is broken (inline only) but the scaffold's entrypoint file
    # (src/main.js) was NOT delivered — the agent renamed it. Restoring the
    # scaffold index.html would wire a dead loader, so the repair must skip.
    a = _agent()
    (tmp_path / "index.html").write_text(_CLOBBERED_INDEX)
    files = {"index.html": _CLOBBERED_INDEX, "src/app.js": "new Phaser.Game({})\n"}
    out = a._repair_html_entrypoint(tmp_path, files, "phaser", _SCAFFOLD)
    assert out["index.html"] == _CLOBBERED_INDEX
    assert a.metadata.get("index_html_repaired") is not True


def test_repair_noop_for_non_html_stack(tmp_path):
    # A stack with no scaffold index.html (e.g. a CLI/api app) is never touched.
    a = _agent()
    files = {"main.py": "print('hi')\n"}
    out = a._repair_html_entrypoint(tmp_path, files, "fastapi", {"main.py": "x\n"})
    assert out == files
    assert a.metadata.get("index_html_repaired") is not True
