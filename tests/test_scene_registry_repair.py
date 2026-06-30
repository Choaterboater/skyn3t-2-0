"""Scene-graph (entry-point) repair net — the orphaned-scene clobber.

Codegen can split a game into real Phaser ``Scene`` modules (BootScene.js,
GameScene.js, …) that each ``export default`` a Scene subclass, yet ship a
``src/main.js`` that never imports them and runs its OWN throwaway inline scene.
The polished game is then orphaned dead code and the player sees "just ships" /
placeholders. ``_repair_scene_registry`` rewrites main.js to import + register the
real scenes — but NEVER when main.js already imports a real scene module, and
NEVER for a correctly wired single-scene game.
"""

from __future__ import annotations

import os
import re

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus

_BOOT = "export default class BootScene extends Phaser.Scene { constructor(){ super('Boot'); } preload(){} }\n"
_MENU = "export default class MenuScene extends Phaser.Scene { constructor(){ super('Menu'); } create(){} }\n"
_GAME = "export default class GameScene extends Phaser.Scene { constructor(){ super('Game'); } create(){} }\n"
_OVER = "export default class GameOverScene extends Phaser.Scene { constructor(){ super('GameOver'); } create(){} }\n"

# main.js that runs its OWN inline scenes and never imports the real modules.
_CLOBBERED_MAIN = (
    "import Phaser from 'phaser';\n"
    "import './styles.css';\n"
    "\n"
    "class BootScene extends Phaser.Scene { constructor(){ super('Boot'); } preload(){} }\n"
    "class GameScene extends Phaser.Scene { constructor(){ super('Game'); } create(){} }\n"
    "\n"
    "const config = {\n"
    "  type: Phaser.AUTO,\n"
    "  parent: 'game-container',\n"
    "  backgroundColor: '#070b1a',\n"
    "  width: 800,\n"
    "  height: 600,\n"
    "  scene: [BootScene, GameScene],\n"
    "};\n"
    "\n"
    "new Phaser.Game(config);\n"
)

# main.js that correctly imports + registers the real scene modules.
_INTACT_MAIN = (
    "import Phaser from 'phaser';\n"
    "import BootScene from './BootScene.js';\n"
    "import MenuScene from './MenuScene.js';\n"
    "import GameScene from './GameScene.js';\n"
    "import GameOverScene from './GameOverScene.js';\n"
    "\n"
    "const config = {\n"
    "  type: Phaser.AUTO,\n"
    "  parent: 'game-container',\n"
    "  width: 800,\n"
    "  height: 600,\n"
    "  scene: [BootScene, MenuScene, GameScene, GameOverScene],\n"
    "};\n"
    "\n"
    "new Phaser.Game(config);\n"
)


def _scene_files() -> dict[str, str]:
    return {
        "src/BootScene.js": _BOOT,
        "src/MenuScene.js": _MENU,
        "src/GameScene.js": _GAME,
        "src/GameOverScene.js": _OVER,
        "src/config.js": "export const WORLD = { WIDTH: 800, HEIGHT: 600 };\n",
        "src/styles.css": "body{margin:0}\n",
    }


def _agent() -> CodeAgent:
    return CodeAgent(event_bus=EventBus())


# ---- detection: _scene_modules ------------------------------------------------

def test_scene_modules_finds_exported_scenes_only():
    files = {**_scene_files(), "src/main.js": _CLOBBERED_MAIN}
    mods = dict(CodeAgent._scene_modules(files))
    assert set(mods) == {
        "src/BootScene.js",
        "src/MenuScene.js",
        "src/GameScene.js",
        "src/GameOverScene.js",
    }
    # main.js and non-scene modules (config.js) are excluded
    assert "src/main.js" not in mods
    assert "src/config.js" not in mods
    # idents are valid default-import names derived from the filename
    assert mods["src/BootScene.js"] == "BootScene"


def test_scene_modules_sanitizes_filename_to_identifier():
    files = {
        "src/game-scene.js": _GAME,
        "src/2nd-scene.js": _MENU,
    }
    mods = dict(CodeAgent._scene_modules(files))
    assert mods["src/game-scene.js"] == "gamescene"
    assert mods["src/2nd-scene.js"][0].isalpha()  # never leads with a digit


# ---- detection: _scene_registry_intact ---------------------------------------

