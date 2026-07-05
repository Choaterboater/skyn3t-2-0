# Asset Foundry v1 Design

**Date:** 2026-07-05
**Status:** approved for spec
**Scope:** 2D Phaser game asset pipeline for full-game asset sets.

## Problem

The current Skyn3t game-art path can write role sprites, including a deterministic
offline fallback, but it is still role-level art. It does not solve complete game
asset coverage: directional movement, animation states, tilesets, UI, sound effects,
music loops, credits, license records, and proof that generated code only references
delivered files.

For higher-quality games, Skyn3t needs an asset pipeline, not just image generation.
The pipeline should ingest curated/local packs first, normalize them into a single
manifest, resolve full requirements from the game design, copy only the used files,
and leave generator APIs as pluggable gap-fillers.

## Goals

- Target 2D Phaser builds first.
- Define a canonical asset manifest for sprites, animations, tiles, UI images, sound
  effects, and music.
- Import local asset packs from disk without network access.
- Support multiple sources behind the same interface: local packs now; Kenney,
  OpenGameArt/LPC, CraftPix, Sonniss, Scenario, Layer, ElevenLabs, Meshy, and Tripo
  as later adapters.
- Map game design/art roles to complete asset requirements such as
  `player.walk.down`, `enemy.attack.left`, `coin.collect_sfx`, and `forest.tileset`.
- Copy only selected assets into generated projects under `public/assets/...`.
- Emit `assets.json`, `audio.json`, and `CREDITS.md`.
- Verify every referenced file exists and every non-CC0 attribution requirement is
  surfaced.

## Non-Goals

- Do not download paid or third-party packs in v1.
- Do not train custom models in v1.
- Do not implement Scenario/Layer/ElevenLabs API calls in v1.
- Do not solve Godot, 3D, or skeletal animation in v1.
- Do not require a Studio UI change in v1.
- Do not rewrite Phaser scaffolding beyond feeding it richer manifests/directives.

## Approach Options

1. **Recommended: manifest-first local pack ingestion.** Build the schema, local
   importer, resolver, copyout, credits, and QA gates before adding paid/generative
   integrations. This gives Skyn3t a stable asset contract and immediately supports
   user-provided packs.
2. **Generator-first integration.** Wire Scenario/Layer/ElevenLabs immediately. This
   may produce impressive one-off assets, but without a canonical manifest Skyn3t
   will still struggle with missing states, directions, credits, and reproducibility.
3. **One-pack hardcode.** Bundle or hardcode one known pack. This is fastest for a
   demo, but it creates a dead-end path and does not generalize across styles or
   licenses.

We will implement option 1.

## Architecture

### Core Data Model

Create `skyn3t/studio/asset_foundry.py` with pure data types and functions:

- `AssetItem`: one normalized asset record.
- `AssetRequirement`: one needed game asset slot.
- `AssetPlan`: selected assets plus missing gaps.
- `load_local_asset_pack(path) -> list[AssetItem]`
- `derive_asset_requirements(brief, art_plan, game_design) -> list[AssetRequirement]`
- `resolve_assets(requirements, catalog) -> AssetPlan`
- `write_asset_plan(project_dir, plan) -> dict`

The manifest must be JSON-safe and deterministic. IDs use slash-separated keys:

```text
sprite/player/idle/down
sprite/player/walk/right
sprite/enemy/death
tile/forest/ground
ui/button/primary
audio/player/jump
audio/coin/collect
music/level/loop
```

### Local Pack Layout

V1 supports an explicit local folder format:

```text
pack.json
sprites/
tiles/
ui/
audio/sfx/
audio/music/
```

`pack.json` describes source, license, credit text, style tags, and asset entries.
The importer validates paths are inside the pack directory and rejects unsafe paths.

### Resolver

The resolver ranks matches by:

1. exact role/state/direction/event;
2. matching role and compatible fallback state;
3. style/genre tag match;
4. generic fallback assets;
5. missing gap.

It never invents file references. Missing gaps are recorded so later generator
adapters can fill them.

### Build Output

The writer copies selected assets into:

```text
public/assets/sprites/
public/assets/tiles/
public/assets/ui/
public/assets/audio/sfx/
public/assets/audio/music/
```

It emits:

- `public/assets/assets.json` for images/sprites/tiles/UI;
- `public/assets/audio/audio.json` for sound and music;
- `CREDITS.md` for license/attribution records.

### Runner Integration

For Phaser stacks, `StudioRunner._generate_assets` will run Asset Foundry after the
art plan and game design are available. The returned manifests are threaded into
`extra` so codegen can load exact files and animation metadata. Existing role-sprite
generation remains as fallback/gap-fill behavior.

### Prompt Integration

`CodeAgent._game_art_directive` will be extended to tell codegen:

- use exact asset IDs and paths from `extra["asset_foundry"]`;
- configure animations from manifest frame metadata;
- load audio files listed in `audio.json`;
- never reference assets absent from the manifests;
- degrade gracefully when a requirement is missing.

### QA

Add deterministic checks before or during proof-run:

- every manifest path exists;
- every generated Phaser `load.image`, `load.spritesheet`, `load.atlas`, and
  `load.audio` path exists;
- every non-CC0 asset has a credit entry;
- missing gaps are surfaced as advisory or blocking depending on asset criticality.

## Source Adapter Roadmap

V1:

- local explicit pack importer;
- existing procedural role-sprite fallback;
- existing missing-image reconcile stays as last-resort repair.

V2:

- Kenney/OpenGameArt/LPC indexers;
- Sonniss/local audio indexer;
- source-specific license rules and credits.

V3:

- ElevenLabs SFX/music adapter;
- Scenario/Layer image workflow adapter;
- 3D adapters such as Meshy/Tripo for later non-Phaser stacks.

## Error Handling

- Invalid pack manifests fail that pack only and report diagnostics.
- Unsafe paths are rejected.
- Missing optional assets become gaps.
- Missing critical assets keep the build from claiming full-art coverage but do not
  crash the build pipeline.
- License ambiguity is treated as a gap unless the source is explicitly marked
  usable.

## Testing

Unit tests:

- local pack importer loads valid `pack.json`;
- importer rejects `../` path escapes;
- resolver chooses exact state/direction matches over generic fallbacks;
- writer copies files and emits `assets.json`, `audio.json`, and `CREDITS.md`;
- non-CC0 assets require credit output;
- missing required slots are reported as gaps.

Integration tests:

- Phaser runner threads `asset_foundry` into codegen `extra`;
- generated prompt includes exact manifest paths;
- proof-run asset scan fails on missing referenced audio/image paths.

## Rollout

1. Implement pure manifest/import/resolve/write modules with tests.
2. Add a tiny test fixture pack under `tests/fixtures/asset_packs/basic_phaser/`.
3. Wire Phaser runner and codegen prompt using the fixture in tests.
4. Add asset QA checks.
5. Later, add real source adapters once the core contract is stable.
