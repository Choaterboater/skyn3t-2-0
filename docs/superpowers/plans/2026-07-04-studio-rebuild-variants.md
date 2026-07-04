# Studio Rebuild Variants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Studio rerun a previous build as an editable variant with preserved brief, stack, profile, model override, and full-app mode.

**Architecture:** Keep build execution on the existing `/api/builds` path for the primary dashboard workflow. Add a small backend replay contract for API callers and for durable field extraction, then wire the Studio Recent Builds table to copy a source build into the existing form instead of surprise-submitting. Render compact diagnostics in Recent Builds so the user can pick a build worth varying.

**Tech Stack:** Python 3, FastAPI route helpers, pytest async tests, React, TanStack Query, Vite source-level UI tests.

## Global Constraints

- Preserve existing in-progress edits in `skyn3t/web/routes.py` and `skyn3t/web/ui/src/routes/Studio.jsx`; they are part of this feature.
- Do not add new runtime dependencies.
- Do not expose controls for individual internal pipeline agents in this slice.
- Direct `/api/builds/rebuild` remains available for API callers, but the Studio UI loads an editable variant into the normal build form.
- Rebuild variants must preserve brief, stack, build profile, model override, and full-app mode when those fields are recoverable.
- UI-triggered rebuild variants must not start an expensive build until the user presses the existing `Forge build` button.

---

## File Structure

- `skyn3t/web/routes.py`
  - Owns replay extraction, `rebuild_build(...)`, and the `/api/builds/rebuild` alias.
  - Also stores `model_trace.full_app` on new live `BuildRecord` instances so live rebuilds can preserve full-app mode.
- `tests/test_web_api.py`
  - Owns backend replay tests for live records, persisted records, and error paths.
- `skyn3t/web/ui/src/routes/Studio.jsx`
  - Owns editable variant state, form hydration, payload stack pinning, source pill, rebuild action, and diagnostics rendering.
- `tests/test_web_ui.py`
  - Owns structural UI tests that assert the rebuild variant flow is wired in the source.

---

### Task 1: Backend Replay Contract

**Files:**
- Modify: `tests/test_web_api.py`
- Modify: `skyn3t/web/routes.py`

**Interfaces:**
- Consumes: `routes.submit_build(state, brief, stack="", slug="", reference_image="", build_profile="cheap_learned", model_override="", full_app=False) -> dict[str, Any]`
- Produces: `routes._build_replay_fields(row: dict[str, Any]) -> dict[str, Any]`
- Produces: `routes.rebuild_build(state: AppState, build_id: str, *, reuse_slug: bool = False) -> dict[str, Any]`
- Produces: `BuildRecord.model_trace["full_app"]` as a boolean on new web-submitted builds.

- [ ] **Step 1: Write failing backend replay tests**

Add these tests in `tests/test_web_api.py` after `test_submit_build_normalizes_model_override`:

