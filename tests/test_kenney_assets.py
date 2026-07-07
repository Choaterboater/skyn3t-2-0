from __future__ import annotations

import json
from pathlib import Path

from skyn3t.agents.art_director import ArtPlan, RoleArt
from skyn3t.config.settings import Settings
from skyn3t.studio.assets import generate_role_sprites
from skyn3t.studio.asset_foundry import build_asset_foundry
from skyn3t.studio.kenney_assets import load_kenney_assets, write_kenney_role_sprites

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _StubClient:
    available = True

    def __init__(self):
        self.prompts: list[str] = []

    async def generate_images(self, prompt, n=1, *, model=None, timeout=None):
        self.prompts.append(prompt)
        return [_PNG]


def _fake_kenney_pack(root: Path) -> Path:
    pack = root / "asset_packs" / "kenney-game-assets-all-in-one-test"
    files = {
        "2D assets/Platformer Pack/PNG/Characters/player_idle.png": _PNG,
        "2D assets/Platformer Pack/PNG/Enemies/slime_walk.png": _PNG,
        "2D assets/Platformer Pack/PNG/Items/coin_gold.png": _PNG,
        "2D assets/Platformer Pack/PNG/Tiles/grass_center.png": _PNG,
        "UI assets/UI Pack/PNG/button_blue.png": _PNG,
        "Audio/Interface Sounds/Audio/click_001.wav": b"RIFF\x00\x00\x00\x00WAVEfmt ",
    }
    for rel, data in files.items():
        target = pack / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return pack


def _platformer_plan() -> ArtPlan:
    return ArtPlan(
        genre="platformer",
        theme="test",
        palette=("#102030", "#80d080"),
        roles={
            "player": RoleArt("player", "sprite", "hero player character", "", "player", "#80d080"),
            "enemy": RoleArt("enemy", "sprite", "slime enemy", "", "enemy", "#d08080"),
            "coin": RoleArt("coin", "sprite", "gold coin collectible", "", "coin", "#ffd700"),
            "platform": RoleArt("platform", "primitive", "grass platform", "", "platform", "#335533"),
        },
    )


def test_load_kenney_assets_classifies_pack_files(tmp_path: Path):
    pack = _fake_kenney_pack(tmp_path)

    items = load_kenney_assets(pack)

    assert len(items) == 6
    assert {item.kind for item in items} >= {"sprite", "tile", "ui", "audio"}
    player = next(item for item in items if "player" in item.tags)
    assert player.license == "CC0-1.0"
    assert player.credit == "Kenney"
    assert player.source_path.endswith("player_idle.png")


def test_write_kenney_role_sprites_copies_best_matches(tmp_path: Path):
    _fake_kenney_pack(tmp_path)
    plan = _platformer_plan()

    res = write_kenney_role_sprites(tmp_path / "project", plan, data_dir=tmp_path)

    sprites = tmp_path / "project" / "public" / "assets" / "sprites"
    assert res["source"] == "kenney"
    assert res["generated"] == 3
    assert res["role_map"] == {
        "player": "/assets/sprites/player.png",
        "enemy": "/assets/sprites/enemy.png",
        "coin": "/assets/sprites/coin.png",
    }
    assert (sprites / "player.png").read_bytes() == _PNG
    manifest = json.loads((sprites / "assets.json").read_text())
    assert manifest["source"] == "kenney"
    assert manifest["sources"]


def test_write_kenney_role_sprites_prefers_distinct_sources(tmp_path: Path):
    pack = _fake_kenney_pack(tmp_path)
    for name in ("enemy_red.png", "enemy_blue.png", "enemy_green.png"):
        target = pack / "2D assets" / "Space Shooter Pack" / "PNG" / "Enemies" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_PNG + name.encode())
    plan = ArtPlan(
        genre="space_shooter",
        theme="test",
        palette=("#000000", "#ffffff"),
        roles={
            "enemy": RoleArt("enemy", "sprite", "enemy alien", "", "enemy", "#ffffff"),
            "interceptor": RoleArt("interceptor", "sprite", "fast enemy alien", "", "interceptor", "#ffffff"),
            "gunship": RoleArt("gunship", "sprite", "heavy enemy alien", "", "gunship", "#ffffff"),
        },
    )

    res = write_kenney_role_sprites(tmp_path / "project", plan, data_dir=tmp_path)

    sources = [meta["source_path"] for meta in res["selected"].values()]
    assert len(sources) == 3
    assert len(set(sources)) == 3


async def test_generate_role_sprites_uses_kenney_without_replicate(tmp_path: Path):
    _fake_kenney_pack(tmp_path)
    client = _StubClient()

    res = await generate_role_sprites(
        str(tmp_path / "project"),
        "a platformer",
        settings=Settings(llm_backend="stub", game_art_source="kenney", data_dir=tmp_path),
        client=client,
        art_plan=_platformer_plan(),
    )

    assert res["source"] == "kenney"
    assert res["generated"] == 3
    assert client.prompts == []


async def test_auto_source_prefers_installed_kenney_pack(tmp_path: Path):
    _fake_kenney_pack(tmp_path)
    client = _StubClient()

    res = await generate_role_sprites(
        str(tmp_path / "project"),
        "a platformer",
        settings=Settings(
            llm_backend="stub",
            game_art_source="auto",
            replicate_api_token="tok",
            data_dir=tmp_path,
        ),
        client=client,
        art_plan=_platformer_plan(),
    )

    assert res["source"] == "kenney"
    assert res["generated"] == 3
    assert client.prompts == []


def test_asset_foundry_uses_kenney_pack_roots(tmp_path: Path):
    pack = _fake_kenney_pack(tmp_path)
    project = tmp_path / "project"

    foundry = build_asset_foundry(
        project,
        "a platformer",
        art_plan=_platformer_plan(),
        asset_pack_roots=[pack.parent],
    )

    assert foundry["catalog_size"] >= 6
    assert str(pack) in foundry["sources"]
    assert "sprite/player/idle/down" in foundry["selected"]
    assert "audio/ui/click" in foundry["selected"]
