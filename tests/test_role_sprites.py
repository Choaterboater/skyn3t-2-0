"""Replicate role-sprite generation (roadmap #6, Phase 1).

``generate_role_sprites`` turns the resolver's per-role plan into one generated
sprite per game role at build time, written to ``public/assets/sprites/{role}.png``
with a ``role_map`` manifest. Gated by ``game_art_enabled`` + ``game_art_source``.
Never raises — a per-role failure OMITS that role (the scaffold falls back to a
colored primitive), never a gap, never a crash. Stubbed client = no network.
"""

from __future__ import annotations

import json

from skyn3t.agents.art_director import direct_art
from skyn3t.config.settings import Settings
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


async def test_generates_a_png_per_sprite_role(tmp_path):
    # Genre-aware: only the genre's SPRITE roles are generated; its PRIMITIVE roles
    # (laser-likes, backdrops) cost nothing and never get a file.
    brief = "a space shooter with aliens"
    client = _StubClient()
    res = await generate_role_sprites(
        str(tmp_path), brief, settings=_settings(), client=client, seed=1
    )
    plan = direct_art(brief)
    sprite_roles = set(plan.sprite_roles())
    assert sprite_roles, "this genre has sprite roles"
    assert res["source"] == "replicate"
    assert res["generated"] == len(sprite_roles)
    sprites = tmp_path / "public" / "assets" / "sprites"
    for role in sprite_roles:
        assert (sprites / f"{role}.png").is_file()
        assert res["role_map"][role] == f"/assets/sprites/{role}.png"
    for role in plan.primitive_roles():
        assert role not in res["role_map"]
        assert not (sprites / f"{role}.png").exists()


async def test_geometric_game_spends_zero(tmp_path):
    # The $0 win: a geometric game is all primitives -> not a single generation.
    client = _StubClient()
    res = await generate_role_sprites(
        str(tmp_path), "a brick breaker", settings=_settings(), client=client, seed=0
    )
    assert res["generated"] == 0
    assert res["role_map"] == {}
    assert client.prompts == [], "a geometric game must not spend a generation"


async def test_manifest_records_genre_and_palette(tmp_path):
    res = await generate_role_sprites(
        str(tmp_path), "a space shooter", settings=_settings(), client=_StubClient(), seed=1
    )
    assert res["genre"] == "space_shooter"
    assert res["palette"] and all(c.startswith("#") for c in res["palette"])


async def test_generator_uses_threaded_art_plan(tmp_path):
    # A pre-computed (e.g. LLM-tailored) plan overrides the brief-derived one, so its
    # OWN sprite roles get generated — the runner threads one plan to both consumers.
    from skyn3t.agents.art_director import ArtPlan, RoleArt

    plan = ArtPlan(
        genre="fishing", theme="llm", palette=("#0a2a3a", "#7ec8e3", "#f4d35e"),
        roles={
            "boat": RoleArt("boat", "sprite", "fishing boat", "a boat sprite", "boat_llm", "#7ec8e3"),
            "water": RoleArt("water", "primitive", "water", "", "water_llm", "#0a2a3a"),
        },
    )
    res = await generate_role_sprites(
        str(tmp_path), "unused brief", settings=_settings(), client=_StubClient(), art_plan=plan
    )
    assert "boat" in res["role_map"], "the threaded plan's sprite role is generated"
    assert "water" not in res["role_map"], "its primitive role is not generated"
    assert res["genre"] == "fishing"


async def test_per_role_failure_omits_that_role_not_a_gap(tmp_path):
    # The coin's themed subject ("coin") is in its prompt; fail only that one.
    client = _StubClient(fail_substr="coin")
    res = await generate_role_sprites(
        str(tmp_path), "a fun arcade game", settings=_settings(), client=client, seed=0
    )
    assert "coin" not in res["role_map"], "a failed role must be omitted, not crash"
    assert "player" in res["role_map"], "other roles still generate"
    assert not (tmp_path / "public/assets/sprites/coin.png").exists()


