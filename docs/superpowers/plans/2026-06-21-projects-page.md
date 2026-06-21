# Projects Page — Implementation Plan (Cockpit GUI)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A cockpit **Projects** page to list every delivered project, delete one (to trash), and run cleanup — surfacing the shipped cleanup backend in the GUI. Plus approve/reject buttons in the Studio builds table (route already exists).

**Architecture:** Two new backend routes (`GET /api/projects` walks `projects_dir` reading manifests; `DELETE /api/projects/{slug}` trashes a dir, reusing cleanup's trash + running-build guard) + one new React route (`routes/Projects.jsx`) wired into `App.jsx`, composing existing `ui.jsx` primitives and the `api.js` + react-query patterns. Frontend has no unit-test infra — it's verified by `npm run build` (compile) + reviewer reading the JSX.

**Tech Stack:** Python/FastAPI + pytest (backend); React 18 + Vite + Tailwind + @tanstack/react-query (frontend); `npm` available at `/opt/homebrew/bin`.

## Global Constraints

- Backend: never raise into a 500 for expected cases — validate slug (traversal guard: resolve + `is_relative_to(projects_dir)`, reject `==` root), 404 missing, 400 invalid/active. Reuse `skyn3t/studio/cleanup.py` `_load_manifest`/`_dir_size` and the trash-move pattern from `cleanup.apply`. NEVER delete a running build's project (`{r.slug for r in state.builds.values() if r.status=="running"}`).
- Backend tests offline (pytest, tmp dirs, monkeypatch `state.settings.projects_dir`); mirror `tests/test_cleanup.py` + the web-route test patterns.
- Frontend: match the EXISTING patterns exactly — `App.jsx` NAV array + lazy `import` + `<Route>`; `api.js` `apiFetch`/`apiPost`/`queryFn`; `@tanstack/react-query` `useQuery`/`useMutation` + `queryClient.invalidateQueries`; the Tailwind theme classes (`bone`, `ash`, `ember`, `plasma`, `panel`, `hairline`, `badge`, `eyebrow`, `nav-link`) and the `components/ui.jsx` primitives (read them: `PageHeader`/`Panel`/`Pill`/`Stat`/`Empty` or whatever exists). Do NOT introduce new deps or a new styling system.
- `GET /api/projects` returns `{projects:[{slug,stack,status,verdict,score,created_at,updated_at,size_bytes,has_preview,has_manifest}]}`.
- `DELETE /api/projects/{slug}` returns `{slug, trashed_to}`.
- Route ordering in `routes.py`: register exact `/projects` and `/projects/cleanup` and `DELETE /projects/{slug}` BEFORE the wildcard `GET /projects/{slug}/{path:path}`.
- Suite baseline (this branch, off main): **447 pass / 2 skip**. `python3 -m pytest -q` after each backend task; stay green. After the frontend task, `cd skyn3t/web/ui && npm run build` must succeed.
- Activation note (for the human, not a task): the new dist + backend routes need a `:6660` server restart to serve.
- Commit after every task.

## File Structure

- Modify `skyn3t/web/routes.py` — `list_projects`, `delete_project` + their `@router.get/delete` handlers.
- Create `tests/test_projects_routes.py`.
- Create `skyn3t/web/ui/src/routes/Projects.jsx`.
- Modify `skyn3t/web/ui/src/App.jsx` — NAV item + lazy import + Route.
- Modify `skyn3t/web/ui/src/routes/Studio.jsx` — approve/reject buttons.
- Rebuild `skyn3t/web/ui/dist/` via `npm run build` (tracked? confirm — if dist is gitignored, note it).

---

### Task 1: Backend — `GET /api/projects` + `DELETE /api/projects/{slug}`

**Files:**
- Modify: `skyn3t/web/routes.py`
- Test: `tests/test_projects_routes.py`

**Interfaces:**
- Produces: `async list_projects(state) -> {"projects":[...]}`; `async delete_project(state, slug) -> {"slug","trashed_to"}` (raises `ValueError` on bad/active slug, `FileNotFoundError` on missing).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_projects_routes.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.web.routes import delete_project, list_projects


def _state(tmp_path, builds=None):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        settings=SimpleNamespace(projects_dir=projects),
        builds=builds or {},
    )


def _project(root, slug, *, status="completed", score=92.0):
    d = root / slug
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "stack": "python", "status": status, "verdict": "go",
         "score": score, "created_at": "2026-06-21T00:00:00+00:00"}))
    (d / "main.py").write_text("print('x')\n")
    return d