```python
async def test_rebuild_build_replays_live_build_settings():
    class _Studio:
        def __init__(self):
            self.calls = []

        def start(self, brief, slug=None, extra=None):
            self.calls.append({
                "brief": brief,
                "slug": slug,
                "extra": dict(extra or {}),
            })

    studio = _Studio()
    st = _state(studio=studio)
    first = await routes.submit_build(
        st,
        brief="a complete analytics dashboard",
        stack="react",
        slug="analytics-v1",
        build_profile="manual",
        model_override="openrouter/custom-model",
        full_app=True,
    )
    st.builds[first["build_id"]].status = "completed"

    out = await routes.rebuild_build(st, first["build_id"])

    assert out["source_build_id"] == first["build_id"]
    assert out["build_id"] != first["build_id"]
    assert out["reused"] == {
        "stack": "react",
        "build_profile": "manual",
        "model_override": "openrouter/custom-model",
        "slug": "",
    }
    assert len(studio.calls) == 2
    replay = studio.calls[1]
    assert replay["brief"] == "a complete analytics dashboard"
    assert replay["slug"] is None
    assert replay["extra"]["stack"] == "react"
    assert replay["extra"]["build_profile"] == "manual"
    assert replay["extra"]["model_override"] == "openrouter/custom-model"
    assert replay["extra"]["full_app_contract"] is True
    assert st.builds[out["build_id"]].model_trace["full_app"] is True


async def test_rebuild_build_replays_persisted_history_row_with_reuse_slug():
    class _Memory:
        async def get_build(self, build_id):
            return {
                "build_id": build_id,
                "manifest": {
                    "brief": "a finance API with audit logs",
                    "stack": "fastapi",
                    "slug": "finance-api",
                    "extra": {
                        "build_profile": "best_quality",
                        "model_override": "openrouter/history-model",
                        "full_app_contract": True,
                    },
                },
                "model_trace": {"profile": "best_quality"},
                "status": "completed",
            }

    class _Studio:
        def __init__(self):
            self.calls = []

        def start(self, brief, slug=None, extra=None):
            self.calls.append({
                "brief": brief,
                "slug": slug,
                "extra": dict(extra or {}),
            })

    studio = _Studio()
    st = _state(memory=_Memory(), studio=studio)

    out = await routes.rebuild_build(st, "hist1", reuse_slug=True)

    assert out["source_build_id"] == "hist1"
    assert out["reused"] == {
        "stack": "fastapi",
        "build_profile": "best_quality",
        "model_override": "openrouter/history-model",
        "slug": "finance-api",
    }
    replay = studio.calls[0]
    assert replay["brief"] == "a finance API with audit logs"
    assert replay["slug"] == "finance-api"
    assert replay["extra"]["stack"] == "fastapi"
    assert replay["extra"]["build_profile"] == "best_quality"
    assert replay["extra"]["model_override"] == "openrouter/history-model"
    assert replay["extra"]["full_app_contract"] is True


async def test_rebuild_build_rejects_missing_source_brief():
    class _Memory:
        async def get_build(self, build_id):
            return {
                "build_id": build_id,
                "manifest": {"extra": {"build_profile": "manual"}},
                "status": "completed",
            }

    st = _state(memory=_Memory())

    with pytest.raises(ValueError, match="source build has no brief"):
        await routes.rebuild_build(st, "hist-empty")


async def test_rebuild_build_missing_source_raises_keyerror():
    st = _state()

    with pytest.raises(KeyError):
        await routes.rebuild_build(st, "missing-build")
```

- [ ] **Step 2: Run the backend replay tests to confirm failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_web_api.py::test_rebuild_build_replays_live_build_settings \
  tests/test_web_api.py::test_rebuild_build_replays_persisted_history_row_with_reuse_slug \
  tests/test_web_api.py::test_rebuild_build_rejects_missing_source_brief \
  tests/test_web_api.py::test_rebuild_build_missing_source_raises_keyerror \
  -q
```

Expected: at least `test_rebuild_build_replays_live_build_settings` fails because live records do not preserve `full_app` yet, or the tests fail because the replay helpers are not fully implemented in the current checkout.

- [ ] **Step 3: Store `full_app` in live build metadata**

In `skyn3t/web/routes.py`, inside `submit_build(...)`, change the `BuildRecord(..., model_trace={...})` block so it includes `full_app`:

```python
        model_trace={
            "profile": profile,
            "model_override": model,
            "backend": getattr(state.settings, "llm_backend", ""),
            "full_app": full_app_requested,
        },
