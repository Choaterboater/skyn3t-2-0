# Projects and Workspace Signal Grid Design

## Context

Studio, Settings, and Activity now expose compact signal grids that answer the
first operational question on each page before the user reads the detailed
controls. Projects and Workspace still require scanning tables or small status
lines to understand the current project state. This pass makes those pages
match the new cockpit pattern and extracts the repeated tile markup into one
shared primitive.

## Goals

- Add a Projects cockpit near the top of Projects with total projects, live
  previews, shippable projects, and wasted spend.
- Add a Workspace signals strip near the top of Workspace with selected project,
  stack, status/score, and recent activity for the selected project.
- Extract a shared `SignalGrid` component in `components/ui.jsx` and use it for
  Studio, Settings, Activity, Projects, and Workspace.
- Keep the UI compact, responsive, and readable for long slugs, model names,
  stack names, and status strings.

## Non-Goals

- No backend API changes.
- No cleanup, serve, improve, or project mutation behavior changes.
- No new dependencies.

## Design

`SignalGrid` is a presentational component that renders a label, optional right
side control, and responsive telemetry tiles. Each item has a `label`, `value`,
and optional `title`. The component owns the repeated tile classes:
`min-w-0`, `break-words`, and `[overflow-wrap:anywhere]`, so every cockpit keeps
long operational values inside the tile.

Projects derives `projectSignals` from the existing `projects` array and
`served` map:

- `projects`: `projects.length`
- `live`: number of currently served slugs
- `shippable`: projects whose status or verdict is `go`, `completed`, or
  `applied`
- `wasted`: sum of `wasted_usd` from no-go builds, formatted with `fmtCost`

Workspace derives `workspaceSignals` from the selected URL slug, current project,
and existing event stream:

- `selected`: current slug, or `none`
- `stack`: current stack, or `pick project`
- `status`: status plus score when a project is selected, or `idle`
- `activity`: count of serve/improve events matching the selected slug

The existing Studio command deck keeps its custom `aside` container and asset
pill, but uses `SignalGrid` for the tile body. Settings and Activity keep their
current `Panel` wrappers and replace only the duplicated grid markup.

## Error Handling

No new network calls are introduced. Missing project data renders stable
fallbacks such as `none`, `pick project`, `idle`, and `—`. Long values use the
shared wrapping classes instead of truncating into unreadable content.

## Testing

Structural tests in `tests/test_web_ui.py` will assert:

- `SignalGrid` is exported with the shared wrapping classes.
- Studio, Settings, Activity, Projects, and Workspace import and render
  `SignalGrid`.
- Projects defines the `projectSignals` derivation and displays
  `Projects cockpit`.
- Workspace defines the `workspaceSignals` derivation and displays
  `Workspace signals`.

Build verification will use `npm run build`; regression verification will use
`pytest -q tests/test_web_ui.py` and the full `pytest -q` suite.
