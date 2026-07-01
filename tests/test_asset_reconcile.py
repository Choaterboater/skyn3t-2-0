"""Tests for the hallucinated-asset reconciler (``asset_reconcile``).

The load-bearing story: a real Phaser build no_go'd because generated code called
``this.load.image('gold', '/assets/sprites/gold.png')`` while the art tier only
delivered tower/enemy/brute/flyer/boss. Phaser can't process the 404 body -> an
uncaught runtime error -> qa_playtest (correctly) fails the build. The reconciler
synthesizes a valid placeholder PNG for each hallucinated IMAGE ref so the game
boots, and reports missing AUDIO refs (never faking silent audio) for the
fix-loop. It CREATES files, never edits code, and never raises.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image

from skyn3t.studio.asset_reconcile import reconcile_asset_refs

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_phaser_project(tmp_path: Path, main_js: str, existing_sprites=()) -> Path:
    """A minimal Phaser project: src/main.js + any already-delivered sprites under
    public/assets/sprites/ (so a public/ dir exists, mirroring the art tier)."""
    proj = tmp_path / "proj"
    _write(proj / "src" / "main.js", main_js)
    # A Vite/Phaser project always ships a public/ dir (index.html + static assets
    # live there) — create it so the reconciler resolves urls under public/, as it
    # does in production, even when no sprites are pre-seeded.
    (proj / "public").mkdir(parents=True, exist_ok=True)
    for name in existing_sprites:
        p = proj / "public" / "assets" / "sprites" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        # a tiny but valid PNG so the file genuinely exists on disk
        Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(p, "PNG")
    return proj


def test_gold_reproduction_creates_exactly_the_missing_sprite(tmp_path: Path):
    """The exact production failure: 5 delivered sprites + 1 hallucinated ``gold``."""
    main_js = """
    export default class Boot extends Phaser.Scene {
      preload() {
        this.load.image('tower', '/assets/sprites/tower.png');
        this.load.image('enemy', '/assets/sprites/enemy.png');
        this.load.image('brute', '/assets/sprites/brute.png');
        this.load.image('flyer', '/assets/sprites/flyer.png');
        this.load.image('boss',  '/assets/sprites/boss.png');
        this.load.image('gold',  '/assets/sprites/gold.png');
      }
    }
    """
    proj = _make_phaser_project(
        tmp_path, main_js,
        existing_sprites=("tower.png", "enemy.png", "brute.png", "flyer.png", "boss.png"),
    )
    result = reconcile_asset_refs(proj, stack="phaser")

    assert result["refs_scanned"] == 6
    assert result["images_created"] == ["public/assets/sprites/gold.png"]
    assert result["missing_audio"] == []

    created = proj / "public" / "assets" / "sprites" / "gold.png"
    assert created.is_file()
    data = created.read_bytes()
    assert data[:8] == _PNG_MAGIC
    im = Image.open(io.BytesIO(data))
    assert im.size == (256, 256)  # matches the art tier's 256x256 sprite convention
    assert im.mode == "RGBA"
    # The 5 already-delivered sprites are untouched (still their tiny 8x8 selves).
    assert Image.open(proj / "public" / "assets" / "sprites" / "tower.png").size == (8, 8)


def test_relative_and_root_relative_urls_both_handled(tmp_path: Path):
    main_js = """
      this.load.image('a', '/assets/a.png');
      this.load.image('b', 'assets/b.png');
    """
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj, stack="phaser")

    assert result["refs_scanned"] == 2
    assert sorted(result["images_created"]) == [
        "public/assets/a.png",
        "public/assets/b.png",
    ]
    assert (proj / "public" / "assets" / "a.png").is_file()
    assert (proj / "public" / "assets" / "b.png").is_file()


def test_spritesheet_call_is_reconciled(tmp_path: Path):
    main_js = (
        "this.load.spritesheet('run', '/assets/sprites/run.png', "
        "{ frameWidth: 32, frameHeight: 32 });"
    )
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj, stack="phaser")

    assert result["images_created"] == ["public/assets/sprites/run.png"]
    assert (proj / "public" / "assets" / "sprites" / "run.png").is_file()


def test_double_and_single_quotes_both_match(tmp_path: Path):
    main_js = """
      this.load.image("dq", "/assets/dq.png");
      this.load.image('sq', '/assets/sq.png');
    """
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj, stack="phaser")
    assert sorted(result["images_created"]) == ["public/assets/dq.png", "public/assets/sq.png"]


def test_path_escape_attempt_is_skipped(tmp_path: Path):
    """A traversal url must never write outside the project root, and must not be
    counted as an asset ref (it doesn't point under assets/ once normalized)."""
    main_js = "this.load.image('evil', '/assets/../../evil.png');"
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj, stack="phaser")

    assert result["images_created"] == []
    # nothing written above the project root
    assert not (tmp_path / "evil.png").exists()
    assert not (proj.parent / "evil.png").exists()
    assert not (proj / "evil.png").exists()


def test_non_game_stack_is_a_noop(tmp_path: Path):
    """A missing ref in a non-game stack must NOT touch disk."""
    main_js = "this.load.image('gold', '/assets/sprites/gold.png');"
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj, stack="nextjs")

    assert result == {"images_created": [], "missing_audio": [], "refs_scanned": 0}
    assert not (proj / "public" / "assets" / "sprites" / "gold.png").exists()