```

- [ ] **Step 4: Implement or replace replay extraction**

In `skyn3t/web/routes.py`, place this helper after `cancel_build(...)`:

```python
def _build_replay_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Extract the small set of inputs needed to rerun a prior build."""
    manifest = row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
    extra = manifest.get("extra") if isinstance(manifest.get("extra"), dict) else {}
    trace = row.get("model_trace") if isinstance(row.get("model_trace"), dict) else {}
    profile = (
        row.get("build_profile")
        or trace.get("profile")
        or extra.get("build_profile")
        or "cheap_learned"
    )
    normalized_profile = _normalize_build_profile(str(profile))
    model = trace.get("model_override") or extra.get("model_override") or ""
    full_app = bool(
        row.get("full_app")
        or trace.get("full_app")
        or extra.get("full_app_contract")
        or normalized_profile == "full_app"
    )
    return {
        "brief": str(manifest.get("brief") or row.get("brief") or ""),
        "stack": str(manifest.get("stack") or row.get("stack") or ""),
        "slug": str(manifest.get("slug") or row.get("slug") or ""),
        "build_profile": normalized_profile,
        "model_override": _normalize_model_override(str(model)),
        "full_app": full_app,
    }
```

- [ ] **Step 5: Implement or replace `rebuild_build(...)`**

In `skyn3t/web/routes.py`, keep this function directly after `_build_replay_fields(...)`:

```python
async def rebuild_build(
    state: AppState,
    build_id: str,
    *,
    reuse_slug: bool = False,
) -> dict[str, Any]:
    """Rerun a previous build with its brief, stack, profile, and model pin."""
    bid = (build_id or "").strip()
    if not bid:
        raise ValueError("build_id is required")

    rec = state.builds.get(bid)
    row: dict[str, Any] | None = rec.to_dict() if rec is not None else None
    if row is None and state.memory is not None and hasattr(state.memory, "get_build"):
        try:
            row = await state.memory.get_build(bid)
        except Exception:  # noqa: BLE001
            row = None
    if row is None:
        raise KeyError(bid)

    replay = _build_replay_fields(row)
    if not replay["brief"].strip():
        raise ValueError("source build has no brief")

    res = await submit_build(
        state,
        brief=replay["brief"],
        stack=replay["stack"],
        slug=replay["slug"] if reuse_slug else "",
        build_profile=replay["build_profile"],
        model_override=replay["model_override"],
        full_app=replay["full_app"],
    )
    return {
        **res,
        "source_build_id": bid,
        "reused": {
            "stack": replay["stack"],
            "build_profile": replay["build_profile"],
            "model_override": replay["model_override"],
            "slug": replay["slug"] if reuse_slug else "",
        },
    }
```

- [ ] **Step 6: Ensure the route alias exists**

In `build_router(state)` in `skyn3t/web/routes.py`, keep this route after `/builds/cancel` and before `/preview/{slug}`:

```python
    @router.post("/builds/rebuild", dependencies=[auth])
    async def _rebuild_alias(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await rebuild_build(
                state,
                build_id=str(body.get("build_id", "")),
                reuse_slug=bool(body.get("reuse_slug", False)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError:
            raise HTTPException(status_code=404, detail="build not found") from None
```

- [ ] **Step 7: Run focused backend tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_web_api.py::test_rebuild_build_replays_live_build_settings \
  tests/test_web_api.py::test_rebuild_build_replays_persisted_history_row_with_reuse_slug \
  tests/test_web_api.py::test_rebuild_build_rejects_missing_source_brief \
  tests/test_web_api.py::test_rebuild_build_missing_source_raises_keyerror \
  -q
```

Expected: `4 passed`.

- [ ] **Step 8: Commit backend replay contract**

Run:

```bash
git add skyn3t/web/routes.py tests/test_web_api.py
git commit -m "Add Studio build replay contract"
```

Expected: commit succeeds and includes only `skyn3t/web/routes.py` plus `tests/test_web_api.py`.

---

### Task 2: Studio Editable Variant UI

**Files:**
- Modify: `tests/test_web_ui.py`
- Modify: `skyn3t/web/ui/src/routes/Studio.jsx`

**Interfaces:**
- Consumes: build rows from `GET /api/builds`, including `brief`, `stack`, `slug`, `build_profile`, `model_trace`, and persisted `manifest.extra` when present.
- Produces: `rebuildFields(build: object) -> object` in `Studio.jsx`.
- Produces: `loadRebuildVariant(build: object) -> void` in the `Studio` component.
- Produces: `variantSource` state with `{ build_id: string, slug: string, stack: string }`.

- [ ] **Step 1: Write failing UI structural tests**

Add this test after `test_studio_wires_build_profiles_and_manual_model` in `tests/test_web_ui.py`:

```python
def test_studio_rebuild_variants_are_editable_and_diagnostic() -> None:
    studio = (ROUTES / "Studio.jsx").read_text()
    assert "function rebuildFields(build)" in studio
    assert "const [variantSource, setVariantSource]" in studio
    assert "const loadRebuildVariant = (build) =>" in studio
    assert "payload.stack = variantSource.stack" in studio
    assert "Loaded from" in studio
    assert "clear variant" in studio
    assert "buildDiagnostics(b)" in studio
    assert "No recoverable brief" in studio
    assert "apiPost(\"/builds/rebuild\"" not in studio
```

- [ ] **Step 2: Run the UI structural test to confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_ui.py::test_studio_rebuild_variants_are_editable_and_diagnostic -q
```

Expected: FAIL because `variantSource`, `rebuildFields`, rendered diagnostics, or the editable rebuild action is not wired yet.

- [ ] **Step 3: Add replay field extraction helpers to Studio**

In `skyn3t/web/ui/src/routes/Studio.jsx`, add this helper after `buildDiagnostics(build)`:

```jsx
function rebuildFields(build) {
  const manifest = build.manifest && typeof build.manifest === "object" ? build.manifest : {};
  const extra = manifest.extra && typeof manifest.extra === "object" ? manifest.extra : {};
  const trace = build.model_trace || {};
  const profile =
    build.build_profile ||
    trace.profile ||
    extra.build_profile ||
    "cheap_learned";
  return {
    brief: String(manifest.brief || build.brief || ""),
    stack: String(manifest.stack || build.stack || build.stack_selection?.stack || ""),
    slug: String(manifest.slug || build.slug || ""),
    buildProfile: BUILD_PROFILES.some((p) => p.id === profile)
      ? profile
      : "cheap_learned",
    modelOverride: String(trace.model_override || extra.model_override || ""),
    fullApp: Boolean(
      trace.full_app ||
        extra.full_app_contract ||
        profile === "full_app"
    ),
  };
}
```

- [ ] **Step 4: Replace direct rebuild mutation with editable variant state**

In the `Studio` component, add this state near the other `useState(...)` declarations:

```jsx
  const [variantSource, setVariantSource] = useState(null);
```

Remove this mutation block from `Studio.jsx`:

```jsx
  const rebuildBuild = useMutation({
    mutationFn: ({ build_id }) => apiPost("/builds/rebuild", { build_id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["builds"] }),
    onSettled: () => setPendingBuildId(null),
  });
```

- [ ] **Step 5: Add the variant loader**

In the `Studio` component, place this function after `useExample(...)`:

```jsx
  const loadRebuildVariant = (build) => {
    const fields = rebuildFields(build);
    if (!fields.brief.trim()) return;
    setBrief(fields.brief);
    setBuildProfile(fields.buildProfile);
    setFullApp(fields.fullApp);
    setModelOverride(fields.modelOverride);
    setVariantSource({
      build_id: String(build.build_id || ""),
      slug: fields.slug,
      stack: fields.stack,
    });
    clearImage();
    const el = briefRef.current;
    if (el) {
      el.focus();
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };
```

- [ ] **Step 6: Include variant stack in normal build submissions**

In the form `onSubmit` handler in `Studio.jsx`, after the `payload` object is created and before `model_override` is set, add:

```jsx
            if (variantSource?.stack) {
              payload.stack = variantSource.stack;
            }
```

In the `submit` mutation `onSuccess`, add `setVariantSource(null);` so the success block becomes:

```jsx
    onSuccess: () => {
      setBrief("");
      clearImage();
      setVariantSource(null);
      qc.invalidateQueries({ queryKey: ["builds"] });
    },
```

- [ ] **Step 7: Render the variant source pill near the build controls**

In `Studio.jsx`, inside the first `Panel` and after the profile/model/full-app row, render:

```jsx
        {variantSource ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-hairline pt-3 font-mono text-[11px] text-ash">
            <Pill tone="ash">
              variant · {variantSource.stack || "auto stack"}
            </Pill>
            <span>
              Loaded from {variantSource.slug || variantSource.build_id || "previous build"}
            </span>
            <button
              type="button"
              onClick={() => setVariantSource(null)}
              className="btn-ghost py-0.5 text-[10px]"
              title="Return to a new auto-stack build"
            >
              clear variant
            </button>
          </div>
        ) : null}
```

- [ ] **Step 8: Render diagnostics and editable Rebuild action in Recent Builds**

Inside `recentBuilds.map((b) => { ... })`, add these constants near `const ai = aiMeta(b);`:

```jsx
                  const diagnostics = buildDiagnostics(b);
                  const fields = rebuildFields(b);
                  const buildKey = b.build_id || b.slug;
                  const active = ["running", "queued", "pending", "awaiting_approval"].includes(b.status);
```

In the AI `<td>`, after the existing model/skills line, add:

```jsx
                        <div
                          className="max-w-[12rem] truncate text-ash/70"
                          title={diagnostics}
                        >
                          {buildDiagnostics(b)}
                        </div>
```

In the actions `<td>`, replace the status array expression with `active`, replace repeated `(b.build_id || b.slug)` expressions with `buildKey`, and add this non-active branch:

```jsx
                        ) : (
                          <button
                            onClick={() => loadRebuildVariant(b)}
                            disabled={!fields.brief.trim()}
                            className="btn-ghost disabled:opacity-50"
                            title={
                              fields.brief.trim()
                                ? "Load this build into the form for an editable rebuild variant"
                                : "No recoverable brief"
                            }
                          >
                            Rebuild
                          </button>
                        )}
```

The approve/reject/cancel controls should continue to use `buildKey`:

```jsx
                                setPendingBuildId(buildKey);
                                approve.mutate({
                                  build_id: buildKey,
                                  approved: true,
                                  reason: "",
                                });
```

Apply the same `buildKey` substitution to reject and cancel.

- [ ] **Step 9: Run the focused UI structural test**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_ui.py::test_studio_rebuild_variants_are_editable_and_diagnostic -q
```

Expected: `1 passed`.

- [ ] **Step 10: Commit Studio editable variant UI**

Run:

```bash
git add skyn3t/web/ui/src/routes/Studio.jsx tests/test_web_ui.py
git commit -m "Add editable Studio rebuild variants"
```

Expected: commit succeeds and includes only `skyn3t/web/ui/src/routes/Studio.jsx` plus `tests/test_web_ui.py`.

---

### Task 3: Focused Verification And Web Build

**Files:**
- Read: `skyn3t/web/routes.py`
- Read: `skyn3t/web/ui/src/routes/Studio.jsx`
- Read: `tests/test_web_api.py`
- Read: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: backend replay contract from Task 1.
- Consumes: editable variant UI from Task 2.
- Produces: verified working tree with no uncommitted test-only mistakes.

- [ ] **Step 1: Run focused backend and UI tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_web_api.py::test_rebuild_build_replays_live_build_settings \
  tests/test_web_api.py::test_rebuild_build_replays_persisted_history_row_with_reuse_slug \
  tests/test_web_api.py::test_rebuild_build_rejects_missing_source_brief \
  tests/test_web_api.py::test_rebuild_build_missing_source_raises_keyerror \
  tests/test_web_ui.py::test_studio_rebuild_variants_are_editable_and_diagnostic \
  -q
```

Expected: `5 passed`.

- [ ] **Step 2: Run the affected test files**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_api.py tests/test_web_ui.py -q
```

Expected: all tests in both files pass.

- [ ] **Step 3: Build the Vite UI if dependencies are installed**

Run:

```bash
npm run build
```

Working directory: `skyn3t/web/ui`

Expected: Vite exits with code `0` and writes `dist/`. If `node_modules` is missing, record the exact missing-package error in the final report and rely on `tests/test_web_ui.py` for source-level verification.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: no unexpected files. Acceptable changed files for this feature are:

```text
skyn3t/web/routes.py
skyn3t/web/ui/src/routes/Studio.jsx
tests/test_web_api.py
tests/test_web_ui.py
```

- [ ] **Step 5: Commit verification-only cleanup if needed**

If Step 4 shows a small cleanup needed after tests, make that cleanup and run:

```bash
git add skyn3t/web/routes.py skyn3t/web/ui/src/routes/Studio.jsx tests/test_web_api.py tests/test_web_ui.py
git commit -m "Verify Studio rebuild variants"
```

Expected: commit is only needed if Task 3 changed tracked files. If Task 3 only ran tests, do not create an empty commit.