async def test_offline_source_writes_local_role_sprites(tmp_path):
    brief = "a space shooter with aliens"
    client = _StubClient()
    res = await generate_role_sprites(
        str(tmp_path), brief, settings=_settings(game_art_source="offline"), client=client
    )
    plan = direct_art(brief)
    sprites = tmp_path / "public" / "assets" / "sprites"

    assert res["skipped"] is False
    assert res["source"] == "offline"
    assert res["generated"] == len(plan.sprite_roles())
    assert res["role_map"]
    assert client.prompts == []  # no predictions spent on the offline floor
    for role in plan.sprite_roles():
        path = sprites / f"{role}.png"
        assert path.is_file()
        assert path.read_bytes().startswith(_PNG[:8])
        assert res["role_map"][role] == f"/assets/sprites/{role}.png"
    manifest = json.loads((sprites / "assets.json").read_text())
    assert manifest["source"] == "offline"
    assert manifest["role_map"] == res["role_map"]


async def test_disabled_skips(tmp_path):
    client = _StubClient()
    res = await generate_role_sprites(
        str(tmp_path), "a game", settings=_settings(game_art_enabled=False), client=client
    )
    assert res["skipped"] is True
    assert client.prompts == []


async def test_auto_without_token_uses_offline_assets(tmp_path):
    client = _StubClient()
    s = Settings(llm_backend="stub", game_art_source="auto", replicate_api_token="")
    res = await generate_role_sprites(str(tmp_path), "a space shooter", settings=s, client=client)
    assert res["source"] == "offline"
    assert res["generated"] > 0
    assert res["role_map"]
    assert client.prompts == []


async def test_writes_a_role_map_manifest(tmp_path):
    brief = "a space game"
    res = await generate_role_sprites(
        str(tmp_path), brief, settings=_settings(), client=_StubClient(), seed=2
    )
    mf = tmp_path / "public" / "assets" / "sprites" / "assets.json"
    assert mf.is_file()
    data = json.loads(mf.read_text())
    a_sprite = next(iter(direct_art(brief).sprite_roles()))
    assert data["role_map"][a_sprite].endswith(f"{a_sprite}.png")
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
    # offline source -> no network; the wiring records real local role sprites,
    # proving _generate_assets invokes generate_role_sprites for a game stack.
    runner = _runner(game_art_source="offline")
    m = _Manifest()
    await runner._generate_assets(str(tmp_path), "a space game", m, {}, stack="phaser")
    assert "role_sprites" in m.extra
    assert m.extra["role_sprites"]["source"] == "offline"
    assert m.extra["role_sprites"]["generated"] > 0
    assert m.extra["role_sprites"]["role_map"]


async def test_runner_skips_role_sprites_for_non_game_stack(tmp_path):
    runner = _runner(game_art_source="offline")
    m = _Manifest()
    await runner._generate_assets(str(tmp_path), "a marketing site", m, {}, stack="nextjs")
    assert "role_sprites" not in m.extra


async def test_runner_threads_art_plan_into_extra_for_game_stack(tmp_path):
    # The runner computes the art plan ONCE and threads its serialized form on the
    # returned extra, so codegen's directive uses the SAME plan the generator did.
    runner = _runner(game_art_source="offline")
    m = _Manifest()
    out = await runner._generate_assets(str(tmp_path), "a space game", m, {}, stack="phaser")
    assert isinstance(out.get("art_plan"), dict)
    assert out["art_plan"].get("roles"), "the threaded plan carries roles"


async def test_runner_does_not_thread_art_plan_for_non_game_stack(tmp_path):
    runner = _runner(game_art_source="offline")
    m = _Manifest()
    out = await runner._generate_assets(str(tmp_path), "a marketing site", m, {}, stack="nextjs")
    assert "art_plan" not in out
    assert "game_design" not in out


async def test_runner_threads_game_design_for_game_stack(tmp_path):
    # The GDD (depth spec) is threaded to codegen like the art plan.
    runner = _runner(game_art_source="offline")
    m = _Manifest()
    out = await runner._generate_assets(str(tmp_path), "a space game", m, {}, stack="phaser")
    assert isinstance(out.get("game_design"), dict)
    assert out["game_design"].get("powerups"), "the threaded GDD carries power-ups"


def test_sprite_model_is_an_official_replicate_model():
    # Verified live: community models (fofr/sticker-maker) return 404 on the
    # official-models endpoint the adapter uses. The sprite model MUST be official.
    # retro-diffusion/* and black-forest-labs/* are live-verified is_official=True.
    from skyn3t.studio.assets import _SPRITE_ROLE_MODEL

    owner = _SPRITE_ROLE_MODEL.split("/")[0]
    assert owner in {"retro-diffusion", "black-forest-labs"}, _SPRITE_ROLE_MODEL