def test_empty_stack_is_a_noop(tmp_path: Path):
    main_js = "this.load.image('gold', '/assets/sprites/gold.png');"
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj)  # default stack=""
    assert result == {"images_created": [], "missing_audio": [], "refs_scanned": 0}
    assert not (proj / "public" / "assets" / "sprites" / "gold.png").exists()


def test_missing_audio_is_reported_not_synthesized(tmp_path: Path):
    main_js = "this.load.audio('theme', '/assets/audio/theme.mp3');"
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj, stack="phaser")

    assert result["images_created"] == []
    assert result["missing_audio"] == ["/assets/audio/theme.mp3"]
    # silent fake audio is worse than none — nothing is written
    assert not (proj / "public" / "assets" / "audio" / "theme.mp3").exists()


def test_present_asset_is_not_recreated(tmp_path: Path):
    main_js = "this.load.image('tower', '/assets/sprites/tower.png');"
    proj = _make_phaser_project(tmp_path, main_js, existing_sprites=("tower.png",))
    result = reconcile_asset_refs(proj, stack="phaser")
    assert result["images_created"] == []
    assert result["refs_scanned"] == 1


def test_idempotent_second_run_creates_nothing(tmp_path: Path):
    main_js = "this.load.image('gold', '/assets/sprites/gold.png');"
    proj = _make_phaser_project(tmp_path, main_js)

    first = reconcile_asset_refs(proj, stack="phaser")
    assert first["images_created"] == ["public/assets/sprites/gold.png"]

    second = reconcile_asset_refs(proj, stack="phaser")
    assert second["images_created"] == []
    assert second["refs_scanned"] == 1


def test_duplicate_ref_creates_one_file(tmp_path: Path):
    main_js = """
      this.load.image('gold', '/assets/sprites/gold.png');
      this.load.image('gold', '/assets/sprites/gold.png');
    """
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj, stack="phaser")
    assert result["images_created"] == ["public/assets/sprites/gold.png"]


def test_no_public_dir_writes_under_project_root(tmp_path: Path):
    """When the project has no public/ dir, assets resolve relative to the root."""
    proj = tmp_path / "proj"
    _write(proj / "src" / "main.js", "this.load.image('g', '/assets/g.png');")
    result = reconcile_asset_refs(proj, stack="phaser")
    assert result["images_created"] == ["assets/g.png"]
    assert (proj / "assets" / "g.png").is_file()


def test_placeholder_color_is_deterministic_per_key(tmp_path: Path):
    """Same key -> byte-identical placeholder (stable hash, not salted hash())."""
    def _bytes_for(root_name: str) -> bytes:
        proj = tmp_path / root_name
        _write(proj / "src" / "main.js", "this.load.image('gold', '/assets/gold.png');")
        reconcile_asset_refs(proj, stack="phaser")
        return (proj / "assets" / "gold.png").read_bytes()

    assert _bytes_for("p1") == _bytes_for("p2")


def test_different_keys_get_different_colors(tmp_path: Path):
    main_js = """
      this.load.image('alpha', '/assets/alpha.png');
      this.load.image('omega', '/assets/omega.png');
    """
    proj = _make_phaser_project(tmp_path, main_js)
    reconcile_asset_refs(proj, stack="phaser")
    a = (proj / "public" / "assets" / "alpha.png").read_bytes()
    o = (proj / "public" / "assets" / "omega.png").read_bytes()
    assert a != o


def test_never_raises_on_missing_src(tmp_path: Path):
    proj = tmp_path / "proj"  # no src/, no files at all
    proj.mkdir()
    result = reconcile_asset_refs(proj, stack="phaser")
    assert result == {"images_created": [], "missing_audio": [], "refs_scanned": 0}


def test_never_raises_on_unreadable_src_file(tmp_path: Path):
    """An unreadable source file is skipped; the readable ones still reconcile."""
    proj = tmp_path / "proj"  # no public/ dir -> assets resolve under the root
    _write(proj / "src" / "good.js", "this.load.image('g', '/assets/g.png');")
    bad = proj / "src" / "bad.js"
    _write(bad, "this.load.image('b', '/assets/b.png');")
    os.chmod(bad, 0o000)
    try:
        if os.access(bad, os.R_OK):  # running as root — chmod won't block; skip
            pytest.skip("cannot make a file unreadable as this user")
        result = reconcile_asset_refs(proj, stack="phaser")
    finally:
        os.chmod(bad, 0o644)
    # the good file still produced its asset; the unreadable one was silently skipped
    assert "assets/g.png" in result["images_created"]


def test_non_asset_and_external_urls_ignored(tmp_path: Path):
    """URLs not under assets/ (a CDN, a bare filename) aren't ours to synthesize."""
    main_js = """
      this.load.image('cdn', 'https://cdn.example.com/x.png');
      this.load.image('bare', 'logo.png');
    """
    proj = _make_phaser_project(tmp_path, main_js)
    result = reconcile_asset_refs(proj, stack="phaser")
    assert result["images_created"] == []
    assert result["refs_scanned"] == 0
