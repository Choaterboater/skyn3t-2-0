# Projects Workspace Signal Grid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add shared signal grids across Projects and Workspace while extracting the duplicated telemetry tile markup used by Studio, Settings, and Activity.

**Architecture:** A new `SignalGrid` primitive in `components/ui.jsx` owns the repeated responsive tile layout. Route components keep their existing data derivations and wrappers, passing simple `{ label, value, title }` objects into the shared primitive.

**Tech Stack:** React, Vite, Tailwind CSS, pytest structural source tests.

## Global Constraints

- No backend API changes.
- No new dependencies.
- Keep long values readable with `min-w-0`, `break-words`, and `[overflow-wrap:anywhere]`.
- Preserve existing serve, cleanup, improve, routing, and build-submission behavior.
- Use TDD: add failing structural tests before production source edits.

---

### Task 1: Structural Test Coverage

**Files:**
- Modify: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: route source files under `skyn3t/web/ui/src/routes`.
- Produces: failing assertions for `SignalGrid`, `projectSignals`, and `workspaceSignals`.

- [ ] **Step 1: Write the failing test**

Add a test that reads `components/ui.jsx` and asserts:

```python
def test_signal_grid_primitive_wraps_long_values() -> None:
    ui = (SRC / "components" / "ui.jsx").read_text()
    assert "export function SignalGrid" in ui
    assert "items.map((item)" in ui
    assert "min-w-0" in ui
    assert "break-words" in ui
    assert "[overflow-wrap:anywhere]" in ui
```

Extend existing route tests so they require `SignalGrid` imports and the new
Projects/Workspace derivations:

```python
assert "SignalGrid" in projects
assert "const projectSignals =" in projects
assert "Projects cockpit" in projects
assert 'label: "shippable"' in projects
assert 'label: "wasted"' in projects
```

```python
workspace = (ROUTES / "Workspace.jsx").read_text()
assert "SignalGrid" in workspace
assert "const workspaceSignals =" in workspace
assert "Workspace signals" in workspace
assert 'label: "selected"' in workspace
assert 'label: "activity"' in workspace
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_web_ui.py`

Expected: FAIL because `SignalGrid`, `projectSignals`, and `workspaceSignals`
do not exist yet.

### Task 2: Shared SignalGrid Primitive and Existing Refactors

**Files:**
- Modify: `skyn3t/web/ui/src/components/ui.jsx`
- Modify: `skyn3t/web/ui/src/routes/Activity.jsx`
- Modify: `skyn3t/web/ui/src/routes/Settings.jsx`
- Modify: `skyn3t/web/ui/src/routes/Studio.jsx`

**Interfaces:**
- Produces: `SignalGrid({ label, items, right, className, gridClassName, valueClassName })`.
- Consumes: existing `activitySignals`, `routingCockpit`, and `buildIntent` values.

- [ ] **Step 1: Implement the primitive**

Add this exported component to `components/ui.jsx`:

