from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.studio.asset_foundry import load_local_asset_pack

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _write_pack(root: Path, pack: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.json").write_text(json.dumps(pack, indent=2) + "\n", encoding="utf-8")
    for entry in pack.get("assets", []):
        rel = entry["path"]
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".png"):
            target.write_bytes(_PNG)
        else:
            target.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    return root


def test_load_local_asset_pack_reads_pack_json(tmp_path: Path):
    pack_root = _write_pack(
        tmp_path / "basic",
        {
            "name": "basic",
            "source": "local",
            "license": "CC0-1.0",
            "credit": "",
            "assets": [
                {
                    "id": "sprite/player/idle/down",
                    "kind": "sprite",
                    "path": "sprites/player/idle/down.png",
                    "tags": ["player", "idle", "down"],
                },
                {
                    "id": "audio/player/jump",
                    "kind": "audio",
                    "path": "audio/sfx/jump.wav",
                    "tags": ["jump"],
                },
            ],
        },
    )

    items = load_local_asset_pack(pack_root)

    assert [item.asset_id for item in items] == [
        "sprite/player/idle/down",
        "audio/player/jump",
    ]
    assert items[0].kind == "sprite"
    assert items[1].kind == "audio"
    assert items[0].source_path.endswith("sprites/player/idle/down.png")


def test_load_local_asset_pack_rejects_escape_path(tmp_path: Path):
    pack_root = _write_pack(
        tmp_path / "basic",
        {
            "name": "basic",
            "source": "local",
            "license": "CC0-1.0",
            "assets": [
                {
                    "id": "sprite/player/idle/down",
                    "kind": "sprite",
                    "path": "../secret.png",
                    "tags": ["player"],
                },
            ],
        },
    )

    with pytest.raises(ValueError, match="outside the pack directory"):
        load_local_asset_pack(pack_root)
