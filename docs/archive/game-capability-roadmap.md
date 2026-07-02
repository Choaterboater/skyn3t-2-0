# skyn3t Game + Visual Capability Roadmap

Merged output of two research fan-outs (2026-06-26): `game-capability-research` (2D art,
engines, design, designer) + `physics-gameplay-deepdive` (physics sim, gameplay patterns,
correctness gates). Goal: give skyn3t **legit** 2D-art, game-engineering, and
game-design/designer capability, then retry games.

> **Status (updated 2026-06-30):** the core of this plan is BUILT and merged. Items
> 1–7 and 9 of the sequenced plan below are shipped and battle-tested; #8 is partly
> shipped (gameplay-specialist checks landed as an advisory gate; physics-specialist
> not built); #10 is partial; #11–12 deferred. The remaining real blocker to a
> reliably-playable game is **codegen sprite-RENDERING reliability** (sprites get
> generated + loaded but not always wired into rendering), being worked now — NOT the
> engine, art, or gate layers. See per-item markers in the sequenced plan.

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
Status legend: ✅ shipped · 🟡 partial · ⬜ not started.
1. ✅ **Skills (hours):** rewrite 2d-game-engineering.md → Phaser; split out fixed-timestep +
   collision-correctness; author phaser-api.md. (Most correctness per hour — retrieval feeds codegen.)
   — Phaser/game-feel skills present in `data/skills/` (`2d-game-engineering.md`, `gh-phaserjs-phaser*.md`, `game-feel-juice.md`).
