# Asset Foundry v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a 2D Phaser asset pipeline that imports local packs, resolves full asset requirements, copies only used files, and emits manifests plus credits.

**Architecture:** Add a pure Asset Foundry core module for pack import, requirement derivation, resolution, and manifest writing. Wire it into the Phaser runner and codegen prompt so the generated game loads exact asset paths and audio references from the manifest, then backstop it with deterministic QA checks.

**Tech Stack:** Python 3.11+, pytest, existing Skyn3t Phaser runner/codegen pipeline, existing manifest/QA conventions.

## Global Constraints

- Target 2D Phaser builds first.
- Do not download paid or third-party packs in v1.
- Do not train custom models in v1.
- Do not implement Scenario/Layer/ElevenLabs API calls in v1.
- Do not solve Godot, 3D, or skeletal animation in v1.
- Do not require a Studio UI change in v1.
- Do not rewrite Phaser scaffolding beyond feeding it richer manifests/directives.

---

### Task 1: Asset Foundry Core And Local Pack Importer

**Files:**
- Create: `skyn3t/studio/asset_foundry.py`
- Create: `tests/test_asset_foundry.py`
- Create: `tests/fixtures/asset_packs/basic_phaser/pack.json`
- Create: `tests/fixtures/asset_packs/basic_phaser/sprites/player/idle/down.png`
- Create: `tests/fixtures/asset_packs/basic_phaser/sprites/player/walk/down/frame-0.png`
- Create: `tests/fixtures/asset_packs/basic_phaser/audio/sfx/jump.wav`
- Create: `tests/fixtures/asset_packs/basic_phaser/audio/music/loop.ogg`
- Create: `tests/fixtures/asset_packs/basic_phaser/tiles/forest/ground.png`

**Interfaces:**
- Consumes: raw local pack directory plus the existing `ArtPlan` and game design payloads.
- Produces: `AssetItem`, `AssetRequirement`, `AssetPlan`, `load_local_asset_pack()`, and `derive_asset_requirements()`.

- [ ] **Step 1: Write the failing importer tests**

Add tests that pin the local pack contract and path safety:

```python
from pathlib import Path

from skyn3t.studio.asset_foundry import load_local_asset_pack


def test_load_local_asset_pack_reads_pack_json(tmp_path: Path):
    pack = tmp_path / "basic"
    pack.mkdir()
    (pack / "pack.json").write_text(
        """
{
  "name": "basic",
  "source": "local",
  "license": "CC0-1.0",
  "credit": "",
  "assets": [
    {"id": "sprite/player/idle/down", "kind": "sprite", "path": "sprites/player/idle/down.png", "tags": ["player", "idle", "down"]},
    {"id": "audio/player/jump", "kind": "audio", "path": "audio/sfx/jump.wav", "tags": ["jump"]}
  ]
]
        """.strip()
    )
    items = load_local_asset_pack(pack)
    assert [item.asset_id for item in items] == ["sprite/player/idle/down", "audio/player/jump"]
    assert items[0].kind == "sprite"
    assert items[1].kind == "audio"


def test_load_local_asset_pack_rejects_escape_path(tmp_path: Path):
    pack = tmp_path / "basic"
    pack.mkdir()
    (pack / "pack.json").write_text(
        """
{
  "name": "basic",
  "source": "local",
  "license": "CC0-1.0",
  "assets": [
    {"id": "sprite/player/idle/down", "kind": "sprite", "path": "../secret.png", "tags": ["player"]}
  ]
}
        """.strip()
    )
    with pytest.raises(ValueError, match="outside the pack directory"):
        load_local_asset_pack(pack)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_asset_foundry.py::test_load_local_asset_pack_reads_pack_json -q
```

Expected: FAIL because `skyn3t/studio/asset_foundry.py` does not exist yet.

- [ ] **Step 3: Write minimal importer and dataclasses**

Create `skyn3t/studio/asset_foundry.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AssetItem:
    asset_id: str
    kind: str
    path: str
    tags: tuple[str, ...] = ()
    license: str = ""
    credit: str = ""


@dataclass(frozen=True, slots=True)
class AssetRequirement:
    asset_id: str
    kind: str
    priority: int
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class AssetPlan:
    selected: dict[str, AssetItem] = field(default_factory=dict)
    missing: list[AssetRequirement] = field(default_factory=list)
```

