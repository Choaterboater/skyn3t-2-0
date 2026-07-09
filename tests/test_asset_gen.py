"""Asset-generation step — writes real images + assets.json into the project
when token+asset_gen are on, and is a degrade-only no-op otherwise. generate_images
is mocked (no network); the step itself is exercised end-to-end on disk.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import skyn3t.studio.assets as assets_mod
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.assets import (
    _extract_subjects,
    _wants_images,
    apply_web_asset_foundry,
    asset_gen_enabled,
    asset_subject_relevant,
    filter_assets_for_brief,
    generate_assets,
    generate_offline_web_assets,
)
from skyn3t.studio.planner import Planner
from skyn3t.studio.runner import StudioRunner

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


def test_business_site_subjects_are_domain_specific_not_hvac_default():
    subjects = _extract_subjects("a modern golf course website with tee times", 4)
    assert subjects
    assert all("hvac" not in s.lower() and "air conditioning" not in s.lower() for s in subjects)
    assert any("golf" in s.lower() or "clubhouse" in s.lower() for s in subjects)


def test_generic_business_site_does_not_invent_hvac_assets():
    assert _extract_subjects("a modern business website", 4) == []


def test_asset_subject_relevance_matches_business_domain():
    brief = "a modern golf course website with tee times"
    assert asset_subject_relevant(brief, "sunlit golf course fairway and green")
    assert not asset_subject_relevant(brief, "uniformed HVAC technician servicing an AC unit")


def test_filter_assets_for_brief_drops_wrong_domain_assets():
    assets = [
        {
            "subject": "sunlit golf course fairway and green",
            "file": "/assets/sunlit-golf-course.webp",
        },
        {
            "subject": "uniformed HVAC technician servicing an AC unit",
            "file": "/assets/uniformed-hvac-technician.webp",
        },
    ]
    filtered = filter_assets_for_brief("a golf website for adult beginners", assets)
    assert [a["subject"] for a in filtered] == ["sunlit golf course fairway and green"]


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


async def test_transient_empty_images_retry_and_recover(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()

    class _EmptyThenImage(_FakeClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def generate_images(self, prompt, n=1, **kw):
            self.calls.append(prompt)
            self.attempts += 1
            return [] if self.attempts == 1 else [_PNG]

    monkeypatch.setattr(assets_mod, "ASSET_PROVIDER_RETRY_DELAY", 0)
    client = _EmptyThenImage()
    s = _settings(replicate_api_token="r8_x", asset_gen=True)

    res = await generate_assets(
        str(proj), "an HVAC company website", settings=s, client=client, max_assets=1,
    )

    assert res["generated"] == 1 and res["skipped"] is False
    assert len(client.calls) == 2


async def test_transient_exception_retries_and_recovers(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()

    class _ExceptionThenImage(_FakeClient):
        async def generate_images(self, prompt, n=1, **kw):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                raise RuntimeError("temporary provider overload")
            return [_PNG]

    monkeypatch.setattr(assets_mod, "ASSET_PROVIDER_RETRY_DELAY", 0)
    client = _ExceptionThenImage()
    s = _settings(replicate_api_token="r8_x", asset_gen=True)

    res = await generate_assets(
        str(proj), "an HVAC company website", settings=s, client=client, max_assets=1,
    )

    assert res["generated"] == 1
    assert len(client.calls) == 2


async def test_concurrent_builds_share_provider_admission(tmp_path):
    class _TrackingClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def generate_images(self, prompt, n=1, **kw):
            self.calls.append(prompt)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return [_PNG]

    client = _TrackingClient()
    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    projects = [tmp_path / "one", tmp_path / "two"]
    for project in projects:
        project.mkdir()

    results = await asyncio.gather(*(
        generate_assets(
            str(project),
            "a golf website with course photos",
            settings=s,
            client=client,
            max_assets=2,
        )
        for project in projects
    ))

    assert [result["generated"] for result in results] == [2, 2]
    assert client.max_active == assets_mod.ASSET_PROVIDER_MAX_CONCURRENCY
    assert len(client.calls) == 4


async def test_repeated_empty_images_open_provider_circuit(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    client = _FakeClient(images=[])
    s = _settings(replicate_api_token="r8_x", asset_gen=True)
    monkeypatch.setattr(assets_mod, "ASSET_PROVIDER_RETRY_DELAY", 0)

    res = await generate_assets(
        str(proj),
        "a kids coloring app with animals",
        settings=s,
        client=client,
        max_assets=5,
    )

    assert res["generated"] == 0 and res["skipped"] is True
    assert len(client.calls) == (
        assets_mod.ASSET_PROVIDER_FAILURE_LIMIT
        * assets_mod.ASSET_PROVIDER_MAX_ATTEMPTS
    )


async def test_runner_asset_step_clears_stale_extra_assets(tmp_path):
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(replicate_api_token="", asset_gen=False),
    )
    manifest = SimpleNamespace(extra={})
    extra = {
        "assets": [
            {
                "subject": "uniformed HVAC technician",
                "file": "/assets/uniformed-hvac-technician.webp",
            }
        ],
        "model_override": "openrouter/test",
    }

    out = await runner._generate_assets(
        str(tmp_path),
        "a golf website for nervous adult beginners",
        manifest,
        extra,
        stack="nextjs",
    )

    assert "assets" not in out
    assert out["model_override"] == "openrouter/test"


async def test_runner_writes_offline_web_asset_foundry_for_ui_stack(tmp_path):
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(replicate_api_token="", asset_gen=False),
    )
    manifest = SimpleNamespace(extra={})

    out = await runner._generate_assets(
        str(tmp_path),
        "a modern golf course website with tee times",
        manifest,
        {},
        stack="nextjs",
    )

    foundry = manifest.extra["asset_foundry"]
    assert foundry["type"] == "web"
    assert foundry["source"] == "offline"
    assert set(foundry["selected"]) == {"web/hero", "web/og", "web/favicon"}
    assert (tmp_path / "public" / "assets" / "hero.png").is_file()
    assert (tmp_path / "public" / "assets" / "og.png").is_file()
    assert (tmp_path / "public" / "assets" / "favicon.png").is_file()
    assert out["asset_foundry"]["selected"]["web/hero"]["path"] == "/assets/hero.png"


def test_offline_web_assets_use_root_assets_for_static_html(tmp_path):
    foundry = generate_offline_web_assets(
        tmp_path,
        "a neighborhood bakery landing page",
        stack="static",
    )

    assert foundry["source"] == "offline"
    assert (tmp_path / "assets" / "hero.png").is_file()
    assert (tmp_path / "assets" / "favicon.png").is_file()
    assert not (tmp_path / "public" / "assets" / "hero.png").exists()
    assert foundry["selected"]["web/hero"]["path"] == "/assets/hero.png"


def test_apply_web_asset_foundry_wires_html_when_codegen_ignored_assets(tmp_path):
    foundry = generate_offline_web_assets(
        tmp_path,
        "a neighborhood bakery landing page",
        stack="static",
    )
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><title>Bakery</title></head>"
        "<body><main><h1>Bakery</h1></main></body></html>",
        encoding="utf-8",
    )

    out = apply_web_asset_foundry(tmp_path, foundry)

    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert out["changed"] is True
    assert out["hero_applied"] is True
    assert out["favicon_applied"] is True
    assert out["og_applied"] is True
    assert '<img src="/assets/hero.png"' in html
    assert '<link rel="icon" href="/assets/favicon.png">' in html
    assert '<meta property="og:image" content="/assets/og.png">' in html


async def test_runner_asset_step_honors_per_build_asset_gen_override(tmp_path, monkeypatch):
    captured = {}

    async def _fake_generate_assets(project_dir, brief, *, settings, stack):
        captured["asset_gen"] = settings.asset_gen
        captured["stack"] = stack
        return {
            "generated": 1,
            "skipped": False,
            "assets": [{"subject": "sunlit golf course fairway and green", "file": "/assets/golf.png"}],
        }

    monkeypatch.setattr("skyn3t.studio.assets.generate_assets", _fake_generate_assets)
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(replicate_api_token="r8_x", asset_gen=False),
    )
    manifest = SimpleNamespace(extra={})

    out = await runner._generate_assets(
        str(tmp_path),
        "a golf website for adult beginners",
        manifest,
        {"asset_gen": True},
        stack="nextjs",
    )

    assert captured == {"asset_gen": True, "stack": "nextjs"}
    assert manifest.extra["asset_gen_requested"] is True
    assert out["assets"][0]["subject"].startswith("sunlit golf")


def test_codegen_payload_prefers_current_worktree_asset_manifest(tmp_path):
    runner = StudioRunner(EventBus(), Orchestrator(EventBus()), settings=Settings())
    worktree = tmp_path / "wt"
    assets_dir = worktree / "public" / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "sunlit-golf-course.svg").write_text("<svg />")
    (assets_dir / "assets.json").write_text(json.dumps([
        {
            "subject": "sunlit golf course fairway and green",
            "file": "/assets/sunlit-golf-course.svg",
        }
    ]))
    plan = Planner().plan(
        "Build a golf website for adult beginners who never played",
        "golf",
        stack_hint="nextjs",
    )
    extra = {
        "assets": [
            {
                "subject": "uniformed HVAC technician",
                "file": "/assets/uniformed-hvac-technician.webp",
            }
        ]
    }

    payload = runner._base_payload(plan, str(tmp_path / "proj"), str(worktree), {}, [], extra)

    assets = payload["extra"]["assets"]
    assert assets == [
        {
            "subject": "sunlit golf course fairway and green",
            "file": "/assets/sunlit-golf-course.svg",
        }
    ]


def test_codegen_payload_filters_wrong_domain_current_asset_manifest(tmp_path):
    runner = StudioRunner(EventBus(), Orchestrator(EventBus()), settings=Settings())
    worktree = tmp_path / "wt"
    assets_dir = worktree / "public" / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "sunlit-golf-course.svg").write_text("<svg />")
    (assets_dir / "uniformed-hvac-technician.svg").write_text("<svg />")
    (assets_dir / "assets.json").write_text(json.dumps([
        {
            "subject": "sunlit golf course fairway and green",
            "file": "/assets/sunlit-golf-course.svg",
        },
        {
            "subject": "uniformed HVAC technician servicing an AC unit",
            "file": "/assets/uniformed-hvac-technician.svg",
        },
    ]))
    plan = Planner().plan(
        "Build a golf website for adult beginners who never played",
        "golf",
        stack_hint="nextjs",
    )

    payload = runner._base_payload(
        plan,
        str(tmp_path / "proj"),
        str(worktree),
        {},
        [],
        {"assets": [{"subject": "stale", "file": "/assets/stale.svg"}]},
    )

    assert payload["extra"]["assets"] == [
        {
            "subject": "sunlit golf course fairway and green",
            "file": "/assets/sunlit-golf-course.svg",
        }
    ]
