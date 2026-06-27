# Game Art Tier (roadmap #6) — design

**Date:** 2026-06-27
**Status:** approved, building Phase 1
**Depends on:** [[phaser-game-stack]] (#4), [[headless-invariant-gate]] (#5, sealed via PR #23)

## Context

Generated Phaser games currently render **only colored primitives** — the `_phaser`
scaffold draws a green rectangle (player) and a gold circle (coin) and has no
sprite preloading at all. That is the single thing between "gray rectangles" and
"looks like a game." This is roadmap item #6, the **art tier**.

The whole point is to make a generated game *look* like a game by default, at
**$0 runtime cost**: real art with no network and no API key. AI-generated sprites
(Replicate) already exist but are online, opt-in, off by default, and produce
*subject* images (a lion), not *game-role* sprites (player / enemy / coin). The art
tier adds an offline, deterministic baseline and a role→sprite resolver, and demotes
paid AI sprites to an explicit premium upgrade.

**Cost model (why this is mostly free):**
- Offline bundled CC0 sprites + DiceBear (offline npm SVG) → **$0**, deterministic.
- Art-director agent → one *cheap* LLM call per build, flag-gated (cents).
- Premium AI sprites (Replicate pixel-art-xl / Retro-Diffusion) → the only paid path,
  **demoted to opt-in, off by default**.

## Invariant we must preserve

The headless invariant gate (#5) runs the game's **pure `src/sim.js`** in Node. Art
is a *rendering* concern only: the sim core stays logic-only and untouched; only the
Phaser scene's `create()`/`draw()` and a new `preload()` change. A missing/broken
sprite must **never** break the game (always fall back to a colored primitive) and
must never affect the sim. This keeps the gate load-bearing.

## Key reframe — the foundation is the PLUMBING, not the pixels

Replicate already *generates* images at build time (`assets.py::generate_assets`).
The actual gaps are source-agnostic: (1) Replicate makes **subject** images (a lion),
not **game-role** sprites (player/enemy/coin); (2) the `_phaser` scaffold can't
**preload or render a sprite at all** — it hardcodes rectangles, so generated images
sit unused. Build the role→sprite plumbing **once**, and the *source* becomes a
selectable setting. So we do NOT lead with a bundled set; we lead with the plumbing
and wire it to the Replicate you already have, with a bundled CC0 set as the $0
fallback.

Reused as-is: `skyn3t/studio/assets.py` (Replicate flow, `assets.json` manifest),
`skyn3t/adapters/replicate.py`, `skyn3t/studio/design_tokens.py`, the `_phaser`
pure-sim split.

### Phase 1 — Role→sprite plumbing + selectable source (this PR)

1. **`skyn3t/studio/asset_resolver.py`** (pure, deterministic) — the heart of the
   tier. `plan_roles(brief, *, seed) -> dict[role, RolePlan]` decides the roles a game
   needs (`player`, `enemy`, `coin`, `platform`, `projectile`, `background`) and, for
   each, a brief-themed description (e.g. `space`→"alien fighter", `fantasy`→"knight").
   Brief keywords + a seeded index make it deterministic. This plan drives BOTH
   sources: it becomes the Replicate prompt per role, and the bundled-variant key.

2. **Selectable source** via a new setting `game_art_source: "offline" | "replicate"
   | "auto"` (default `auto`), GUI-configurable like other settings (no env/hardcode):
   - `replicate` → `assets.py` generates a **role sprite** per role at build time
     using the role plan as the prompt (game-sprite style: transparent bg, clean,
     centered), via the existing `ReplicateClient`. ~4–6 images/build.
   - `offline` → resolve each role to a **bundled CC0 sprite** (see #3). $0, offline.
   - `auto` → `replicate` if `replicate_available` (token set), else `offline`.
   Either way the output is the same `role_map` the scaffold consumes — sources are
   interchangeable behind the resolver.

3. **Bundled CC0 fallback set** under `skyn3t/data/sprites/`, produced by a committed
   deterministic generator `scripts/gen_sprites.py` (Pillow) with a CC0 `LICENSE`.
   Minimal: one clean sprite per role × a few palette variants. This is the
   guaranteed floor (used by `offline`, and by `auto`/`replicate` on any failure).

4. **`_phaser` scaffold (`_scaffold.py`)** — when a role map is present: emit
   `preload()` that `this.load.image(role, path)` per role, and render
   `this.add.sprite(...)` per role **with a colored-primitive fallback**
   (`this.textures.exists(role) ? sprite : rectangle`). `src/sim.js` is **unchanged**.

5. **Manifest + runner wiring** — extend `assets.json` with `source` and `role_map`
   (`role -> file`). For game stacks the runner runs the resolver, obtains sprites
   (generate or bundled per `game_art_source`), writes them to
   `public/assets/sprites/`, and records the role map. Architect directive gains one
   line: render the role sprites with primitive fallback.

6. **Settings:** `game_art_enabled: bool = True` (on; the offline path is free) and
   `game_art_source: str = "auto"`, both surfaced in the settings UI.

### Phase 2 — Art-director agent + DiceBear (next PR)

`skyn3t/agents/art_director.py`: an LLM agent (flag-gated `art_director_enabled =
False`) that reads brief + GDD to refine the role plan (art style, cohesion,
per-role themes), and can request **DiceBear** characters (offline `@dicebear` npm in
the generated game) for player/enemy/NPC. Feeds the same `role_map`.

### Phase 3 — Premium fidelity (next PR)

Add premium sprite models (pixel-art-xl / Retro-Diffusion) as `game_art_source`
options / quality tiers, routed through the same resolver. Opt-in.

## Data flow (Phase 1)

```
brief + stack=phaser + settings.game_art_source
  └─ resolver.plan_roles(brief, seed)              # asset_resolver.py (pure): roles + themed prompts
       └─ source switch:
            replicate -> assets.py generates a role sprite per role (build time)
            offline   -> map each role to a bundled CC0 sprite
            (any failure -> bundled fallback)
       └─ write sprites -> public/assets/sprites/ + role_map -> assets.json
            └─ _phaser scaffold: preload() + sprite render (primitive fallback)
                 └─ headless gate runs src/sim.js (UNCHANGED) -> still load-bearing
```

## Error handling

- Resolver never raises; an unknown/unmapped role → omitted → scaffold falls back to a
  primitive. A missing sprite file → `textures.exists` guard → primitive.
- `game_art_enabled = False` → scaffold emits today's colored-primitive code verbatim
  (zero behavior change), so the feature is reversible by a flag.

## Testing

- **`tests/test_asset_resolver.py`** — `plan_roles` determinism (same brief+seed → same
  plan), covers the core roles, brief-keyword theming (space/fantasy/default), and
  never-raises on empty/odd briefs. Source switch: `offline` resolves every role to an
  existing bundled file; `auto` picks replicate-vs-offline by `replicate_available`;
  any generation failure falls back to a bundled file (no role left unmapped).
- **`tests/test_phaser_art.py`** — scaffold emits `preload()` + sprite render WHEN a
  role map is present; retains the primitive fallback; `src/sim.js` is byte-identical
  to the no-art scaffold (gate contract preserved); `game_art_enabled=False` → no
  preload, primitives only.
- **`scripts/gen_sprites.py`** — a test asserts the generated set covers every role and
  the files are valid PNGs.
- **Replicate role-sprite path** — with a stubbed `ReplicateClient` (no network),
  assert each role yields a written file + manifest entry, and that a stubbed failure
  on one role falls back to its bundled sprite (never a gap).
- **Verify-by-running:** a real `vite build` of a phaser game with the role map; confirm
  sprites load and the headless gate still passes the unchanged sim.

## Out of scope (deferred)

Procedural levels (rot.js, roadmap #11), animation/spritesheets, audio, the
game-designer GDD agent (#7) and specialists (#8). Phase 1 is static single-frame
sprites only.
