# Studio Rebuild Variants Design

_SkyN3t 2.0 · 2026-07-04_

## Goal

Let a user rerun a previous Studio build as a controlled variant: same idea and stack, but with different build quality settings such as build profile, model override, and full-app mode. The feature should make A/B build comparisons cheap, visible, and repeatable without requiring the user to retype the original brief.

## Scope

In scope:

- A backend replay contract that extracts the prior build's brief, stack, slug, profile, model override, and full-app flag from live or persisted build records.
- A direct `/api/builds/rebuild` endpoint for exact or near-exact replay.
- A Studio UI flow that prefers editable variants: copy a prior build into the existing build form, let the user change settings, then submit through the normal build route.
- Compact diagnostics in Recent Builds so the user can decide which build deserves a variant.
- Focused tests for replay extraction, endpoint behavior, and UI wiring.

Out of scope:

- Selecting individual internal pipeline agents such as architect, coder, reviewer, or critic.
- A full experiment dashboard or statistical comparison engine.
- Changing the StudioRunner stage graph.

## Recommended Approach

Use "Rebuild as variant" as the primary UI behavior.

The direct backend rebuild endpoint remains useful for API callers and exact replay, but the dashboard should make the editable path obvious. Clicking a build's Rebuild action should populate the existing form with:

- Original brief.
- Original stack pin, if available.
- Original build profile.
- Original model override.
- Original full-app mode.

The user can then switch profile/model/full-app settings and submit normally. This keeps the interaction understandable and avoids a separate modal or agent-roster surface before the simpler workflow proves useful.

## Backend Design

Add a small replay extraction helper near `submit_build` in `skyn3t/web/routes.py`.

Inputs should come from the most durable source available:

- Live `BuildRecord.to_dict()` if the build is still present in memory.
- `state.memory.get_build(build_id)` if the live cache no longer has it.
- `manifest.extra` and `model_trace` as fallback sources for profile/model metadata.

The extracted replay fields are:

- `brief`: required; missing brief returns `422`.
- `stack`: optional; passed back into `submit_build`.
- `slug`: optional; only reused if `reuse_slug=true`.
- `build_profile`: normalized through the existing profile normalizer.
- `model_override`: normalized through the existing model normalizer.
- `full_app`: true if the source build used the full-app contract or profile implies it.

`rebuild_build(state, build_id, reuse_slug=false)` should call `submit_build(...)` with those fields and return the new build id plus a `source_build_id` and `reused` metadata block. Missing builds return `404`.

## UI Design

Recent Builds should expose one primary affordance for the first implementation:

- `Rebuild`: populate the existing build form from the selected build, scroll/focus the form, and let the user submit with the normal `Forge build` button after adjusting settings.

This avoids surprise-submitting an expensive build. If the source build had a stack pin, Studio should keep that stack in variant state and include it in the next `/api/builds` payload. Show a compact source/stack pill near the form with a clear action so the user can return to the normal auto-stack flow.

The existing `buildDiagnostics(build)` helper should be rendered under the AI metadata in Recent Builds. It should summarize:

- Proof failure.
- Build failure.
- Terminal failed status.
- Zero skills.
- Zero recall.
- Completed builds with zero prompt count.

If none apply, show `no obvious gaps`.

The asset-generation pill already belongs near the Full app control because it explains whether richer generated assets are actually available for high-quality/full-app runs.

## Data Flow

```
Recent build row
  -> user clicks Rebuild
  -> Studio copies source metadata into the build form and variant stack state
  -> user adjusts profile/model/full-app or clears the source variant
  -> normal POST /api/builds
  -> new build streams through the existing forge line and ledger
```

Direct API replay:

```
POST /api/builds/rebuild { build_id, reuse_slug? }
  -> fetch live or persisted source build
  -> extract replay fields
  -> submit_build(...)
  -> return new build id + source metadata
```

## Error Handling

- Missing `build_id`: `422 build_id is required`.
- Unknown build id: `404 build not found`.
- Source build without a recoverable brief: `422 source build has no brief`.
- Bad or obsolete profile/model values are normalized through existing helpers rather than rejected.
- UI mutation errors are shown in the Recent Builds panel alongside approval/cancel errors.

## Testing

Backend tests:

- Rebuild a live build and assert the new build receives the original brief, stack, profile, model override, and full-app flag.
- Rebuild from a persisted memory row when the live cache misses.
- Missing brief returns `ValueError`.
- Missing build returns `KeyError`.

UI text/wiring tests:

- `Studio.jsx` includes the rebuild action.
- `Studio.jsx` renders diagnostics from `buildDiagnostics`.
- `Studio.jsx` keeps the asset-generation pill and model/profile controls wired to the existing build payload.

## Rollout

This is a narrow dashboard/API improvement. It does not change build execution internals, so the safe rollout path is:

1. Land backend replay extraction with tests.
2. Finish the Recent Builds UI action and diagnostics rendering.
3. Run focused backend/UI tests.
4. Run a broader web API suite if focused tests pass.
