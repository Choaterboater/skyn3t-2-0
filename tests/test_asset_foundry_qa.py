from __future__ import annotations

import json
from pathlib import Path

from skyn3t.studio.asset_foundry import check_asset_outputs
from skyn3t.studio.qa_playtest import build_verdict


def test_check_asset_outputs_reports_missing_path_and_credit(tmp_path: Path):
    assets_dir = tmp_path / "public" / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "assets.json").write_text(
        json.dumps(
            [
                {
                    "asset_id": "sprite/player/idle/down",
                    "kind": "sprite",
                    "path": "/assets/sprites/player/idle/down.png",
                    "license": "MIT",
                    "credit": "",
                }
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (assets_dir / "audio" / "audio.json").parent.mkdir(parents=True, exist_ok=True)
    (assets_dir / "audio" / "audio.json").write_text("[]\n", encoding="utf-8")
    (tmp_path / "CREDITS.md").write_text("# Credits\n\n", encoding="utf-8")

    out = check_asset_outputs(tmp_path)

    assert out["missing_paths"] == ["/assets/sprites/player/idle/down.png"]
    assert out["missing_credits"] == ["sprite/player/idle/down"]


def test_check_asset_outputs_accepts_served_public_asset_paths(tmp_path: Path):
    assets_dir = tmp_path / "public" / "assets"
    sprite = assets_dir / "sprites" / "player" / "idle" / "down.png"
    sprite.parent.mkdir(parents=True)
    sprite.write_bytes(b"png")
    (assets_dir / "assets.json").write_text(
        json.dumps(
            [
                {
                    "asset_id": "sprite/player/idle/down",
                    "kind": "sprite",
                    "path": "/assets/sprites/player/idle/down.png",
                    "license": "CC0",
                    "credit": "",
                }
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    out = check_asset_outputs(tmp_path)

    assert out["missing_paths"] == []


def test_build_verdict_includes_manifest_gaps(tmp_path: Path):
    assets_dir = tmp_path / "public" / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "assets.json").write_text(
        json.dumps(
            [
                {
                    "asset_id": "sprite/player/idle/down",
                    "kind": "sprite",
                    "path": "/assets/sprites/player/idle/down.png",
                    "license": "MIT",
                    "credit": "",
                }
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (assets_dir / "audio" / "audio.json").parent.mkdir(parents=True, exist_ok=True)
    (assets_dir / "audio" / "audio.json").write_text("[]\n", encoding="utf-8")
    verdict = build_verdict([], tmp_path)
    assert verdict.missing_asset_paths == ["/assets/sprites/player/idle/down.png"]
    assert verdict.missing_credit_assets == ["sprite/player/idle/down"]
    assert verdict.ok is False
    assert any("missing" in gap.lower() for gap in verdict.gaps())