def test_intact_false_when_main_orphans_real_scenes():
    files = {**_scene_files(), "src/main.js": _CLOBBERED_MAIN}
    assert CodeAgent._scene_registry_intact(files) is False


def test_intact_true_when_main_imports_real_scenes():
    files = {**_scene_files(), "src/main.js": _INTACT_MAIN}
    assert CodeAgent._scene_registry_intact(files) is True


def test_intact_true_when_no_separate_scene_modules():
    # A single-scene game keeps everything in main.js — nothing to wire.
    files = {"src/main.js": _CLOBBERED_MAIN}
    assert CodeAgent._scene_registry_intact(files) is True


def test_intact_true_when_main_has_no_inline_scenes():
    # Modules exist and main imports none, but main defines no inline scene of its
    # own (unusual layout) — too ambiguous to repair, so leave it alone.
    main = "import Phaser from 'phaser';\nconst config = { scene: [] };\nnew Phaser.Game(config);\n"
    files = {**_scene_files(), "src/main.js": main}
    assert CodeAgent._scene_registry_intact(files) is True


def test_intact_true_when_no_main():
    assert CodeAgent._scene_registry_intact(_scene_files()) is True


# ---- ordering ----------------------------------------------------------------

def test_order_scenes_boot_first_gameover_last():
    files = {**_scene_files(), "src/main.js": _CLOBBERED_MAIN}
    ordered = [i for _, i in CodeAgent._order_scenes(CodeAgent._scene_modules(files))]
    assert ordered == ["BootScene", "MenuScene", "GameScene", "GameOverScene"]


# ---- repair ------------------------------------------------------------------

def _write(tmp_path, files):
    (tmp_path / "src").mkdir(exist_ok=True)
    for path, text in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def test_repair_rewires_orphaned_scene_graph(tmp_path):
    a = _agent()
    files = {**_scene_files(), "src/main.js": _CLOBBERED_MAIN}
    _write(tmp_path, files)

    out = a._repair_scene_registry(tmp_path, files, "phaser")
    new_main = out["src/main.js"]

    # imports every real scene module
    for ident, stem in [
        ("BootScene", "BootScene"),
        ("MenuScene", "MenuScene"),
        ("GameScene", "GameScene"),
        ("GameOverScene", "GameOverScene"),
    ]:
        assert f"import {ident} from './{stem}.js';" in new_main
    # registers them in order, boot first
    assert "scene: [BootScene, MenuScene, GameScene, GameOverScene]" in new_main
    # exactly one game instantiation, no shadowing inline scene classes
    assert new_main.count("new Phaser.Game(config)") == 1
    assert "class BootScene extends Phaser.Scene" not in new_main
    assert "class GameScene extends Phaser.Scene" not in new_main
    # preserves original non-scene imports and config details
    assert "import './styles.css';" in new_main
    assert "parent: 'game-container'" in new_main
    assert "backgroundColor: '#070b1a'" in new_main
    # written to disk + flagged
    assert (tmp_path / "src" / "main.js").read_text() == new_main
    assert a.metadata.get("scene_registry_repaired") is True


def test_repair_noop_when_intact(tmp_path):
    a = _agent()
    files = {**_scene_files(), "src/main.js": _INTACT_MAIN}
    _write(tmp_path, files)
    out = a._repair_scene_registry(tmp_path, files, "phaser")
    assert out["src/main.js"] == _INTACT_MAIN
    assert a.metadata.get("scene_registry_repaired") is not True


def test_repair_noop_for_non_phaser_stack(tmp_path):
    a = _agent()
    files = {**_scene_files(), "src/main.js": _CLOBBERED_MAIN}
    out = a._repair_scene_registry(tmp_path, files, "nextjs")
    assert out == files
    assert a.metadata.get("scene_registry_repaired") is not True


def test_repair_injects_scene_key_when_config_has_none(tmp_path):
    a = _agent()
    main = (
        "import Phaser from 'phaser';\n"
        "class GameScene extends Phaser.Scene { constructor(){ super('Game'); } }\n"
        "const config = {\n  type: Phaser.AUTO,\n  width: 800,\n  height: 600,\n};\n"
        "new Phaser.Game(config);\n"
    )
    files = {"src/main.js": main, "src/GameScene.js": _GAME, "src/BootScene.js": _BOOT}
    _write(tmp_path, files)
    out = a._repair_scene_registry(tmp_path, files, "phaser")
    new_main = out["src/main.js"]
    assert "scene: [BootScene, GameScene]" in new_main
    assert "import GameScene from './GameScene.js';" in new_main
    assert a.metadata.get("scene_registry_repaired") is True


