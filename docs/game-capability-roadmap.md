# skyn3t Game + Visual Capability Roadmap

Merged output of two research fan-outs (2026-06-26): `game-capability-research` (2D art,
engines, design, designer) + `physics-gameplay-deepdive` (physics sim, gameplay patterns,
correctness gates). Goal: give skyn3t **legit** 2D-art, game-engineering, and
game-design/designer capability, then retry games.

## The core finding
COIN REAPER built `go`/100 but played badly (collision-from-far, dual-state, weak feel)
because skyn3t **hand-rolls the game loop** and nothing **enforces** correctness. Fix =
(1) generate ON an engine that owns the loop/collision, (2) gate correctness automatically,
(3) give it real art + specialist agents.

## Recommended substrates
- **Engine (default): Phaser 3** (MIT) — most LLM training data → least hallucination;
  owns loop, fixed-step, scene FSM, Arcade physics, tilemaps. Removes the COIN REAPER class.
- **Physics tiers (by intent):** matter.js (MIT, default casual — easiest for an LLM) →
  planck.js (MIT, Box2D port: deterministic/joints/CCD via setBullet, MKS-units gotcha) →
  @dimforge/rapier2d (Apache-2.0, WASM: perf + cross-platform determinism, async-init gotcha)
  → box2d-wasm+LiquidFun (fluids/soft-body, specialist, defer). **Never default to**
  rapier/box2d-wasm/kaplay/bitECS (thin corpora) without an API-pinning skill. Drop p2.js.
- **Art (default, free, deterministic):** Kenney CC0 packs + DiceBear (CC0 styles) + a
  role→sprite resolver; Replicate/pixel-art-xl/Retro-Diffusion demoted to premium tier.
  CC0-only for auto-commercial output.
- **App UI:** expand `design_tokens.py` to full scales + shadcn var names; adopt
  shadcn/Radix/daisyUI as the default UI substrate.

## Correctness gates (the automatable "catch that stuff" — reuse critic.py + proof_run)
Static: single-state (no sim in React render/effect, no two stores) · fixed-timestep (no
Engine.run/`world.step(rawDelta)`; require accumulator + clamped dt; all motion ×dt) ·
CCD/bullet on fast bodies · PIXELS_PER_METER for Box2D-family · `await init()` for WASM ·
broadphase before O(n²) >~150 entities · collision-radius == visual-radius.
Runtime (the key one): **headless invariant gate** — run the pure `step(state,input,dt)`
for N seeded ticks with scripted input; assert no NaN/Inf, hp in bounds, entity pools don't
leak, win AND lose reachable, pause stops stepping, game-over disables input. Non-blocking:
juice-score + balance-curve report. Wire low scores/missing-mechanics into the fix-loop the
same way compile errors are fed back.

## Specialist agents to add
- **game-designer** — fills a GDD (core loop, mechanics, win/lose, scope, MDA aesthetic)
  BEFORE codegen; persists as a build artifact = the completeness spec.
- **physics-specialist** — deterministic+LLM review: accumulator present, no raw-delta step,
  CCD on fast bodies, units, WASM init, no teleport. Can block.
- **gameplay-specialist** — enforces single authoritative store, ECS/pooling above
  thresholds, FSM/BT with reachable fallback, pause/game-over correctness.
- **qa-playtest** — drives the running build (Playwright/proof_run harness) + the headless
  gate; emits feel/playability/juice + GDD-diff into the verdict; feeds the fix-loop.
- **art-director / asset-resolver** — maps semantic roles → Kenney/DiceBear/premium art.

## Skills to author (data/skills, tags=game)
fixed-timestep-game-loop · collision-correctness · physics-engine-selection ·
matter-js-game / planck-game / rapier-game scaffolds · spatial-partition · state-machines ·
enemy-ai-behavior-trees · steering-behaviors · pathfinding-astar · entity-component-systems ·
object-pooling · game-feel-juice (Nijman checklist + numeric recipes) · core-loop-and-balancing ·
game-design-lenses (Schell) · mda-game-design · game-design-document · phaser-api / kaplay-api
(pin API) · shadcn-ui-components · game-ui-art. Rewrite existing 2d-game-engineering.md → Phaser.

## Sequenced plan (quickest win → biggest lift)
1. **Skills (hours):** rewrite 2d-game-engineering.md → Phaser; split out fixed-timestep +
   collision-correctness; author phaser-api.md. (Most correctness per hour — retrieval feeds codegen.)
2. **Static gates (low effort, reuse critic.py regex):** single-state, fixed-timestep, CCD,
   units, WASM-init, broadphase, collision-radius. Catches the COIN REAPER bug class at build.
3. **Richer design tokens** (pure-Python; lifts ALL UI).
4. **Phaser game stack:** REAL_BUILDER_STACKS + `_phaser_vite` scaffold + detector keywords + tests.
5. **Headless invariant gate** (sibling to proof_run; the biggest correctness lever).
6. **Art tier:** Kenney CC0 + DiceBear offline baseline + resolver; Replicate → premium. + art-director agent.
7. **game-designer agent** (GDD gate before codegen).
8. **physics-specialist + gameplay-specialist** review stages.
9. **qa-playtest agent** (runtime feel/balance scoring → fix-loop). **Then RETRY games.**
10. **App-UI:** critic.py → visual score→regenerate loop; shadcn/Radix/daisyUI substrate.
11. Procedural levels (rot.js) + backgrounds; premium AI sprites (pixel-art-xl / Retro Diffusion).
12. Defer: fluids/soft-body (box2d-wasm+LiquidFun); kaplay secondary stack; 3D art (own project).

## Risks / guardrails
- Hallucination ∝ 1/training-data → Phaser/matter safe; pin APIs for the rest; never default to thin-corpus libs.
- CC0-only for fully-automated commercial output; auto-emit CREDITS.md for CC-BY; keep GPL tools (trianglify) as image-emitting CLI workers only.
- qa-playtest: scope to scripted invariant gate now (achievable), NOT RL playtesting.
- Cap loop iterations (critic regen + qa fix-loop) — unbounded loops ship the scaffold stub (per the codegen-timeout regression).
- Gate juice/animation to game/marketing stacks (do-no-harm: don't make dashboards noisy).
- Stale-web-server: restart the dashboard after scaffold/settings changes or no_go is an artifact.

_Full team transcripts: tasks/wv58gos0k.output (capability) + tasks/wizglma4z.output (physics/gameplay)._
