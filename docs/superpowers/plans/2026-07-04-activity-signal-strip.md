# Activity Signal Strip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a compact signal strip to Activity so users can scan the current event slice before reading the table.

**Architecture:** UI-only change. Derive `activitySignals` from existing Activity local state and render it above replay controls.

**Tech Stack:** React, Tailwind classes, pytest source-level UI tests, Vite.

## Global Constraints

- Do not change replay API calls or query params.
- Do not change event filtering semantics.
- Do not add dependencies.
- Long filter and event type values must wrap safely.

---

## File Structure

- `tests/test_web_ui.py`
  - Extend the Activity structural test.
- `skyn3t/web/ui/src/routes/Activity.jsx`
  - Add `activitySignals`.
  - Render the signal strip under the page header.

---

### Task 1: Activity Signal Strip

**Files:**
- Modify: `tests/test_web_ui.py`
- Modify: `skyn3t/web/ui/src/routes/Activity.jsx`

**Interfaces:**
- Consumes: `mode`, `events`, `filtered`, `filter`, `selected`.
- Produces: `activitySignals`, an array of `{ label, value }` objects.

- [ ] **Step 1: Write the failing UI test**

Add to `test_activity_wires_trajectory_replay_ui`:

```python
    assert "const activitySignals =" in activity
    assert "Activity signals" in activity
    assert 'label: "view", value: mode' in activity
    assert 'label: "visible", value: `${filtered.length}/${events.length}`' in activity
    assert 'label: "filter", value: filter.trim() || "none"' in activity
    assert 'label: "selected", value: selected?.type || "none"' in activity
    assert "[overflow-wrap:anywhere]" in activity
```

- [ ] **Step 2: Run the focused test and confirm failure**

Run:

```bash
pytest -q tests/test_web_ui.py::test_activity_wires_trajectory_replay_ui
```

Expected: `FAIL` because `activitySignals` is not present yet.

- [ ] **Step 3: Add derived Activity signals**

After `filtered` is derived in `Activity.jsx`, add:

```jsx
  const activitySignals = [
    { label: "view", value: mode },
    { label: "visible", value: `${filtered.length}/${events.length}` },
    { label: "filter", value: filter.trim() || "none" },
    { label: "selected", value: selected?.type || "none" },
  ];
```

- [ ] **Step 4: Render the signal strip**

Under `PageHeader`, add:

```jsx
      <Panel className="mb-4 p-3">
        <div className="eyebrow mb-2">Activity signals</div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {activitySignals.map((item) => (
            <div key={item.label} className="min-w-0 rounded-md border border-hairline bg-void/45 p-3">
              <div className="eyebrow text-[9px]">{item.label}</div>
              <div className="mt-2 min-w-0 break-words [overflow-wrap:anywhere] font-mono text-xs text-bone">
                {item.value}
              </div>
            </div>
          ))}
        </div>
      </Panel>
```

- [ ] **Step 5: Verify and commit**

Run:

```bash
pytest -q tests/test_web_ui.py
cd skyn3t/web/ui && npm run build
pytest -q
git add docs/superpowers/specs/2026-07-04-activity-signal-strip-design.md docs/superpowers/plans/2026-07-04-activity-signal-strip.md tests/test_web_ui.py skyn3t/web/ui/src/routes/Activity.jsx
git commit -m "Add Activity signal strip"
```
