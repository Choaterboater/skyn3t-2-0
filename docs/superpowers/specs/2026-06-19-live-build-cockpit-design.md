# Live Build Cockpit — Phase A Design

_SkyN3t 2.0 · 2026-06-19 · status: draft for review_

## 1. Goal

Make SkyN3t **debug each build step autonomously, before moving to the next**, and
**show that happening live** so you can watch an app take shape without ever being
prompted. This is Phase A of a three-phase roadmap (cockpit → self-learning →
mobile); each phase gets its own spec.

One-line vision: _the build self-corrects step-by-step, and you watch it on a cockpit;
the corrections it makes become the training signal that (Phase B) makes future apps
better._

## 2. Background — why this is a real change

Today the pipeline (`skyn3t/studio/runner.py:593` `StudioRunner.start`) runs all ~11
stages **blind**, merges the result with `merge_back` (`skyn3t/worktree.py:121`), then
does **one** objective proof-run (`skyn3t/studio/proof_run.py:112`) and **one** bounded
repair loop (`runner.py:375 _fix_loop`) at the very end.

Two consequences:

1. **Errors compound.** A bad decision in `architect` is not caught until `verify_boot`
   fails six stages later. Debugging happens in the wrong place — at the end, globally —
   instead of per step.
2. **Nothing is watchable mid-build.** Files live in an isolated git worktree the whole
   time and only appear in `Projects/<slug>/` at the final atomic `merge_back`. The SPA
   already animates a "forge-line" of the stage stream from `BUILD_STAGE_*` events
   (`web/ui/src/routes/Studio.jsx`), so **stage progress** is visible — but the **app
   itself** (files, rendered UI) is not.

Phase A moves debugging *inside* the per-stage loop and adds a live preview surface.

## 3. Scope

**In scope (Phase A):**

- `StageDebugPass` — a per-stage verify→fix→re-check micro-loop.
- New events: `STAGE_DEBUG_STARTED`, `STAGE_DEBUG_ATTEMPT`, `STAGE_DEBUG_RESOLVED`,
  `STAGE_ARTIFACT_SNAPSHOT`.
- Preview serving: `GET /preview/{slug}` and `GET /projects/{slug}/{path}`.
- `.preview/` incremental worktree sync (read-only, per debugged stage).
- Cockpit UI: per-stage debug timeline + live preview panel + "files so far" tree, on
  the existing Studio page.
- Autonomous failure policy (no prompts).

**Out of scope (later phases / explicitly excluded):**

- Cross-build learning that consumes the debug signal → **Phase B** (own spec).
- Mobile / cross-platform target stack → **Phase C** (own spec).
- Human steering or hand-edits mid-build → **excluded by design** ("no prompts").

## 4. Architecture

### 4.1 The per-stage debug loop (`StageDebugPass`)

Generalize the existing end-of-build `_fix_loop` (`runner.py:375`) into a reusable pass
invoked **inside** the stage loop (`runner.py:667-731`), after each productive stage and
before the next begins. It reuses existing machinery: scoped `proof_run`, the
`code_improver` agent, `CostTracker`, and `LLMClient.budget`.

```
for stage in plan.stages:
    run stage                       # existing
    StageDebugPass(stage):          # NEW
        check = stage_check(stage)  # stage-appropriate (table below)
        if check.passed: emit RESOLVED(passed); continue
        for attempt in 1..K:
            fix = repair(stage, check)        # code_improver / deterministic fill
            emit STAGE_DEBUG_ATTEMPT(...)
            check = stage_check(stage)
            if check.passed: break
        if not check.passed:
            escalate(tier+1) within per_build_usd_cap; re-check   # bounded
        resolve(stage, check)       # pass | degraded | (critical) re-attempt/no_go
    snapshot_to_preview(stage)      # NEW (.preview/ sync + STAGE_ARTIFACT_SNAPSHOT)
```

**Per-stage check definitions** (what "passing" means; not every stage is debuggable the
same way):