def test_repair_preserves_inline_game_config(tmp_path):
    # Some models inline the config object inside new Phaser.Game({...}) with no
    # `const config`. The repair must still preserve those settings + swap scenes.
    a = _agent()
    main = (
        "import Phaser from 'phaser';\n"
        "class GameScene extends Phaser.Scene { constructor(){ super('Game'); } }\n"
        "new Phaser.Game({\n"
        "  type: Phaser.AUTO,\n"
        "  parent: 'arena',\n"
        "  width: 480,\n"
        "  height: 640,\n"
        "  scene: [GameScene],\n"
        "});\n"
    )
    files = {"src/main.js": main, "src/BootScene.js": _BOOT, "src/GameScene.js": _GAME}
    _write(tmp_path, files)
    new_main = a._repair_scene_registry(tmp_path, files, "phaser")["src/main.js"]
    assert "parent: 'arena'" in new_main          # original settings preserved
    assert "width: 480" in new_main
    assert "scene: [BootScene, GameScene]" in new_main
    assert "class GameScene extends Phaser.Scene" not in new_main  # inline dropped
    assert new_main.count("new Phaser.Game(config)") == 1


def test_repair_falls_to_default_when_config_is_computed(tmp_path):
    # No `const config` and Game() is called with a computed arg (not an inline
    # object). The repair must NOT grab a stray brace from a helper — it falls back
    # to a safe default config and still registers the real scenes.
    a = _agent()
    main = (
        "import Phaser from 'phaser';\n"
        "class GameScene extends Phaser.Scene { constructor(){ super('Game'); } }\n"
        "function buildConfig(){ return { width: 1234, height: 777 }; }\n"
        "new Phaser.Game(buildConfig());\n"
    )
    files = {"src/main.js": main, "src/BootScene.js": _BOOT, "src/GameScene.js": _GAME}
    _write(tmp_path, files)
    new_main = a._repair_scene_registry(tmp_path, files, "phaser")["src/main.js"]
    assert "scene: [BootScene, GameScene]" in new_main
    assert "width: 1234" not in new_main  # did NOT steal the helper's object
    assert "new Phaser.Game(config)" in new_main
    assert new_main.count("{") == new_main.count("}")


def test_repaired_main_has_balanced_braces(tmp_path):
    a = _agent()
    files = {**_scene_files(), "src/main.js": _CLOBBERED_MAIN}
    _write(tmp_path, files)
    new_main = a._repair_scene_registry(tmp_path, files, "phaser")["src/main.js"]
    assert new_main.count("{") == new_main.count("}")
    assert new_main.count("(") == new_main.count(")")
    assert new_main.count("[") == new_main.count("]")


def test_repair_handles_scenes_in_subdirectory(tmp_path):
    # Scenes under src/scenes/ must import as './scenes/X.js', NOT flattened to
    # './X.js' (which would fail to resolve and break the whole game).
    a = _agent()
    files = {
        "src/main.js": _CLOBBERED_MAIN,
        "src/scenes/BootScene.js": _BOOT,
        "src/scenes/GameScene.js": _GAME,
    }
    _write(tmp_path, files)
    new_main = a._repair_scene_registry(tmp_path, files, "phaser")["src/main.js"]
    assert "import BootScene from './scenes/BootScene.js';" in new_main
    assert "import GameScene from './scenes/GameScene.js';" in new_main
    assert "from './BootScene.js'" not in new_main  # not flattened
    assert "scene: [BootScene, GameScene]" in new_main


