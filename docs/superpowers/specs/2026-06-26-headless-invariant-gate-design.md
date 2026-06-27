# Headless Invariant Gate — Design

_Roadmap item #5 (docs/game-capability-roadmap.md): "the biggest correctness lever."_
_Date: 2026-06-26. Stacks on the Phaser game stack (PR #20)._

## Goal

COIN REAPER built `go`/100 but played badly because nothing **enforced** runtime
correctness. The headless invariant gate runs a game's pure simulation core
outside the browser for N seeded ticks with scripted input and asserts hard
invariants (no NaN, no pool leak, determinism, pause/game-over correctness),
feeding violations into the fix-loop the same way compile errors are fed back.
It is a **sibling to `proof_run`** — an objective, offline, runtime gate.

## Core decision (approved)

**Pure sim-core split**, not a headless browser. To be runnable headlessly a
game must expose its logic as a pure module separate from Phaser rendering. This
also makes the #1 correctness rule (a single authoritative state updated by one
function) **structural**: the gate can only run if the game is built correctly.

## 1. The sim-core contract (`src/sim.js`)

A pure ES module with **no Phaser import**:

```js
export function createState(seed)          // deterministic initial state from an integer seed
export function step(state, input, dt)     // advance one tick; returns the (same) state
export function isWin(state)  -> boolean
export function isLose(state) -> boolean
```

- `input` = `{ left, right, up, down, action, pause }` (all booleans).
- `dt` is seconds (the gate uses a fixed `dt`, e.g. `1/60`).
- State conventions the gate relies on (each optional; a missing one disables
  only its own check, never the whole gate):
  - `state.paused` (bool) — when true, `step` must not advance the simulation.
  - `state.over` (bool) — when true (game over), `step` must ignore input.
- **Determinism**: all randomness must derive from `seed` via a seeded RNG
  carried in state. `Math.random()` / `Date.now()` are forbidden (the
  determinism check catches them).

The Phaser scene (`src/main.js`) owns **no logic**: each frame it reads input,
calls `step(this.state, input, dt)`, and draws `this.state`.

## 2. Scaffold refactor (`_scaffold._phaser`)

`_phaser` now emits:
- `src/sim.js` — the pure core: player integrated from input velocity, circular
  coin collision by distance, `score`, `hp`, win at `score >= TARGET`, lose at
  `hp <= 0`, seeded RNG for coin respawn, `paused`/`over` honored.
- `src/main.js` — Phaser scene that renders `state` (rectangle player, circle
  coin, HUD) and reads keys; calls `step`. No Arcade-as-authority.
- Unchanged: `package.json` (phaser+vite), `index.html`, `vite.config.js`,
  `styles.css`, `.gitignore`, `README.md`.

The scaffold is **self-proving**: the gate runs its own `sim.js` and passes.

## 3. The gate (`skyn3t/studio/headless_gate.py`)

`run_headless_gate(project_dir, *, ticks=600, seed=1234, timeout=30) -> HeadlessGateResult`

- Locate the sim entry: `src/sim.js` (configurable; fall back to scanning for a
  module exporting `step` + `createState`).
- If none found → `HeadlessGateResult(applicable=False, passed=True)` (skip,
  non-blocking — a game without the convention is not failed, only un-gated).
- Write an embedded Node harness (`.mjs` string, like `proof_run` shells out to
  npm) to a temp dir; `node harness.mjs <sim_path>`; parse its JSON stdout.
- Never raises; subprocess errors / missing `node` → `applicable=False` with a
  noted reason.

`HeadlessGateResult` (mirrors `ProofResult`):
```
applicable: bool          # was a sim core found + runnable
passed: bool              # no BLOCKING violation
violations: list[str]     # blocking invariant failures
report: dict              # non-blocking: reachability + juice/balance
detail: dict
def error_gaps() -> list[str]   # fix-loop feedback lines (one per violation)
```

## 4. Invariant battery (Node harness)

**Blocking** (fail the gate; feed the fix-loop). None needs a game-specific
"how to win" policy — they just run `step` and scan state:
1. **No NaN/Inf** — after N ticks under a mix of scripted + seeded-random input,
   deep-scan every numeric in state.
2. **No value explosion** — every numeric magnitude stays `< 1e6` (catches
   runaway integration / missing clamps).
3. **No unbounded pool growth** — no array in state exceeds an absolute cap
   (e.g. 10000). Uses a high cap, not monotonic detection, so legitimate
   spawning isn't a false positive.
4. **Determinism** — two runs from the same seed + identical input sequence
   produce an identical state trajectory (catches `Math.random`/`Date.now`).
5. **Pause freezes** — if `state.paused` is supported: set it and assert the
   sim does not change across ticks.
6. **Game-over disables input** — force `state.over = true`, step with full
   input, assert score/positions do not change.

**Non-blocking** (report only; best-effort, may need a scripted policy):
7. **Win reachable** — under a simple goal-seeking policy, does `isWin` become
   true within budget? Warn if never.
8. **Lose reachable** — does `isLose` become reachable under any input? Warn if
   never (no stakes).
9. **Juice/balance hints** — e.g. score-growth curve, time-to-first-event.

## 5. Pipeline wiring (game stacks only)

In `runner.py`, after `proof_run`, for `stack in _GAME_STACKS` (`{"phaser"}`):
- Run `run_headless_gate(project_dir)`.
- Attach `report` to the verdict output (non-blocking, always shown).
- If `violations`, feed `error_gaps()` into `_fix_loop` as error gaps (same
  channel proof errors use), so the loop repairs them.
- Do-no-harm: never runs for non-game stacks; behind a settings flag
  (`headless_gate_enabled`, default on for game stacks) so it can be disabled.

## 6. Codegen directive

Extend the `architect._GAME_STACKS` directive (and any game directive in
`code_agent`): instruct the model to author the pure `src/sim.js`
(createState/step/isWin/isLose, seeded RNG, `paused`/`over` flags; pause freezes,
game-over ignores input) and keep `src/main.js` render-only. This makes
codegen-produced games gate-compatible by construction.

## 7. Testing (TDD)

- **Known-good**: `run_headless_gate` on the scaffold's `sim.js` → `passed`,
  `applicable`.
- **Known-bad fixtures** (tiny sim.js strings, each tripping exactly one check):
  NaN-emitter, value-exploder, pool-leaker, non-deterministic (`Math.random`),
  pause-ignorer, game-over-ignorer → each produces the matching violation.
- **Skip-when-absent**: a project with no `sim.js` → `applicable=False`,
  `passed=True`.
- **`error_gaps()`** produces a fix-loop feedback line per violation.
- **Scaffold**: `scaffold_for("phaser", …)` ships `src/sim.js` exporting the
  contract and a render-only `src/main.js` (no logic in the scene).
- **Verify-by-running**: the test actually runs `node` (skip-marked if `node`
  is unavailable) on the scaffold sim + a bad fixture.

## Out of scope (YAGNI)

- RL playtesting / smart win policies beyond a trivial goal-seeker (roadmap
  guardrail: "scope to the scripted invariant gate now").
- Physics-engine sims (matter/planck) — they can run headlessly later under the
  same contract; not required for the default scaffold.
- Non-game stacks — explicitly excluded.

## Files

- New: `skyn3t/studio/headless_gate.py`, `tests/test_headless_gate.py`,
  this spec.
- Changed: `skyn3t/agents/_scaffold.py` (`_phaser` split),
  `skyn3t/studio/runner.py` (wiring), `skyn3t/agents/architect.py` (directive),
  `skyn3t/config/settings.py` (flag), `tests/test_phaser_stack.py` (assert the
  sim/render split).
