# Activity Signal Strip Design

_SkyN3t 2.0 · 2026-07-04_

## Goal

Make Activity easier to scan by summarizing the current event slice before the table. Users should know whether they are viewing live or replay data, how many events are visible, whether a filter is active, and whether an event is selected.

## Scope

In scope:

- Add a compact signal strip to the Activity page.
- Derive values from existing `mode`, `events`, `filtered`, `filter`, and `selected` state.
- Keep existing replay controls, filters, table, and selected-event panel unchanged.
- Add source-level UI tests.

Out of scope:

- New backend endpoints.
- New event types.
- Changing replay query behavior.
- Reworking the Activity table.

## Recommended Approach

Add an `activitySignals` array in `Activity.jsx` after `filtered` is derived. Render it below the header and above the replay panel.

Signals:

- View: `live` or `replay`.
- Visible: filtered count and total count.
- Filter: active filter text or `none`.
- Selected: selected event type or `none`.

Use the existing Foundry panel language: bordered tiles, mono labels, compact values, and overflow-safe text.

## Data Flow

```
Activity local state + stream events
  -> activitySignals array
  -> signal strip grid
  -> existing replay and table behavior unchanged
```

## Testing

Update `tests/test_web_ui.py` to assert:

- `const activitySignals =` exists.
- The page renders `Activity signals`.
- The signal labels `view`, `visible`, `filter`, and `selected` exist.
- Values derive from `mode`, `filtered.length`, `events.length`, `filter`, and `selected?.type`.

## Rollout

1. Add failing source-level UI assertions.
2. Implement the derived signal strip.
3. Run `pytest -q tests/test_web_ui.py`.
4. Run `npm run build` in `skyn3t/web/ui`.
5. Run the full suite before committing and pushing.