2. 🟡 **Static gates (low effort, reuse critic.py regex):** single-state, fixed-timestep, CCD,
   units, WASM-init, broadphase, collision-radius. Catches the COIN REAPER bug class at build.
   — behavioral checks landed via `studio/gameplay_checks.py` (run-don't-parse) + the headless gate; the full regex static-lint set is not all present by name.
3. ✅ **Richer design tokens** (pure-Python; lifts ALL UI). — `skyn3t/studio/design_tokens.py`.
4. ✅ **Phaser game stack:** REAL_BUILDER_STACKS + `_phaser_vite` scaffold + detector keywords + tests.
   — MERGED (PR #20, 2026-06-27); `stack_selector.py` lists `phaser`, `_scaffold.py` has `_phaser`.
5. ✅ **Headless invariant gate** (sibling to proof_run; the biggest correctness lever).
   — MERGED (PR #22) + SEALED (PR #23); `skyn3t/studio/headless_gate.py`.
6. ✅ **Art tier:** Kenney CC0 + DiceBear offline baseline + resolver; Replicate → premium. + art-director agent.
   — MERGED (PR #24/#25); `skyn3t/agents/art_director.py`, role sprites in `_scaffold.py`.
7. ✅ **game-designer agent** (GDD gate before codegen). — `skyn3t/agents/game_designer.py` (`game_designer_enabled`, default on).
8. 🟡 **physics-specialist + gameplay-specialist** review stages.
   — gameplay-specialist shipped as `studio/gameplay_checks.py` (advisory, feeds fix-loop); dedicated physics-specialist agent NOT built.
9. ✅ **qa-playtest agent** (runtime feel/balance scoring → fix-loop). **Then RETRY games.**
   — `studio/qa_playtest.py` + `game_visual_check.py` + `game_visual_loop.py` (all default on); games retried live across 3 debugging rounds.
10. 🟡 **App-UI:** critic.py → visual score→regenerate loop; shadcn/Radix/daisyUI substrate.
    — visual repair loop shipped (`visual_check.py` / `visual_loop.py` / `game_visual_loop.py`); shadcn substrate adoption partial.
11. ⬜ Procedural levels (rot.js) + backgrounds; premium AI sprites (pixel-art-xl / Retro Diffusion).
12. ⬜ Defer: fluids/soft-body (box2d-wasm+LiquidFun); kaplay secondary stack; 3D art (own project).

## 2026-06-30/07-01 session
Recently completed (verified in code + tests, full suite 1612 green):
- **Dangling-import codegen bug FIXED** (Workstream 1). Four stacked defects: `scaffold_missing_imports`
  wrote to the wrong filename for extension-qualified specs (`./PreloadScene.js` → `PreloadScene.js.jsx`);
  stub content was React-shaped on every stack; `CodeImproverAgent` could only EDIT files, never CREATE
  the missing one; and the lone "final guard" was CSS-only and ran BEFORE the game gates. Added
  `_final_consistency_check` — a true end-of-pipeline pass (after liveness) that re-runs deterministic
  repairs + an unresolved-imports rescan and can only ever DOWNGRADE a verdict. Bonus: fixed a real
  pre-existing path-traversal / arbitrary-file-write bug (`_confine()` on every stub write). Live-revalidated.
- **`apply_deterministic_repairs()` extracted** in `proof_run.py` — single source of truth for
  build-readying repairs, now shared by the build pipeline (`runner`) and the improve engine.
- **qa_playtest re-verifies after a repair** (`runner._run_qa_playtest_gate`): it was reading the stale
  pre-repair verdict, so a successful game repair could never flip `no_go → go`; now repair → re-run once.

Still open / next up (honest status):
- Item #8 physics-specialist agent, #11 procedural levels / premium sprites — not started.

## 2026-07-01 evening — the retry ladder (each rung caught a REAL, DIFFERENT defect)
- **Sprite-rendering false positive FIXED + live-validated**: `_VAR_KEYED_TEXTURE` recognizes computed
  texture keys; a real rebuild reported `sprites_rendered=true, missing=[]`. The previous "games still
  fail" item is closed.
- **tower-defence-retry-2** (no_go/49): qa_playtest correctly caught an uncaught runtime error — codegen
  loaded `/assets/sprites/gold.png` that the art tier never delivered (GDD said gold = currency, model
  invented an asset). FIXED at root: `asset_reconcile.reconcile_asset_refs` (deterministic placeholder
  PNG for any referenced-but-missing image, wired into `apply_deterministic_repairs`) + a directive
  clause forbidding invented `/assets/` paths.
- **tower-defence-retry-3** (no_go/49): asset guard clean (5/5 refs valid), qa_playtest + visual PASS —
  the headless gate caught `Infinity in state.hazard.cooldownRemaining` (model used Infinity as a
  "no hazard" sentinel; the gate requires JSON-serializable finite state BY DESIGN). FIXED same
  night (b55d36d): finite-state directive clause + file-targeted headless repairs.
- **tower-defence-retry-4 = GO/100** (2026-07-01 22:40): all fixes in — qa_playtest 0 console errors,
  all sprites rendered, headless 0 violations, visual clean, real vite build passed. The brief that
  opened this ladder is a delivered, playable game.
- Pattern worth keeping: score 49 has now meant three DIFFERENT correct catches. Never assume a repeat
  verdict is a repeat cause — read the manifest.

## Risks / guardrails
- Hallucination ∝ 1/training-data → Phaser/matter safe; pin APIs for the rest; never default to thin-corpus libs.
- CC0-only for fully-automated commercial output; auto-emit CREDITS.md for CC-BY; keep GPL tools (trianglify) as image-emitting CLI workers only.
- qa-playtest: scope to scripted invariant gate now (achievable), NOT RL playtesting.
- Cap loop iterations (critic regen + qa fix-loop) — unbounded loops ship the scaffold stub (per the codegen-timeout regression).
- Gate juice/animation to game/marketing stacks (do-no-harm: don't make dashboards noisy).
- Stale-web-server: restart the dashboard after scaffold/settings changes or no_go is an artifact.

_Full team transcripts: tasks/wv58gos0k.output (capability) + tasks/wizglma4z.output (physics/gameplay)._
