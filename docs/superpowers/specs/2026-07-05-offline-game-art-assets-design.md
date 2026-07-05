# Offline Game Art Assets Design

**Date:** 2026-07-05
**Status:** approved for implementation
**Scope:** Fill the current `game_art_source=offline` gap with deterministic local role sprites.

## Problem

Skyn3t's game pipeline already has a role-aware art planner and a Replicate-backed sprite generator. The intended design says game builds should have a `$0` offline floor, but the current offline path returns an empty `role_map`; generated Phaser games then fall back to primitives unless paid sprite generation is enabled.

That is acceptable for geometric games like Pong or Breakout, but it is too weak for character/object games like platformers, space shooters, and tower defense. Those builds should get visible sprite files without requiring a Replicate token or a paid asset pack.

## Goals

- Make `game_art_source=offline` write real local PNG sprites for every sprite role in the current `ArtPlan`.
- Preserve the existing `role_map` contract: `role -> /assets/sprites/<role>.png`.
- Keep the build deterministic, offline, and network-free.
- Keep generated sprites small, valid PNGs, and safe to commit.
- Leave `game_art_source=replicate` as the premium/higher-fidelity path.
- Preserve primitive-only behavior for geometric games whose art plan has no sprite roles.

## Non-Goals

- Do not download Kenney packs or add a dependency on external asset hosting in this pass.
- Do not add Studio UI controls in this pass.
- Do not add animation sheets, sound effects, tilemaps, procedural levels, or backgrounds.
- Do not change the Phaser sim contract or headless invariant gate.

## Approach Options

1. **Recommended: deterministic local fallback sprites.** Generate simple transparent PNGs from the role plan and bundled palette using Pillow. This immediately fixes the offline gap and keeps builds reproducible.
2. **Kenney pack importer first.** Add a pack/index system that consumes a local Kenney bundle. This gives better art, but it introduces licensing/vendor storage decisions and does not help until the pack is present.
3. **Replicate-only premium art.** Keep the current behavior and rely on paid generation. This is the least code, but it contradicts the `$0` default requirement.

We will implement option 1 and structure it so a future Kenney importer can replace or augment the local fallback behind the same `role_map` interface.

## Architecture

Add a focused offline sprite module under `skyn3t/studio/offline_sprites.py`. It will expose one public function:

```python
def write_offline_role_sprites(project_dir: str | Path, art_plan: ArtPlan) -> dict[str, Any]:
    ...
```

The function will:

- inspect `art_plan.sprite_roles()`;
- create `public/assets/sprites/`;
- generate one transparent PNG per sprite role;
- write `public/assets/sprites/assets.json`;
- return the same manifest shape used by `generate_role_sprites`: `generated`, `skipped`, `reason`, `source`, `genre`, `palette`, and `role_map`.

`skyn3t/studio/assets.py::generate_role_sprites` will delegate to this module when `_role_art_source(settings)` resolves to `offline`, including `auto` without a Replicate token. `disabled` will remain a skip with no files.

## Sprite Generation

The first-pass sprites are intentionally simple and deterministic, not a replacement for curated art. Each role gets a transparent PNG with:

- a palette-derived primary/accent color;
- a role-distinct silhouette selected from a small built-in shape set;
- a short role label drawn with Pillow's default font;
- a stable variation derived from the role name and art plan genre.

The assets are generated into each build output, not pre-generated into source control. This avoids storing many binary variants while still producing real image files for every build.

## Data Flow

```text
brief + stack=phaser
  -> runner computes ArtPlan
  -> generate_role_sprites(...)
     -> _role_art_source(settings)
        -> replicate: existing Replicate path
        -> offline: write_offline_role_sprites(...)
        -> disabled: no files
  -> manifest.extra["role_sprites"]
  -> extra["art_plan"] for codegen
  -> generated game loads /assets/sprites/<role>.png
```

## Error Handling

- Missing or invalid sprite roles return a skipped manifest with `reason="no_sprite_roles"`.
- Directory or file write failures are caught and returned as `reason="write_failed"` or `reason="mkdir_failed"`.
- A per-role generation failure omits that role but keeps the rest.
- No exception from the offline path may break a build.

## Testing

Tests will be added before implementation:

- `game_art_source=offline` writes PNG files and a manifest for sprite roles.
- `game_art_source=auto` without a Replicate token uses the offline writer and does not call the client.
- geometric primitive-only games still generate zero files.
- `game_art_enabled=False` remains disabled and writes no files.
- the runner records non-empty offline `role_sprites` for a game stack.

Focused verification:

```bash
pytest tests/test_role_sprites.py -q
pytest tests/test_game_art_directive.py -q
```

Broader verification, if the focused tests pass:

```bash
pytest tests/test_asset_gen.py tests/test_role_sprites.py tests/test_game_art_directive.py -q
```