Implement `load_local_asset_pack(path)` to read `pack.json`, validate relative paths stay inside the pack, and return a list of `AssetItem`.

Implement `derive_asset_requirements(brief, art_plan, game_design)` so it returns concrete asset IDs such as `sprite/player/idle/down`, `sprite/player/walk/down`, `audio/player/jump`, and `music/level/loop` for Phaser-style games.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_asset_foundry.py::test_load_local_asset_pack_reads_pack_json tests/test_asset_foundry.py::test_load_local_asset_pack_rejects_escape_path -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/asset_foundry.py tests/test_asset_foundry.py tests/fixtures/asset_packs/basic_phaser/pack.json tests/fixtures/asset_packs/basic_phaser/sprites/player/idle/down.png tests/fixtures/asset_packs/basic_phaser/sprites/player/walk/down/frame-0.png tests/fixtures/asset_packs/basic_phaser/audio/sfx/jump.wav tests/fixtures/asset_packs/basic_phaser/audio/music/loop.ogg tests/fixtures/asset_packs/basic_phaser/tiles/forest/ground.png
git commit -m "feat: add asset foundry core importer"
```

---

### Task 2: Resolver, Writer, And Credits

**Files:**
- Modify: `skyn3t/studio/asset_foundry.py`
- Create: `tests/test_asset_foundry_resolver.py`

**Interfaces:**
- Consumes: `AssetRequirement` and `AssetItem`.
- Produces: `resolve_assets(requirements, catalog) -> AssetPlan` and `write_asset_plan(project_dir, plan) -> dict`.

- [ ] **Step 1: Write failing resolver/writer tests**

Add tests that verify the precedence rules and emitted outputs:

```python
def test_resolve_prefers_exact_direction_over_generic_fallback():
    catalog = [
        AssetItem("sprite/player/idle/down", "sprite", "sprites/player/idle/down.png", ("player", "idle", "down")),
        AssetItem("sprite/player/idle", "sprite", "sprites/player/idle.png", ("player", "idle")),
    ]
    plan = resolve_assets([AssetRequirement("sprite/player/idle/down", "sprite", 10, ("player", "down"))], catalog)
    assert list(plan.selected) == ["sprite/player/idle/down"]
    assert plan.missing == []


def test_write_asset_plan_emits_assets_audio_and_credits(tmp_path: Path):
    plan = AssetPlan(
        selected={
            "sprite/player/idle/down": AssetItem("sprite/player/idle/down", "sprite", "sprites/player/idle/down.png", ("player",)),
            "audio/player/jump": AssetItem("audio/player/jump", "audio", "audio/sfx/jump.wav", ("jump",), "CC0-1.0", ""),
        }
    )
    out = write_asset_plan(tmp_path, plan)
    assert (tmp_path / "public/assets/assets.json").is_file()
    assert (tmp_path / "public/assets/audio/audio.json").is_file()
    assert (tmp_path / "CREDITS.md").is_file()
    assert out["selected"]["sprite/player/idle/down"]["path"].endswith("down.png")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_asset_foundry_resolver.py -q
```

Expected: FAIL because resolution and writer behavior are not implemented yet.

- [ ] **Step 3: Implement resolver and writer**

In `skyn3t/studio/asset_foundry.py`, add:

```python
def resolve_assets(requirements: list[AssetRequirement], catalog: list[AssetItem]) -> AssetPlan:
    ...


def write_asset_plan(project_dir: str | Path, plan: AssetPlan) -> dict[str, Any]:
    ...
```

Keep the resolver deterministic and never invent file paths. The writer should copy selected files into `public/assets/sprites/`, `public/assets/tiles/`, `public/assets/ui/`, `public/assets/audio/sfx/`, and `public/assets/audio/music/`, then emit `assets.json`, `audio.json`, and `CREDITS.md`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_asset_foundry_resolver.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/asset_foundry.py tests/test_asset_foundry_resolver.py
git commit -m "feat: resolve and write asset plans"
```

