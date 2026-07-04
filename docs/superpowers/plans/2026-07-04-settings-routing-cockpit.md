# Settings Routing Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a compact routing cockpit to Settings so model-routing state is easier to scan.

**Architecture:** UI-only change. Derive `routingCockpit` from existing Settings query/local state and render it above the existing raw routing rows.

**Tech Stack:** React, TanStack Query, Tailwind classes, pytest source-level UI tests, Vite.

## Global Constraints

- Do not add backend endpoints or fields.
- Do not change model save/routing save request payloads.
- Keep the existing raw routing rows and model controls.
- Long model IDs must wrap instead of overflowing.

---

## File Structure

- `tests/test_web_ui.py`
  - Add structural assertions for the Settings routing cockpit.
- `skyn3t/web/ui/src/routes/Settings.jsx`
  - Add `routingCockpit`.
  - Render compact cockpit tiles in the existing `Model routing` panel.

---

### Task 1: Settings Routing Cockpit

**Files:**
- Modify: `tests/test_web_ui.py`
- Modify: `skyn3t/web/ui/src/routes/Settings.jsx`

**Interfaces:**
- Consumes: `active`, `routing`, `codegen`, `model`.
- Produces: `routingCockpit`, an array of `{ label, value }` objects rendered in Settings.

- [ ] **Step 1: Write the failing UI test**

Add to `test_settings_explains_model_precedence`:

```python
    assert "const routingCockpit =" in settings
    assert "Routing cockpit" in settings
    assert "requested backend" in settings
    assert "active route" in settings
    assert "primary model" in settings
    assert "codegen path" in settings
    assert "break-words" in settings
```

- [ ] **Step 2: Run the focused test and confirm failure**

Run:

```bash
pytest -q tests/test_web_ui.py::test_settings_explains_model_precedence
```

Expected: `FAIL` because `routingCockpit` is not present yet.

- [ ] **Step 3: Add derived cockpit state**

In `Settings.jsx`, after `codegenModelChoices`, add:

```jsx
  const routingCockpit = [
    { label: "requested backend", value: routing.requested || "auto" },
    { label: "active route", value: routing.active || active || "stub" },
    { label: "primary model", value: model || "auto · learned routing" },
    { label: "codegen path", value: codegen.reason || "follows active backend" },
  ];
```

- [ ] **Step 4: Render the cockpit grid**

Inside the `Model routing` panel, before the existing raw `Row` table, render:

```jsx
            <div className="mb-4 grid gap-2 md:grid-cols-4">
              {routingCockpit.map((item) => (
                <div key={item.label} className="rounded-md border border-hairline bg-void/45 p-3">
                  <div className="eyebrow text-[9px]">{item.label}</div>
                  <div className="mt-2 min-h-[2.5rem] break-words font-mono text-xs text-bone">
                    {item.value}
                  </div>
                </div>
              ))}
            </div>
```

- [ ] **Step 5: Verify and commit**

Run:

```bash
pytest -q tests/test_web_ui.py
cd skyn3t/web/ui && npm run build
pytest -q
git add docs/superpowers/specs/2026-07-04-settings-routing-cockpit-design.md docs/superpowers/plans/2026-07-04-settings-routing-cockpit.md tests/test_web_ui.py skyn3t/web/ui/src/routes/Settings.jsx
git commit -m "Add Settings routing cockpit"
```
