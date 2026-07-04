# Studio Command Deck Design

_SkyN3t 2.0 · 2026-07-04_

## Goal

Make the top of Studio feel more intentional, easier to scan, and more useful before a build starts. The user should be able to see the selected build mode, model path, reference image state, and fan-out state at a glance without reading every control.

## Scope

In scope:

- Reorganize the existing Studio build form into a command-deck layout.
- Add a live build summary derived from local UI state.
- Keep existing build submission, reference image, profile, model override, full-app, and fan-out behavior.
- Preserve the Foundry visual language: compact panels, mono labels, ember/plasma status accents, and dense operational controls.
- Add structural UI tests that prove the new command-deck wiring is present.

Out of scope:

- New backend endpoints.
- New packages or icon libraries.
- A whole-app redesign.
- Changing the build pipeline or model-routing semantics.
- Replacing the existing forge line, stage ledger, debug timeline, or recent-build table.

## Recommended Approach

Use a command-deck panel at the top of Studio.

The left side remains the build brief and main Forge action. The right side becomes a compact summary rail with four status rows:

- Mode: selected build profile plus full-app state.
- Model: manual override when present, otherwise learned routing.
- Reference: attached image name or no reference image.
- Fan-out: selected stack count and selected stack ids when applicable.

The existing profile chips, model override, reference image, and full-app controls stay in the same top panel, but the summary makes the current build intent visible before submission. Fan-out remains optional and still requires two selected stacks.

## UI Details

Add a small `buildIntent` object in `Studio.jsx` that derives labels from existing state:

- `mode`: profile label and whether full-app is enabled.
- `model`: manual model override or learned routing.
- `reference`: selected image name or no image.
- `fanout`: selected stack ids or auto-stack.

Render this object as a command summary inside the top panel. Avoid explanatory marketing text; use compact operational labels and values.

The top panel should become a responsive two-column layout on large screens:

- Main column: brief input, reference image, Forge button, profile/model/full-app controls, examples, fan-out controls.
- Summary column: command deck with the derived build intent and asset status.

On mobile, the summary should stack below the primary form controls.

## Data Flow

```
Studio local state
  -> buildIntent derived object
  -> command summary rows
  -> existing POST /api/builds payload remains unchanged
```

No backend data shape changes are required.

## Error Handling

- Existing submit and fan-out errors remain rendered in the top panel.
- Empty brief continues to disable the Forge button.
- Fan-out continues to require at least two selected stacks.
- Unknown model override warning remains near the model input.

## Testing

Update `tests/test_web_ui.py` with a structural test that asserts:

- `buildIntent` exists.
- Studio renders `Command deck`.
- The command summary includes `mode`, `model`, `reference`, and `fan-out`.
- The existing `/api/builds` payload remains unchanged by checking the same `build_profile`, `full_app`, `model_override`, and fan-out wiring strings.

## Rollout

This is a narrow UI-only improvement. The safe path is:

1. Add the failing structural UI test.
2. Implement the command-deck layout in `Studio.jsx`.
3. Run `pytest -q tests/test_web_ui.py`.
4. Run `npm run build` in `skyn3t/web/ui`.
5. Run the full suite before pushing if the focused checks pass.
