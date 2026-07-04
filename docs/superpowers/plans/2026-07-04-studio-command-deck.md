# Studio Command Deck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a compact command-deck summary to Studio so users can scan the selected build mode, model path, reference image state, and fan-out state before forging.

**Architecture:** Keep the change UI-only. Derive a `buildIntent` object from existing Studio local state and render it in the current top build panel; do not change API payloads or backend contracts.

**Tech Stack:** React, TanStack Query, Vite, Tailwind classes, pytest structural UI tests.

## Global Constraints

- Do not add runtime dependencies.
- Do not change `/api/builds` or `/studio/fanout` request shapes.
- Do not remove existing Studio controls.
- Preserve the Foundry visual language: compact, operational, ember/plasma accents, no marketing hero.
- Keep tests source-level and offline in `tests/test_web_ui.py`.

---

## File Structure

- `tests/test_web_ui.py`
  - Add one structural test for command-deck wiring.
- `skyn3t/web/ui/src/routes/Studio.jsx`
  - Add derived `buildIntent`.
  - Render the command-deck summary inside the existing top build panel.
  - Keep the existing form submit and fan-out logic intact.

---

### Task 1: Studio Command Deck

**Files:**
- Modify: `tests/test_web_ui.py`
- Modify: `skyn3t/web/ui/src/routes/Studio.jsx`

**Interfaces:**
- Consumes: Studio local state `buildProfile`, `fullApp`, `normalizedModelOverride`, `refImage`, `selectedStacks`, and `assetState`.
- Produces: `buildIntent` object with `mode`, `model`, `reference`, and `fanout` labels.

- [ ] **Step 1: Write the failing UI test**

Add this test after `test_studio_wires_build_profiles_and_manual_model`:

```python
def test_studio_has_command_deck_summary() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "const buildIntent =" in studio
    assert "Command deck" in studio
    assert "mode" in studio
    assert "model" in studio
    assert "reference" in studio
    assert "fan-out" in studio
    assert "assetState.label" in studio
    assert "selectedStacks.size" in studio
```

- [ ] **Step 2: Run the new test and confirm it fails**

Run:

```bash
pytest -q tests/test_web_ui.py::test_studio_has_command_deck_summary
```

Expected: `FAIL` because `buildIntent` and `Command deck` are not present yet.

- [ ] **Step 3: Add the derived command-deck state**

In `skyn3t/web/ui/src/routes/Studio.jsx`, after `assetState`, add:

```jsx
  const buildIntent = {
    mode: `${BUILD_PROFILES.find((p) => p.id === buildProfile)?.label || buildProfile}${fullApp ? " · full app" : ""}`,
    model: normalizedModelOverride || "learned routing",
    reference: refImage?.name || "no reference image",
    fanout: selectedStacks.size
      ? `${selectedStacks.size} stacks · ${[...selectedStacks].join(", ")}`
      : "auto stack",
  };
```

- [ ] **Step 4: Render the command deck**

Change the top `Panel` body in `Studio.jsx` from a single-column layout into a responsive grid. Add this summary column inside the panel:

```jsx
          <aside className="rounded-md border border-hairline bg-void/45 p-3">
            <div className="mb-3 flex items-center justify-between">
              <span className="eyebrow">Command deck</span>
              <Pill tone={assetState.tone}>{assetState.label}</Pill>
            </div>
            <div className="space-y-2 font-mono text-[11px]">
              {[
                ["mode", buildIntent.mode],
                ["model", buildIntent.model],
                ["reference", buildIntent.reference],
                ["fan-out", buildIntent.fanout],
              ].map(([label, value]) => (
                <div key={label} className="flex items-start justify-between gap-3 border-t border-hairline/60 pt-2">
                  <span className="text-ash/60">{label}</span>
                  <span className="max-w-[13rem] text-right text-bone">{value}</span>
                </div>
              ))}
            </div>
          </aside>
```

Keep the existing asset-status pill near the full-app control only if it still improves scanability; otherwise the command deck can own it.

- [ ] **Step 5: Run focused verification**

Run:

```bash
pytest -q tests/test_web_ui.py
cd skyn3t/web/ui && npm run build
```

Expected: UI tests pass and Vite exits `0`.

- [ ] **Step 6: Run broad verification and commit**

Run:

```bash
pytest -q
git status -sb
git add docs/superpowers/specs/2026-07-04-studio-command-deck-design.md docs/superpowers/plans/2026-07-04-studio-command-deck.md tests/test_web_ui.py skyn3t/web/ui/src/routes/Studio.jsx
git commit -m "Add Studio command deck"
```

Expected: full suite passes, then one focused commit is created.