| Stage | Check |
| --- | --- |
| brainstorm / research | light sanity (non-empty, on-brief); no fix loop |
| architect / design | contract check — required artifacts / decisions present |
| **code** | full scoped `proof_run` (syntax + substance + entrypoint) → `code_improver` fix loop |
| verify_contract / verify_build / verify_boot | the stage already checks; debug loop retries its own fix |
| critic / review / package | re-run against the **debugged** tree, not the blind one |

### 4.2 Failure policy (autonomous, no prompts)

Tiered and fully automated:

1. Bounded fix attempts (`K`, default 3) at the current model tier.
2. If still failing → **escalate** (more attempts / stronger tier) — strictly within
   `per_build_usd_cap`; `LLMClient.budget` remains the hard backstop.
3. Still failing:
   - **Non-critical** step → proceed **best-effort**, flag the step `degraded`.
   - **Critical** step (`code`, `verify_build`) → one **fresh re-attempt** (best-of-N
     style) first; if it still fails, deliver what exists as **`completed_no_go`** (reuse
     the status + `_final_build_status` helper added in `a720c7d`) rather than abort —
     "no prompts" means a partial, flagged app beats nothing.

The loop never blocks waiting on a human; approval gates are not used in this flow.

### 4.3 Events & live display

New `EventType` members (`skyn3t/core/events.py:29-66`). **Critical discipline:** the
event payload contract is added in the enum, the WS hub wrapper
(`web/websockets.py:70`, wraps as `{event:{…}}`), and the SPA reader
(`web/ui/src/api.js:112`, unwraps) **as one atomic change** — drift across these three
layers is this codebase's #1 recurring bug (commit `5029b10`), and the contract test
must cover the new events.

| Event | Payload |
| --- | --- |
| `STAGE_DEBUG_STARTED` | `{build_id, stage, capability, check}` |
| `STAGE_DEBUG_ATTEMPT` | `{build_id, stage, agent_type, attempt, errors, fix_applied, passed, score_before, score_after}` |
| `STAGE_DEBUG_RESOLVED` | `{build_id, stage, status: passed\|degraded\|reattempt\|no_go, reason}` |
| `STAGE_ARTIFACT_SNAPSHOT` | `{build_id, stage, files: [path…], preview_url?}` |

### 4.4 Preview serving

Clean seam in `web/app.py:139-147` (guard `/preview` and `/projects` **before** the SPA
catch-all). New endpoints on the build router (`web/routes.py:584+`):

- `GET /preview/{slug}` → manifest + file tree (read `skyn3t_manifest.json`).
- `GET /projects/{slug}/{path}` → file contents via `FileResponse`.

Behavior by stack:

- **Web (react/nextjs/static):** once `code`'s debug pass yields a buildable tree, run the
  production `vite build → dist/` (`proof_run` already does this) and serve `dist/` at the
  preview URL, refreshed as later steps/fixes change it.
- **CLI / Python:** preview = live file tree + code viewer + proof-run console output
  (nothing to render).

**Security (non-negotiable, given the WS-token-leak history):** preview routes are
auth-gated like every other route, honor the loopback-only posture, serve **nothing**
outside `Projects/<slug>/`, and are path-traversal guarded.

### 4.5 Incremental worktree sync (`.preview/`)

The one change to the delivery model. Today delivery is atomic — files only reach
`Projects/<slug>/` at the final `merge_back`. Add a **per-stage, read-only sync** of the
worktree into `Projects/<slug>/.preview/` after each debugged step (the cockpit watches
`.preview/`). The real atomic `merge_back` at the end is unchanged, preserving the
"delivered != empty" guarantee. `.preview/` is disposable and git-ignored.

### 4.6 Cockpit UI

Extends `web/ui/src/routes/Studio.jsx` (no new page). New panels beside the existing
forge-line:

- **Stage-debug timeline** (left): the `STAGE_DEBUG_*` stream — per-stage attempts,
  errors found, fixes applied, pass/degraded flags.
- **Live preview** (right): rendered `iframe` of the built app (web) or run console
  (cli/py), refreshed per debugged stage.
- **Files so far** (right, lower): the `.preview/` tree from `STAGE_ARTIFACT_SNAPSHOT`.

