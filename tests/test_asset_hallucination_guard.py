"""Hallucinated-asset guard: the two-layer fix for the tower-defence-retry-2 no_go.

A real build (2026-07-01) shipped `this.load.image('gold', '/assets/sprites/gold.png')`
for a sprite the art tier never delivered (the GDD mentioned gold only as currency).
Phaser can't decode the 404 body -> uncaught runtime error -> qa_playtest (correctly)
failed the build. Layer 1: apply_deterministic_repairs synthesizes a valid placeholder
PNG for any referenced-but-missing image asset (do-no-harm: creates files, never edits
code). Layer 2: the game-art directive explicitly forbids loading asset paths beyond
the delivered role list, so the model stops inventing them in the first place.
"""
from __future__ import annotations

from pathlib import Path

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.studio.proof_run import apply_deterministic_repairs

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _phaser_tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "public" / "assets" / "sprites").mkdir(parents=True)
    (tmp_path / "public" / "assets" / "sprites" / "enemy.png").write_bytes(_PNG_MAGIC + b"x")
    (tmp_path / "src" / "main.js").write_text(
        "this.load.image('enemy', '/assets/sprites/enemy.png');\n"
        "this.load.image('gold', '/assets/sprites/gold.png');\n",
        encoding="utf-8",
    )
    return tmp_path


# ── layer 1: the deterministic-repairs wiring ────────────────────────────────

def test_deterministic_repairs_synthesize_missing_game_assets(tmp_path: Path):
    apply_deterministic_repairs(_phaser_tree(tmp_path), stack="phaser")
    gold = tmp_path / "public" / "assets" / "sprites" / "gold.png"
    assert gold.is_file() and gold.read_bytes()[:8] == _PNG_MAGIC


def test_deterministic_repairs_report_reconciled_assets(tmp_path: Path):
    out = apply_deterministic_repairs(_phaser_tree(tmp_path), stack="phaser")
    assert out["assets_reconciled"], "created placeholder must be reported"
    assert all("gold.png" in p for p in out["assets_reconciled"])


def test_non_game_stack_gets_no_asset_synthesis(tmp_path: Path):
    out = apply_deterministic_repairs(_phaser_tree(tmp_path), stack="nextjs")
    assert out["assets_reconciled"] == []
    assert not (tmp_path / "public" / "assets" / "sprites" / "gold.png").exists()


# ── layer 2: the directive clause ────────────────────────────────────────────

def test_sprite_genre_directive_forbids_inventing_asset_paths():
    text = CodeAgent._game_art_directive(
        "tower defence game with detail animation for sprites - multiple levels"
    )
    assert "ONLY textures that exist" in text
    assert "do NOT load any other /assets/" in text


def test_open_ended_directive_forbids_inventing_asset_paths():
    text = CodeAgent._game_art_directive(
        "a quirky underwater basket-weaving contest game"
    )
    assert "ONLY textures that exist" in text
    assert "do NOT load any other /assets/" in text
