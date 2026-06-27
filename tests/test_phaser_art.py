"""Phaser scaffold art rendering (roadmap #6, Phase 1).

With art ON the scaffold must preload role sprites and render them with a
colored-primitive FALLBACK (a missing/failed sprite never breaks the game). With
art OFF the scaffold is byte-identical to today's primitive-only game. Crucially
``src/sim.js`` is byte-identical either way — the headless gate runs the SAME pure
logic; art is a rendering-only concern, so the seal stays intact.
"""

from __future__ import annotations

from skyn3t.agents._scaffold import scaffold_for


def test_art_off_is_the_default_and_primitive_only():
    main = scaffold_for("phaser", "dino", "a dino runner")["src/main.js"]
    assert "this.add.rectangle" in main
    assert "preload()" not in main
    assert "this.load.image" not in main


def test_art_on_emits_preload_and_sprite_with_fallback():
    main = scaffold_for("phaser", "dino", "a dino runner", art=True)["src/main.js"]
    assert "preload()" in main
    assert "this.load.image('player'" in main
    assert "this.load.image('coin'" in main
    # render uses the sprite WHEN present, else falls back to a primitive
    assert "this.textures.exists('player')" in main
    assert "this.add.sprite(0, 0, 'player')" in main
    assert "this.add.rectangle" in main  # fallback retained


def test_sim_js_is_byte_identical_regardless_of_art():
    off = scaffold_for("phaser", "dino", "a dino runner")
    on = scaffold_for("phaser", "dino", "a dino runner", art=True)
    assert on["src/sim.js"] == off["src/sim.js"], "art must not touch the pure sim core"


def test_art_on_still_drives_the_pure_sim():
    # Art changes rendering ONLY — the scene still imports and steps the pure sim.
    main = scaffold_for("phaser", "dino", "a dino runner", art=True)["src/main.js"]
    assert "from './sim.js'" in main
    assert "step(this.state" in main


def test_art_off_main_js_unchanged_from_primitive_baseline():
    # art=False must reproduce the exact primitive create() (no behavior drift).
    main = scaffold_for("phaser", "dino", "a dino runner")["src/main.js"]
    assert "this.playerView = this.add.rectangle(0, 0, 36, 36, 0x4ade80)" in main
    assert "this.coinView = this.add.circle(0, 0, 12, 0xfbbf24)" in main