```jsx
export function SignalGrid({
  label,
  items,
  right = null,
  className = "",
  gridClassName = "sm:grid-cols-2 xl:grid-cols-4",
  valueClassName = "",
}) {
  return (
    <div className={className}>
      {label || right ? (
        <div className="mb-2 flex items-center justify-between gap-2">
          {label ? <div className="eyebrow">{label}</div> : <span />}
          {right}
        </div>
      ) : null}
      <div className={`grid gap-2 ${gridClassName}`}>
        {items.map((item) => (
          <div
            key={item.label}
            className="min-w-0 rounded-md border border-hairline bg-void/45 p-3"
            title={item.title || String(item.value ?? "")}
          >
            <div className="eyebrow text-[9px]">{item.label}</div>
            <div className={`mt-2 min-w-0 break-words [overflow-wrap:anywhere] font-mono text-xs text-bone ${valueClassName}`}>
              {item.value ?? "—"}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Refactor current signal grids**

Import `SignalGrid` in Activity, Settings, and Studio. Replace duplicated tile
maps with `<SignalGrid />` while preserving the existing `Panel` or `aside`
wrappers and existing labels.

- [ ] **Step 3: Run test to verify partial progress**

Run: `pytest -q tests/test_web_ui.py`

Expected: Still FAIL until Projects and Workspace are implemented.

### Task 3: Projects Cockpit

**Files:**
- Modify: `skyn3t/web/ui/src/routes/Projects.jsx`

**Interfaces:**
- Consumes: existing `projects`, `served`, `fmtCost`.
- Produces: `projectSignals` and a `Projects cockpit` panel before cleanup.

- [ ] **Step 1: Add signal derivation**

After `liveCount`, derive:

```jsx
const shippableCount = projects.filter((project) => {
  const state = String(project.status || project.verdict || "").toLowerCase();
  return state === "go" || state === "completed" || state === "applied";
}).length;
const wastedSpend = projects.reduce((sum, project) => sum + Number(project.wasted_usd || 0), 0);
const projectSignals = [
  { label: "projects", value: String(projects.length) },
  { label: "live", value: String(liveCount) },
  { label: "shippable", value: String(shippableCount) },
  { label: "wasted", value: fmtCost(wastedSpend) },
];
```

- [ ] **Step 2: Render the cockpit**

Render a compact `Panel` with `<SignalGrid label="Projects cockpit" items={projectSignals} />`
between the error panel and `CleanupPanel`.

- [ ] **Step 3: Run test to verify progress**

Run: `pytest -q tests/test_web_ui.py`

Expected: Still FAIL until Workspace is implemented.

### Task 4: Workspace Signals

**Files:**
- Modify: `skyn3t/web/ui/src/routes/Workspace.jsx`

**Interfaces:**
- Consumes: existing `slug`, `current`, and `stream?.events`.
- Produces: `workspaceSignals` and a `Workspace signals` panel before the two-pane grid.

- [ ] **Step 1: Add signal derivation**

After `current`, derive:

```jsx
const workspaceActivity = (stream?.events || []).filter(
  (event) =>
    slug &&
    (event.type?.startsWith("serve.") || event.type?.startsWith("improve.")) &&
    event.payload?.slug === slug,
).length;
const workspaceSignals = [
  { label: "selected", value: slug || "none" },
  { label: "stack", value: current?.stack || (slug ? "unknown stack" : "pick project") },
  {
    label: "status",
    value: current ? `${current.status || "—"} · score ${current.score ?? "—"}` : "idle",
  },
  { label: "activity", value: slug ? `${workspaceActivity} event${workspaceActivity === 1 ? "" : "s"}` : "none" },
];
```

- [ ] **Step 2: Render the signal strip**

Replace the small selected-project status line with a `Panel` containing
`<SignalGrid label="Workspace signals" items={workspaceSignals} />`.

- [ ] **Step 3: Run focused verification**

Run: `pytest -q tests/test_web_ui.py`

Expected: PASS.

### Task 5: Build and Regression Verification

**Files:**
- Verify only.

**Interfaces:**
- Consumes: all source changes from Tasks 1-4.
- Produces: local evidence for commit and push.

- [ ] **Step 1: Build frontend**

Run: `npm run build` from `skyn3t/web/ui`.

Expected: Vite build exits 0.

- [ ] **Step 2: Run full Python regression suite**

Run: `pytest -q` from the repo root.

Expected: full suite exits 0.

- [ ] **Step 3: Commit and push**

Run:

```bash
git status --short
git add tests/test_web_ui.py skyn3t/web/ui/src/components/ui.jsx skyn3t/web/ui/src/routes/Activity.jsx skyn3t/web/ui/src/routes/Settings.jsx skyn3t/web/ui/src/routes/Studio.jsx skyn3t/web/ui/src/routes/Projects.jsx skyn3t/web/ui/src/routes/Workspace.jsx
git commit -m "Add shared signal grids"
git push
```

## Execution Evidence

- `pytest -q tests/test_web_ui.py` failed red after adding structural SignalGrid,
  Projects, and Workspace assertions, then passed after implementation.
- `pytest -q tests/test_web_spa_compat.py` failed red for cached SPA index
  fallback after simulated frontend rebuild, then passed after reading
  `dist/index.html` per request.
- Code-review follow-up added regressions for encoded SPA traversal and
  correlation-matched Workspace activity; `pytest -q tests/test_web_spa_compat.py
  tests/test_web_ui.py` passed with 43 tests.
- Frontend verification: `npm run build` completed successfully.
- Full regression verification: `pytest -q` completed with 2249 passed,
  3 skipped, and 73 warnings before the review follow-up patch.
- Final post-review regression verification: `pytest -q` completed with
  2251 passed, 3 skipped, and 73 warnings.
