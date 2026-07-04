# Settings Routing Cockpit Design

_SkyN3t 2.0 · 2026-07-04_

## Goal

Make Settings easier to understand when model routing feels ambiguous. The user should immediately see requested backend, active route, primary model mode, and codegen path without parsing the raw routing rows.

## Scope

In scope:

- Add a compact routing cockpit inside the existing Settings model-routing panel.
- Derive all cockpit rows from existing `secrets.data`, `routing`, `codegen`, and `model` state.
- Keep existing backend picker, model inputs, codegen inputs, model pins, and save actions unchanged.
- Add source-level UI tests for the new wiring.

Out of scope:

- New backend fields or endpoints.
- Changing model resolution behavior.
- Replacing the existing raw routing rows.
- A full Settings redesign.

## Recommended Approach

Add a `routingCockpit` array in `Settings.jsx` after the existing routing/model-choice derived values. Render it as a responsive grid at the top of the `Model routing` panel.

The cockpit should show:

- Requested: `routing.requested || "auto"`.
- Active: `routing.active || active || "stub"`.
- Primary model: `model || "auto · learned routing"`.
- Codegen: `codegen.reason || "follows active backend"`.

Use compact bordered tiles with mono labels and wrapping values so long model IDs do not break the panel.

## Data Flow

```
secrets.data + routing + local model state
  -> routingCockpit array
  -> Settings model-routing cockpit grid
  -> existing inputs and save APIs remain unchanged
```

## Error Handling

- Missing routing data should show safe fallback strings.
- Model validation errors continue to render below the primary model input.
- Save errors and messages remain unchanged.

## Testing

Update `tests/test_web_ui.py` to assert:

- `const routingCockpit =` exists.
- The Settings route renders `Routing cockpit`.
- The cockpit includes `requested backend`, `active route`, `primary model`, and `codegen path`.
- The existing model precedence and choice assertions remain intact.

## Rollout

1. Add the failing Settings UI structural test.
2. Implement `routingCockpit` and the grid in `Settings.jsx`.
3. Run `pytest -q tests/test_web_ui.py`.
4. Run `npm run build` in `skyn3t/web/ui`.
5. Run the full suite before committing and pushing.