def test_list_projects_reads_manifests(tmp_path):
    state = _state(tmp_path)
    _project(state.settings.projects_dir, "alpha")
    _project(state.settings.projects_dir, "beta", status="failed", score=10.0)
    (state.settings.projects_dir / "no-manifest").mkdir()  # orphan dir still listed
    out = asyncio.run(list_projects(state))
    rows = {p["slug"]: p for p in out["projects"]}
    assert rows["alpha"]["status"] == "completed" and rows["alpha"]["score"] == 92.0
    assert rows["alpha"]["size_bytes"] > 0 and rows["alpha"]["has_manifest"] is True
    assert rows["no-manifest"]["has_manifest"] is False
    assert "beta" in rows and rows["beta"]["status"] == "failed"


def test_delete_project_moves_to_trash(tmp_path):
    state = _state(tmp_path)
    proj = _project(state.settings.projects_dir, "gamma")
    out = asyncio.run(delete_project(state, "gamma"))
    assert not proj.exists()
    trash = state.settings.projects_dir.parent / ".skyn3t_trash"
    assert Path(out["trashed_to"]).exists() and trash in Path(out["trashed_to"]).parents


def test_delete_project_rejects_traversal(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "../secrets"))


def test_delete_project_missing_is_filenotfound(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(FileNotFoundError):
        asyncio.run(delete_project(state, "nope"))


def test_delete_project_refuses_running_build(tmp_path):
    state = _state(tmp_path, builds={"b1": SimpleNamespace(slug="live", status="running")})
    _project(state.settings.projects_dir, "live")
    with pytest.raises(ValueError):
        asyncio.run(delete_project(state, "live"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_projects_routes.py -v`
Expected: FAIL — `list_projects`/`delete_project` not importable.

- [ ] **Step 3: Implement the backend functions + routes**

In `skyn3t/web/routes.py`, add the backend-agnostic functions (near `list_builds`, ~line 162):
```python
async def list_projects(state: AppState) -> dict[str, Any]:
    from skyn3t.studio.cleanup import _dir_size, _load_manifest
    pdir = Path(state.settings.projects_dir)
    out: list[dict[str, Any]] = []
    if pdir.is_dir():
        for d in sorted(p for p in pdir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            man = _load_manifest(d)
            m = man or {}
            out.append({
                "slug": m.get("slug", d.name),
                "stack": m.get("stack", ""),
                "status": m.get("status", ""),
                "verdict": m.get("verdict", ""),
                "score": m.get("score", 0.0),
                "created_at": m.get("created_at", ""),
                "updated_at": m.get("updated_at", ""),
                "size_bytes": _dir_size(d),
                "has_preview": (d / "index.html").exists(),
                "has_manifest": man is not None,
            })
    return {"projects": out}


async def delete_project(state: AppState, slug: str) -> dict[str, Any]:
    import shutil
    projects_root = Path(state.settings.projects_dir).resolve()
    target = (projects_root / slug).resolve()
    if target == projects_root or not target.is_relative_to(projects_root):
        raise ValueError(f"invalid slug: {slug!r}")
    if not target.is_dir():
        raise FileNotFoundError(slug)
    active = {getattr(r, "slug", "") for r in state.builds.values()
              if getattr(r, "status", "") == "running"}
    if target.name in active or slug in active:
        raise ValueError("project belongs to a running build")
    trash = projects_root.parent / ".skyn3t_trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / target.name
    n = 1
    while dest.exists():
        dest = trash / f"{target.name}.{n}"
        n += 1
    shutil.move(str(target), str(dest))
    return {"slug": slug, "trashed_to": str(dest)}
```
Then add the routes inside `build_router`, **before** the `GET /projects/{slug}/{path:path}` wildcard (~line 735):
```python
    @router.get("/projects", dependencies=[auth])
    async def _projects() -> dict[str, Any]:
        return await list_projects(state)

    @router.delete("/projects/{slug}", dependencies=[auth])
    async def _delete_project(slug: str) -> dict[str, Any]:
        try:
            return await delete_project(state, slug)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid or active project")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found")
```

- [ ] **Step 4: Run tests + suite + commit**

Run: `python3 -m pytest tests/test_projects_routes.py -v && python3 -m pytest -q` (expect 452 pass / 2 skip).
```bash
git add skyn3t/web/routes.py tests/test_projects_routes.py
git commit -m "feat: GET /api/projects (list) + DELETE /api/projects/{slug} (trash)"
```

---

### Task 2: Frontend — Projects page + cleanup panel + delete + approve/reject buttons

**Files:**
- Create: `skyn3t/web/ui/src/routes/Projects.jsx`
- Modify: `skyn3t/web/ui/src/App.jsx` (NAV + lazy import + Route)
- Modify: `skyn3t/web/ui/src/routes/Studio.jsx` (approve/reject buttons)
- Build: `cd skyn3t/web/ui && npm run build`

**Read first (mirror these patterns — do NOT invent new ones):**
- `skyn3t/web/ui/src/App.jsx` — the `NAV` array (add `{ to: "/projects", label: "Projects", glyph: "▤" }`), the `lazy(() => import("./routes/Projects.jsx"))`, and the `<Route path="/projects" element={<Projects stream={stream} />} />`.
- `skyn3t/web/ui/src/api.js` — `apiFetch`, `apiPost`, `queryFn`. For DELETE use `apiFetch(\`/projects/${slug}\`, { method: "DELETE" })`.
- `skyn3t/web/ui/src/components/ui.jsx` — reuse its exported primitives (PageHeader/Panel/Pill/Stat/Empty or equivalents). READ it to learn the exact names + props.
- `skyn3t/web/ui/src/routes/Studio.jsx` — copy its `useQuery` (react-query) + `useMutation` + `queryClient.invalidateQueries` + table render patterns; this is the template for Projects.jsx and the home of the approve/reject buttons.
- `skyn3t/web/ui/src/routes/Cortex.jsx` — its approve/reject "decide" mutation is the closest analog for the Studio approve/reject buttons.

**Requirements:**
1. **`Projects.jsx`** — `useQuery(["projects"], queryFn("/projects"))` → a table/grid of projects (slug, stack, status as a Pill, score, size as MB, a "preview" link to `/api/projects/{slug}/index.html` when `has_preview`). Use `Empty` when none.
2. **Cleanup panel** on the page: a "Scan" button → `apiFetch("/projects/cleanup")` (GET dry-run) shows the 5 categories + total `size_bytes`; an "Apply" button → `apiPost("/projects/cleanup", { dry_run: false })` then `invalidateQueries(["projects"])`. Show "would free X MB" vs "freed X MB".
3. **Per-row Delete** → `useMutation` calling `apiFetch(\`/projects/${slug}\`, { method: "DELETE" })`, then `invalidateQueries(["projects"])`. Confirm before firing (a simple window.confirm or an inline confirm state).
4. **App.jsx**: add the NAV item, lazy import, and Route (match the existing 8 exactly).
5. **Studio.jsx approve/reject**: in the recent-builds table, for builds whose status indicates a pending gate (or unconditionally, best-effort), add Approve/Reject buttons → `apiPost("/studio/approve", { build_id, approved: true|false, reason: "" })` then invalidate the builds query. Mirror Cortex's decide mutation.

- [ ] **Step 1: Read the pattern files** (App.jsx, api.js, components/ui.jsx, routes/Studio.jsx, routes/Cortex.jsx). Note the exact primitive names, the react-query client usage, and the Tailwind classes in use.

- [ ] **Step 2: Create `Projects.jsx`** per Requirements 1–3, composing the real `ui.jsx` primitives + `api.js` helpers + react-query patterns you just read. Keep it one focused component file.

- [ ] **Step 3: Wire `App.jsx`** (Requirement 4) — NAV item + lazy import + Route.

- [ ] **Step 4: Add approve/reject to `Studio.jsx`** (Requirement 5).

- [ ] **Step 5: Build to verify it compiles**

Run: `cd skyn3t/web/ui && npm run build`
Expected: a clean Vite build (exit 0), no errors. Fix any compile/JSX errors until it builds. (If `npm install` is needed first — no `node_modules` — run it once.)

- [ ] **Step 6: Confirm the backend suite is still green**

Run: `python3 -m pytest -q` (still 452 pass / 2 skip — the frontend doesn't touch Python).

- [ ] **Step 7: Commit**

```bash
git add skyn3t/web/ui/src/routes/Projects.jsx skyn3t/web/ui/src/App.jsx skyn3t/web/ui/src/routes/Studio.jsx skyn3t/web/ui/dist
git commit -m "feat: cockpit Projects page (list/delete/cleanup) + Studio approve/reject buttons"
```
(If `skyn3t/web/ui/dist` is gitignored, force-add it like the other tracked UI assets, or note that the human must `npm run build` after pulling.)

---

## Self-Review

**Coverage:** GET /api/projects (list) → Task 1 ✓; DELETE /api/projects/{slug} (trash, guarded) → Task 1 ✓; Projects page (list) → Task 2 ✓; cleanup panel (existing API) → Task 2 ✓; per-row delete → Task 2 ✓; approve/reject buttons (existing API) → Task 2 ✓; nav/route wiring → Task 2 ✓.

**Deferred (not this slice):** improve/serve/shoot GUI surfaces (need new backend routes wrapping ImproveEngine/AppRunner — their own slice); live (non-static) preview.

**Placeholder scan:** backend has real code; the frontend task is intentionally pattern-directed (the implementer reads the actual `ui.jsx`/`Studio.jsx` and matches them) because the React components depend on primitives + Tailwind classes that must be mirrored, not invented — `npm run build` is the objective gate.

**Consistency:** `GET /api/projects` shape + `DELETE` return are used identically by the backend tests and the Projects.jsx fetches; the cleanup panel reuses the already-shipped `/api/projects/cleanup` report shape.
