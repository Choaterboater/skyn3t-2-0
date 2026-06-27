"""Replicate role-sprite generation (roadmap #6, Phase 1).

``generate_role_sprites`` turns the resolver's per-role plan into one generated
sprite per game role at build time, written to ``public/assets/sprites/{role}.png``
with a ``role_map`` manifest. Gated by ``game_art_enabled`` + ``game_art_source``.
Never raises — a per-role failure OMITS that role (the scaffold falls back to a
colored primitive), never a gap, never a crash. Stubbed client = no network.
"""

from __future__ import annotations

import json

from skyn3t.config.settings import Settings
from skyn3t.studio.asset_resolver import ROLES
from skyn3t.studio.assets import generate_role_sprites

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16  # magic header is all _ext_for reads


class _StubClient:
    available = True

    def __init__(self, fail_substr: str | None = None):
        self.fail_substr = fail_substr
        self.prompts: list[str] = []

    async def generate_images(self, prompt, n=1, *, model=None, timeout=None):
        self.prompts.append(prompt)
        if self.fail_substr and self.fail_substr in prompt:
            return []
        return [_PNG]


def _settings(**kw):
    base = dict(llm_backend="stub", replicate_api_token="tok", game_art_source="replicate")
    base.update(kw)
    return Settings(**base)


async def test_generates_a_png_per_role(tmp_path):
    client = _StubClient()
    res = await generate_role_sprites(
        str(tmp_path), "a space shooter", settings=_settings(), client=client, seed=1
    )
    assert res["source"] == "replicate"
    assert res["generated"] == len(ROLES)
    sprites = tmp_path / "public" / "assets" / "sprites"
    for role in ROLES:
        assert (sprites / f"{role}.png").is_file()
        assert res["role_map"][role] == f"/assets/sprites/{role}.png"


async def test_per_role_failure_omits_that_role_not_a_gap(tmp_path):
    # The coin's themed subject ("coin") is in its prompt; fail only that one.
    client = _StubClient(fail_substr="coin")
    res = await generate_role_sprites(
        str(tmp_path), "a fun arcade game", settings=_settings(), client=client, seed=0
    )
    assert "coin" not in res["role_map"], "a failed role must be omitted, not crash"
    assert "player" in res["role_map"], "other roles still generate"
    assert not (tmp_path / "public/assets/sprites/coin.png").exists()


async def test_offline_source_skips_generation(tmp_path):
    client = _StubClient()
    res = await generate_role_sprites(
        str(tmp_path), "a game", settings=_settings(game_art_source="offline"), client=client
    )
    assert res["skipped"] is True
    assert res["source"] == "offline"
    assert res["role_map"] == {}
    assert client.prompts == []  # no predictions spent on the offline floor


async def test_disabled_skips(tmp_path):
    client = _StubClient()
    res = await generate_role_sprites(
        str(tmp_path), "a game", settings=_settings(game_art_enabled=False), client=client
    )
    assert res["skipped"] is True
    assert client.prompts == []


async def test_auto_without_token_uses_offline(tmp_path):
    client = _StubClient()
    s = Settings(llm_backend="stub", game_art_source="auto", replicate_api_token="")
    res = await generate_role_sprites(str(tmp_path), "a game", settings=s, client=client)
    assert res["skipped"] is True
    assert res["source"] == "offline"
    assert client.prompts == []


async def test_writes_a_role_map_manifest(tmp_path):
    res = await generate_role_sprites(
        str(tmp_path), "a space game", settings=_settings(), client=_StubClient(), seed=2
    )
    mf = tmp_path / "public" / "assets" / "sprites" / "assets.json"
    assert mf.is_file()
    data = json.loads(mf.read_text())
    assert data["role_map"]["player"].endswith("player.png")
    assert data["source"] == "replicate"


# ---- runner wiring: role sprites run for game stacks, not others ----
def _runner(**kw):
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub", **kw))


class _Manifest:
    def __init__(self):
        self.build_id = "b"
        self.extra: dict = {}


async def test_runner_wires_role_sprites_for_game_stack(tmp_path):
    # offline source -> no network; the wiring still records the (skipped) result,
    # proving _generate_assets invokes generate_role_sprites for a game stack.
    runner = _runner(game_art_source="offline")
    m = _Manifest()
    await runner._generate_assets(str(tmp_path), "a space game", m, {}, stack="phaser")
    assert "role_sprites" in m.extra
    assert m.extra["role_sprites"]["source"] == "offline"


async def test_runner_skips_role_sprites_for_non_game_stack(tmp_path):
    runner = _runner(game_art_source="offline")
    m = _Manifest()
    await runner._generate_assets(str(tmp_path), "a marketing site", m, {}, stack="nextjs")
    assert "role_sprites" not in m.extra


def test_sprite_model_is_an_official_replicate_model():
    # Verified live: the community model fofr/sticker-maker returns 0 images via
    # the official-models endpoint the adapter uses. The sprite model MUST be an
    # OFFICIAL Replicate model (bare owner/name works; community needs a version).
    from skyn3t.studio.assets import _SPRITE_ROLE_MODEL

    assert _SPRITE_ROLE_MODEL.startswith("black-forest-labs/"), _SPRITE_ROLE_MODEL
