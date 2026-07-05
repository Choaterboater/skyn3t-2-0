from __future__ import annotations

import json
from pathlib import Path

from skyn3t.studio.asset_foundry import (
    AssetItem,
    AssetPlan,
    AssetRequirement,
    resolve_assets,
    write_asset_plan,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_resolve_prefers_exact_direction_over_generic_fallback():
    catalog = [
        AssetItem(
            "sprite/player/idle/down",
            "sprite",
            "sprites/player/idle/down.png",
            ("player", "idle", "down"),
            "CC0-1.0",
            "",
            source_path="/tmp/down.png",
        ),
        AssetItem(
            "sprite/player/idle",
            "sprite",
            "sprites/player/idle.png",
            ("player", "idle"),
            "CC0-1.0",
            "",
            source_path="/tmp/idle.png",
        ),
    ]
    plan = resolve_assets(
        [AssetRequirement("sprite/player/idle/down", "sprite", 10, ("player", "idle", "down"))],
        catalog,
    )
    assert list(plan.selected) == ["sprite/player/idle/down"]
    assert plan.missing == []


def test_write_asset_plan_emits_assets_audio_and_credits(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    sprite_src = source_root / "sprites/player/idle/down.png"
    sprite_src.parent.mkdir(parents=True, exist_ok=True)
    sprite_src.write_bytes(_PNG)
    audio_src = source_root / "audio/sfx/jump.wav"
    audio_src.parent.mkdir(parents=True, exist_ok=True)
    audio_src.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")

    plan = AssetPlan(
        selected={
            "sprite/player/idle/down": AssetItem(
                "sprite/player/idle/down",
                "sprite",
                "sprites/player/idle/down.png",
                ("player",),
                "CC0-1.0",
                "",
                source_path=str(sprite_src),
            ),
            "audio/player/jump": AssetItem(
                "audio/player/jump",
                "audio",
                "audio/sfx/jump.wav",
                ("jump",),
                "CC0-1.0",
                "",
                source_path=str(audio_src),
            ),
        }
    )
    out = write_asset_plan(tmp_path, plan)

    assert (tmp_path / "public/assets/assets.json").is_file()
    assert (tmp_path / "public/assets/audio/audio.json").is_file()
    assert (tmp_path / "CREDITS.md").is_file()
    assert (tmp_path / "public/assets/sprites/player/idle/down.png").is_file()
    assert (tmp_path / "public/assets/audio/sfx/jump.wav").is_file()

    assets = json.loads((tmp_path / "public/assets/assets.json").read_text())
    audio = json.loads((tmp_path / "public/assets/audio/audio.json").read_text())
    assert assets[0]["path"] == "/assets/sprites/player/idle/down.png"
    assert audio[0]["path"] == "/assets/audio/sfx/jump.wav"
    assert out["selected"]["sprite/player/idle/down"]["path"] == "player/idle/down.png"