def test_repair_preserves_multiline_import(tmp_path):
    # A multi-line destructured import in the original main.js must be preserved
    # whole, not truncated to a dangling `import {`.
    a = _agent()
    main = (
        "import Phaser from 'phaser';\n"
        "import {\n  WORLD,\n  PALETTE,\n} from './config.js';\n"
        "class GameScene extends Phaser.Scene { constructor(){ super('Game'); } }\n"
        "const config = { width: WORLD.W, height: WORLD.H, scene: [GameScene] };\n"
        "new Phaser.Game(config);\n"
    )
    files = {"src/main.js": main, "src/BootScene.js": _BOOT, "src/GameScene.js": _GAME,
             "src/config.js": "export const WORLD={W:800,H:600}; export const PALETTE={};\n"}
    _write(tmp_path, files)
    new_main = a._repair_scene_registry(tmp_path, files, "phaser")["src/main.js"]
    assert "WORLD," in new_main and "PALETTE," in new_main  # full import kept
    assert "} from './config.js';" in new_main
    assert new_main.count("{") == new_main.count("}")
    assert "scene: [BootScene, GameScene]" in new_main


def test_repair_dedupes_colliding_identifiers(tmp_path):
    # Two filenames that sanitize to the same ident must not emit duplicate
    # `import gameover` / duplicate scene-array entries (invalid JS).
    a = _agent()
    files = {
        "src/main.js": _CLOBBERED_MAIN,
        "src/BootScene.js": _BOOT,
        "src/game-over.js": _OVER,
        "src/gameover.js": _OVER,
    }
    _write(tmp_path, files)
    new_main = a._repair_scene_registry(tmp_path, files, "phaser")["src/main.js"]
    # the two collide on 'gameover' -> one keeps it, the other gets a suffix
    import_idents = re.findall(r"import (\w+) from '\./game", new_main)
    assert len(import_idents) == len(set(import_idents))  # all unique
    assert new_main.count("import gameover from") <= 1


def test_compose_bails_on_unbalanced_result():
    # If composition would yield unbalanced JS (e.g. a malformed kept import with an
    # open brace), _compose_scene_main returns None so _repair_scene_registry leaves
    # the file for the fix loop and never ships broken JS.
    a = _agent()
    broken = (
        "import Phaser from 'phaser';\n"
        "import { Thing from './x.js';\n"  # malformed: missing closing brace
        "class GameScene extends Phaser.Scene {}\n"
        "const config = { scene: [GameScene] };\n"
        "new Phaser.Game(config);\n"
    )
    ordered = [("src/GameScene.js", "GameScene")]
    assert a._compose_scene_main(broken, ordered) is None


def test_repair_unterminated_config_falls_back_to_default(tmp_path):
    # An unparseable original config (no matching close brace) degrades to a clean
    # default config rather than bailing — still a valid, running entry.
    a = _agent()
    main = (
        "import Phaser from 'phaser';\n"
        "class GameScene extends Phaser.Scene {}\n"
        "const config = { width: 800, extra: { open:\n"  # never closes
        "new Phaser.Game(config);\n"
    )
    files = {"src/main.js": main, "src/BootScene.js": _BOOT, "src/GameScene.js": _GAME}
    _write(tmp_path, files)
    new_main = a._repair_scene_registry(tmp_path, files, "phaser")["src/main.js"]
    assert "scene: [BootScene, GameScene]" in new_main
    assert new_main.count("{") == new_main.count("}")  # valid default config used


import re  # noqa: E402  (used by the dedup test above)


def test_repair_real_build6_stub_if_present(tmp_path):
    """If the live build #6 artifact is still on disk, repair its actual broken
    stub against its real scene modules (the exact real-world clobber)."""
    build = os.path.expanduser(
        "~/Documents/Projects/a-polished-vertical-scrolling-space-shooter-with-6"
    )
    stub = os.path.join(build, "src", "main.js.stub-bak")
    if not os.path.exists(stub):
        return  # artifact cleaned up — skip silently (self-contained tests above cover it)
    src_dir = os.path.join(build, "src")
    files: dict[str, str] = {}
    for name in os.listdir(src_dir):
        if name.endswith(".js") and name != "main.js":
            with open(os.path.join(src_dir, name), encoding="utf-8") as fh:
                files[f"src/{name}"] = fh.read()
    with open(stub, encoding="utf-8") as fh:
        files["src/main.js"] = fh.read()

    assert CodeAgent._scene_registry_intact(files) is False
    _write(tmp_path, files)
    a = _agent()
    new_main = a._repair_scene_registry(tmp_path, files, "phaser")["src/main.js"]
    assert "import GameScene from './GameScene.js';" in new_main
    assert "new Phaser.Game(config)" in new_main
    assert a.metadata.get("scene_registry_repaired") is True