## 5. Phase B readiness

`STAGE_DEBUG_ATTEMPT` is intentionally shaped as a learning transcript
(`{agent_type, stage, instruction, input_digest, output_digest, errors, fix_applied,
passed, score_before, score_after}`) — exactly what `Reflector.propose_prompt_improvement`
(`skyn3t/intelligence/reflection.py:185`) needs. Phase A **produces** the signal; Phase B
**consumes** it (wire transcript collection → Reflector → evolved agent instructions;
populate `ModelTournament.record_win`; replace binary reward with `score_after -
score_before`). Building A correctly makes B a wiring task, not a redesign.

## 6. Data flow

```
stage runs → StageDebugPass (check→fix→recheck, bounded, escalating)
   → STAGE_DEBUG_* events + .preview/ sync + STAGE_ARTIFACT_SNAPSHOT
   → EventBus → WS hub {event:{…}} → SPA api.js → cockpit panels
   → /preview/{slug} + /projects/{slug}/{path} serve the live artifact
final stage → existing atomic merge_back → existing proof + delivery gate (unchanged)
```

## 7. Testing

- Offline-first: every test runs on the deterministic stub backend, no keys, no Docker.
- `StageDebugPass`: per-stage pass / fix-then-pass / degraded / critical-no_go paths.
- Events: contract test asserting `STAGE_DEBUG_*` + `STAGE_ARTIFACT_SNAPSHOT` payloads
  survive the enum → WS-wrap → SPA-unwrap round trip (guards the `5029b10` bug class).
- Routes: `/preview/{slug}` and `/projects/{slug}/{path}` — happy path, auth gate,
  path-traversal rejection, missing-slug 404.
- Degradation: no Docker → inline proof; offline → file-tree-only preview (no render).
- Budget: per-stage escalation never exceeds `per_build_usd_cap`.
- Keep the suite green (currently 330 pass / 1 skip) and Vite-rebuild the SPA.

## 8. File-level change map

| File | Change |
| --- | --- |
| `skyn3t/studio/runner.py` | factor `_fix_loop` → `StageDebugPass`; invoke in stage loop; emit new events; `.preview/` sync |
| `skyn3t/studio/proof_run.py` | scoped per-stage check entrypoint (reuse existing checks) |
| `skyn3t/core/events.py` | add 4 `EventType` members + payload docs |
| `skyn3t/web/websockets.py` | ensure new events fan out (wrap contract) |
| `skyn3t/web/routes.py` | `/preview/{slug}`, `/projects/{slug}/{path}` |
| `skyn3t/web/app.py` | route guard before SPA catch-all |
| `skyn3t/web/ui/src/api.js` | consume new events (unwrap contract) |
| `skyn3t/web/ui/src/routes/Studio.jsx` | debug timeline + preview + files-so-far panels |
| `.gitignore` | ignore `Projects/*/.preview/` |
| `tests/…` | loop, event-contract, route, degradation, budget tests |

## 9. Risks / open questions

- **Per-stage proof cost.** Running checks every stage costs more than one final proof.
  Mitigation: cheap checks for non-code stages; full `proof_run` only on `code`/`verify_*`;
  all within `per_build_usd_cap`.
- **Preview render latency.** `vite build` per refresh is slow. Phase A serves the
  production build post-`code`; a true dev-server (HMR) is a possible later optimization,
  not in scope.
- **`.preview/` disk churn.** Bounded by clean-on-build; git-ignored.

## 10. Roadmap (future specs)

- **Phase B — Learn from the cockpit:** consume `STAGE_DEBUG_*` as transcripts; wire
  `Reflector` → evolved instructions; populate `ModelTournament`; score-delta reward.
- **Phase C — Mobile target stack:** register a new stack in all three vocabularies
  (`agents/_common.py` `detect_stack`/`_normalize_stack`, `agents/_scaffold.py` `_BUILDERS`,
  `intelligence/skill_library.py` `_STACK_GROUPS`) + sandbox toolchain image + Expo/web
  preview path.
