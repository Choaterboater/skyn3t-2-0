"""Asset-generation step — writes real images + assets.json into the project
when token+asset_gen are on, and is a degrade-only no-op otherwise. generate_images
is mocked (no network); the step itself is exercised end-to-end on disk.
"""

from __future__ import annotations

import json

from skyn3t.config.settings import Settings
from skyn3t.studio.assets import (
    _extract_subjects,
    _wants_images,
    asset_gen_enabled,
    generate_assets,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _FakeClient:
    """A ReplicateClient stand-in that returns one PNG per call, no network."""

    def __init__(self, available=True, images=None):
        self.available = available
        self._images = images if images is not None else [_PNG]
        self.calls = []

    async def generate_images(self, prompt, n=1, **kw):
        self.calls.append(prompt)
        return list(self._images)


def _settings(**kw):
    return Settings(**kw)


# ---- gating ----------------------------------------------------------------
def test_asset_gen_enabled_requires_token_and_flag():
    assert asset_gen_enabled(_settings(replicate_api_token="r8", asset_gen=True)) is True
    assert asset_gen_enabled(_settings(replicate_api_token="r8", asset_gen=False)) is False
    assert asset_gen_enabled(_settings(replicate_api_token="", asset_gen=True)) is False


def test_wants_images_signal():
    assert _wants_images("a kids coloring book app with animals") is True
    assert _wants_images("a REST API for invoices") is False


def test_extract_subjects_named_and_implied():
    assert "elephant" in _extract_subjects("color the elephant and the fox", 4)
    # Implied animals for a coloring/kids brief with no explicit nouns.
    implied = _extract_subjects("a coloring app for toddlers", 3)
    assert len(implied) == 3 and all(isinstance(s, str) for s in implied)


def test_extract_subjects_no_invented_defaults():
    """A brief that mentions images but names no subjects and isn't a
    coloring/kids brief must NOT invent coloring-book defaults (the old
    cat/dog/tree/flower leak). No nameable subject -> generate nothing."""
    # Real-world regression: a tool that *consumes* image/photo input.
    brief = "a tool that takes an image or photo as input and outputs a file"
    assert _extract_subjects(brief, 4) == []


# ---- end-to-end (mocked client) -------------------------------------------
async def test_writes_images_and_manifest(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    client = _FakeClient()
    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    res = await generate_assets(
        str(proj), "a kids coloring app with animals", settings=s,
        client=client, max_assets=3,
    )
    assert res["generated"] == 3
    assert res["skipped"] is False
    assets_dir = proj / "assets"
    pngs = sorted(p.name for p in assets_dir.glob("*.png"))
    assert len(pngs) == 3
    # Manifest written and matches what was reported.
    manifest = json.loads((assets_dir / "assets.json").read_text())
    assert {a["file"] for a in manifest} == {a["file"] for a in res["assets"]}
    assert all((proj / a["file"]).read_bytes() == _PNG for a in res["assets"])


async def test_stack_arg_routes_to_public_before_package_json(tmp_path):
    # Assets run BEFORE codegen, so package.json doesn't exist yet — the known stack
    # must still route images to public/ (else the code's /assets/... refs 404).
    proj = tmp_path / "proj"
    proj.mkdir()
    assert not (proj / "package.json").exists()
    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    res = await generate_assets(
        str(proj), "an HVAC company services website", settings=s,
        client=_FakeClient(), max_assets=2, stack="nextjs",
    )
    assert res["generated"] >= 1
    assert (proj / "public" / "assets").is_dir()
    for a in res["assets"]:
        assert a["file"].startswith("/assets/")
        assert (proj / "public" / a["file"].lstrip("/")).is_file()


async def test_web_stack_writes_images_to_public(tmp_path):
    # Regression: a Next.js/Vite app serves static files from public/ — writing to
    # ./assets/ made the code's /assets/... refs 404 on the live site. A JS framework
    # (package.json present) must place images under public/ and reference /assets/...
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "site"}')
    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    res = await generate_assets(
        str(proj), "an HVAC company services website", settings=s,
        client=_FakeClient(), max_assets=2,
    )
    assert res["generated"] >= 1
    assert (proj / "public" / "assets").is_dir()
    for a in res["assets"]:
        assert a["file"].startswith("/assets/")            # served URL, not a bare path
        assert (proj / "public" / a["file"].lstrip("/")).is_file()  # resolves from public/


async def test_noop_when_disabled(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    client = _FakeClient()
    s = _settings(replicate_api_token="r8_x", asset_gen=False)
    res = await generate_assets(str(proj), "coloring animals", settings=s, client=client)
    assert res["generated"] == 0 and res["skipped"] is True
    assert not (proj / "assets").exists()
    assert client.calls == []  # never called the client


async def test_noop_when_no_token(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    s = _settings(replicate_api_token="", asset_gen=True)
    res = await generate_assets(str(proj), "coloring animals", settings=s)
    assert res["generated"] == 0 and res["skipped"] is True
    assert not (proj / "assets").exists()


async def test_noop_for_non_image_brief(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    client = _FakeClient()
    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    res = await generate_assets(str(proj), "a CSV-to-JSON converter CLI", settings=s, client=client)
    assert res["generated"] == 0 and res["reason"] == "no_image_brief"
    assert client.calls == []


async def test_noop_when_no_nameable_subjects(tmp_path):
    """Image-mentioning brief with nothing to draw -> skip, never invent assets."""
    proj = tmp_path / "proj"
    proj.mkdir()
    client = _FakeClient()
    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    res = await generate_assets(
        str(proj),
        "a tool that takes an image or photo as input and outputs a file",
        settings=s, client=client,
    )
    assert res["generated"] == 0 and res["skipped"] is True
    assert res["reason"] == "no_subjects"
    assert client.calls == []  # never spent a prediction
    assert not (proj / "assets").exists()


async def test_generation_failure_yields_zero_not_raise(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()

    class _Boom(_FakeClient):
        async def generate_images(self, prompt, n=1, **kw):
            raise RuntimeError("replicate exploded")

    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    res = await generate_assets(str(proj), "coloring animals", settings=s, client=_Boom())
    assert res["generated"] == 0  # no crash; assets.json not written
    assert not (proj / "assets" / "assets.json").exists()


async def test_empty_images_yields_zero(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    client = _FakeClient(images=[])  # client returns nothing
    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    res = await generate_assets(str(proj), "coloring animals", settings=s, client=client)
    assert res["generated"] == 0 and res["skipped"] is True