---

### Task 3: Phaser Runner And Prompt Wiring

**Files:**
- Modify: `skyn3t/studio/runner.py`
- Modify: `skyn3t/agents/code_agent.py`
- Modify: `tests/test_role_sprites.py`
- Modify: `tests/test_game_art_directive.py`

**Interfaces:**
- Consumes: `AssetPlan`, generated manifests, and `extra["asset_foundry"]`.
- Produces: game builds that load exact asset and audio files from the manifests.

- [ ] **Step 1: Write failing integration tests**

Add a runner test that proves the Asset Foundry data is threaded into `extra`, and a prompt test that proves the directive names exact manifest paths:

```python
async def test_runner_threads_asset_foundry_into_extra(tmp_path):
    runner = _runner(game_art_source="offline")
    manifest = _Manifest()
    out = await runner._generate_assets(str(tmp_path), "a platformer", manifest, {}, stack="phaser")
    assert "asset_foundry" in out
    assert out["asset_foundry"]["selected"]


def test_game_art_directive_mentions_asset_foundry_paths():
    prompt = _agent()._agentic_prompt("a platformer", "phaser", _plan(), "", extra={"asset_foundry": {"selected": {"sprite/player/idle/down": {"path": "sprites/player/idle/down.png"}}}})
    assert "sprites/player/idle/down.png" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_role_sprites.py::test_runner_threads_asset_foundry_into_extra tests/test_game_art_directive.py::test_game_art_directive_mentions_asset_foundry_paths -q
```

Expected: FAIL because the runner and directive do not know about `asset_foundry` yet.

- [ ] **Step 3: Wire the runner and directive**

Update `StudioRunner._generate_assets` to run the new Asset Foundry after art/game design are available, then thread the result into `extra["asset_foundry"]`.

Update `CodeAgent._game_art_directive` so the generated game code:

- loads exact asset paths from the manifest;
- uses animation metadata from the manifest when present;
- loads audio files from `audio.json`;
- never invents asset paths outside the manifest.

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_role_sprites.py tests/test_game_art_directive.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/runner.py skyn3t/agents/code_agent.py tests/test_role_sprites.py tests/test_game_art_directive.py
git commit -m "feat: thread asset foundry into game builds"
```

---

### Task 4: QA Gates And Fixture Coverage

**Files:**
- Modify: `skyn3t/studio/qa_playtest.py`
- Modify: `skyn3t/studio/asset_reconcile.py`
- Create: `tests/test_asset_foundry_qa.py`
- Create: `tests/fixtures/asset_packs/basic_phaser/README.md`

**Interfaces:**
- Consumes: emitted manifests plus generated game source.
- Produces: deterministic validation that asset references exist and credits are emitted.

- [ ] **Step 1: Write failing QA tests**

Add tests that verify missing references are surfaced and that credit output is required for non-CC0 assets:

```python
def test_qa_scan_flags_missing_audio_reference(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text("this.load.audio('jump', 'audio/sfx/jump.wav');")
    rendered, missing = check_assets_referenced(tmp_path)
    assert rendered is False
    assert "audio/sfx/jump.wav" in missing


def test_credits_written_for_non_cc0_assets(tmp_path: Path):
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_asset_foundry_qa.py -q
```

Expected: FAIL because Asset Foundry QA hooks are not present yet.

- [ ] **Step 3: Implement QA hooks**

Expand the existing asset/audio reconcile and QA scan so Phaser builds fail when they reference files absent from `assets.json` / `audio.json`, and so missing credits are reported when the source license requires attribution.

- [ ] **Step 4: Run full focused verification**

Run:

```bash
pytest tests/test_asset_foundry.py tests/test_asset_foundry_resolver.py tests/test_asset_foundry_qa.py tests/test_role_sprites.py tests/test_game_art_directive.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/qa_playtest.py skyn3t/studio/asset_reconcile.py tests/test_asset_foundry_qa.py tests/fixtures/asset_packs/basic_phaser/README.md
git commit -m "feat: add asset foundry qa gates"
```
