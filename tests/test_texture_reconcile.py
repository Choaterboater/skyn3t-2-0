"""Deterministic texture-key reconciliation (the model-independent "sprites render
as green boxes" fix): every texture key a render call USES must be LOADED. For each
used-but-unloaded key we synthesize a placeholder PNG and inject the matching
load.image into preload — anchoring on an existing loader call. Do-no-harm: game
stacks only, insert-only, bail on computed/dynamic keys, never raise."""
from __future__ import annotations

from pathlib import Path

from skyn3t.studio.texture_reconcile import reconcile_texture_keys


def _game(tmp_path: Path, scene_src: str) -> Path:
    proj = tmp_path / "game"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "Scene.js").write_text(scene_src, encoding="utf-8")
    return proj


_SCENE_INVENTED_KEY = """\
class Play extends Phaser.Scene {
  preload() {
    this.load.image('enemy', 'assets/sprites/enemy.png');
  }
  create() {
    // renders with an invented key that preload never loaded -> green box
    this.add.sprite(100, 100, 'monster');
  }
}
"""


def test_used_but_unloaded_key_gets_placeholder_and_injected_load(tmp_path):
    proj = _game(tmp_path, _SCENE_INVENTED_KEY)
    out = reconcile_texture_keys(proj, stack="phaser")

    assert out["textures_loaded_added"] == ["monster"]
    # placeholder synthesized at the conventional sprite path (under public/).
    assert "public/assets/sprites/monster.png" in out["placeholders_created"]
    assert (proj / "public" / "assets" / "sprites" / "monster.png").is_file()
    # the loader was injected into preload, matching the existing receiver.
    src = (proj / "src" / "Scene.js").read_text(encoding="utf-8")
    assert "this.load.image('monster', 'assets/sprites/monster.png');" in src
    # the original enemy load and the render call are untouched (insert-only).
    assert "this.load.image('enemy', 'assets/sprites/enemy.png');" in src
    assert "this.add.sprite(100, 100, 'monster');" in src


def test_reconcile_is_idempotent(tmp_path):
    proj = _game(tmp_path, _SCENE_INVENTED_KEY)
    reconcile_texture_keys(proj, stack="phaser")
    # second pass: 'monster' is now loaded, so nothing more to do.
    out2 = reconcile_texture_keys(proj, stack="phaser")
    assert out2["textures_loaded_added"] == []
    # exactly one injected loader line (no duplicate).
    src = (proj / "src" / "Scene.js").read_text(encoding="utf-8")
    assert src.count("this.load.image('monster'") == 1


def test_all_used_keys_already_loaded_is_noop(tmp_path):
    proj = _game(tmp_path, """\
class Play extends Phaser.Scene {
  preload() { this.load.image('hero', 'assets/sprites/hero.png'); }
  create() { this.add.sprite(10, 10, 'hero'); }
}
""")
    before = (proj / "src" / "Scene.js").read_text(encoding="utf-8")
    out = reconcile_texture_keys(proj, stack="phaser")
    assert out["textures_loaded_added"] == []
    assert (proj / "src" / "Scene.js").read_text(encoding="utf-8") == before


def test_setTexture_key_is_reconciled(tmp_path):
    proj = _game(tmp_path, """\
class Play extends Phaser.Scene {
  preload() { this.load.image('hero', 'assets/sprites/hero.png'); }
  create() { const s = this.add.sprite(0, 0, 'hero'); s.setTexture('powerup'); }
}
""")
    out = reconcile_texture_keys(proj, stack="phaser")
    assert "powerup" in out["textures_loaded_added"]


def test_dynamic_keyed_game_is_skipped_untouched(tmp_path):
    # A loop-load + computed-key render: a "used literal" may be loaded dynamically,
    # so we must NOT conclude it's unloaded. Bail, touch nothing.
    proj = _game(tmp_path, """\
class Play extends Phaser.Scene {
  preload() { for (const k of this.list) this.load.image(k, `assets/sprites/${k}.png`); }
  create() { let key; key = 'enemy'; this.add.sprite(0, 0, key); }
}
""")
    before = (proj / "src" / "Scene.js").read_text(encoding="utf-8")
    out = reconcile_texture_keys(proj, stack="phaser")
    assert out["skipped_dynamic"] is True
    assert out["textures_loaded_added"] == []
    assert (proj / "src" / "Scene.js").read_text(encoding="utf-8") == before
    assert not (proj / "public").exists()


def test_non_game_stack_is_noop(tmp_path):
    proj = _game(tmp_path, _SCENE_INVENTED_KEY)
    out = reconcile_texture_keys(proj, stack="react")
    assert out == {"textures_loaded_added": [], "placeholders_created": [], "skipped_dynamic": False}


def test_no_loader_to_anchor_on_does_not_inject(tmp_path):
    # A used key but no existing load call anywhere: we can't guess a preload
    # location, so nothing is injected (the game loads nothing by design).
    proj = _game(tmp_path, """\
class Play extends Phaser.Scene {
  create() { this.add.sprite(0, 0, 'ghost'); }
}
""")
    out = reconcile_texture_keys(proj, stack="phaser")
    assert out["textures_loaded_added"] == []
    src = (proj / "src" / "Scene.js").read_text(encoding="utf-8")
    assert "load.image" not in src
