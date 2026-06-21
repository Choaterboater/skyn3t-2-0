# Improve Engine — Implementation Plan (Spec 3, Slice 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A headless `skyn3t studio improve <project> --goal "..."` that loads an already-built project, improves it toward a goal (reusing `code_improver`/`repo_map`/`proof_run`), verifies it still works, and delivers the change back — emitting `IMPROVE_*` events so the future cockpit can render it.

**Architecture:** A standalone `ImproveEngine` (mirrors `StudioRunner`'s deps) runs an existing project through: resolve+guard → seed a worktree from the delivered files → repo-map context → `code_improver` toward the goal → `proof_run` → `merge_back(clean=True)` → record `manifest.extra["improve_history"]`. The CLI `_run_improve` mirrors `_run_build`'s dependency wiring. This is Spec 3 Layer A (engine only); the live app runner, visual self-inspection loop, and two-pane cockpit are later slices.

**Tech Stack:** Python 3.11+, pytest, Typer, existing `skyn3t.*` (worktree, manifest, stack_detector, rag.repo_map, agents.code_improver via orchestrator, studio.proof_run, core.events).

## Global Constraints

- Python 3.11+. Offline-first tests: no network, no real LLM — mock `orchestrator.submit`; `proof_run` runs in static mode (`run_tests=False, run_build=False`) so no subprocess.
- **Never raise into a half-improved delivery:** the engine wraps the work in `try/finally` and ALWAYS `cleanup_worktree`s; a failure returns `status="failed"`, never a partial `merge_back`. The original project is only overwritten by a successful `merge_back(clean=True)`.
- **Path safety:** a slug must resolve INSIDE `settings.projects_dir` (resolve + `is_relative_to`); reject traversal. An absolute path to an existing dir is also accepted.
- Reuse, don't reinvent: `create_worktree`/`merge_back`/`cleanup_worktree` (worktree.py), `BuildManifest.load/.save` (manifest.py), `StackDetector.detect` (stack_detector.py), `get_repo_map` (rag/repo_map.py), `proof_run` (studio/proof_run.py), `code_improver` via `orchestrator.submit(TaskRequest(type="code_improver", ...))`.
- Exact reused contracts:
  - `create_worktree(base_dir, slug, *, worktrees_root=None) -> Worktree` (`.dir` is the str path).
  - `merge_back(worktree_dir, project_dir, *, overwrite=True, clean=False) -> list[str]` (used BOTH to seed the worktree from the project and to deliver back).
  - `get_repo_map(directory: str, max_tokens: int = 2000) -> str`.
  - `proof_run(project_dir, *, checklist=None, execution_backend="auto", stack="", run_tests=False, test_timeout=90, run_build=False, build_timeout=300) -> ProofResult` (`.passed`, `.score`, `.missing`).
  - `TaskRequest(type="code_improver", payload={worktree_dir, brief, slug, stack, gaps, repo_map}, capabilities_required=("code_improve",), correlation_id=...)`; `orchestrator.submit(task) -> TaskResult` (`.success`, `.output{files, files_improved, backend}`).
  - `EventBus.emit(type: EventType, source: str, payload: dict, correlation_id: str|None)`.
  - `StudioRunner.__init__(event_bus, orchestrator, *, settings, memory, ..., skills, rag, ...)`.
- Suite baseline (this branch, post Spec 1 merge): **416 pass / 2 skip**. Run `python3 -m pytest -q` after each task; stay green, no new warnings.
- Commit after every task.

## File Structure

- Create `skyn3t/studio/improve.py` — `ImproveEngine`, `ImproveOutcome`.
- Modify `skyn3t/core/events.py` — add `IMPROVE_*` `EventType` members.
- Modify `skyn3t/cli/main.py` — `studio improve` command + `_run_improve` helper.
- Create `tests/test_improve_engine.py`, `tests/test_cli_improve.py`.

---

### Task 1: ImproveEngine + IMPROVE_* events

**Files:**
- Create: `skyn3t/studio/improve.py`
- Modify: `skyn3t/core/events.py` (add enum members)
- Test: `tests/test_improve_engine.py`

**Interfaces:**
- Produces: `class ImproveEngine` with
  `async def improve(self, project: str, goal: str, *, correlation_id: str | None = None) -> ImproveOutcome`
  and `__init__(self, event_bus, orchestrator, *, settings=None, memory=None, skills=None, rag=None)`.
- Produces: `@dataclass ImproveOutcome(project_dir, slug, stack, goal, files_changed: list[str], proof_passed: bool, score: float, status: str, detail: dict)` with `.to_dict()`.
- Consumes: the reused contracts in Global Constraints.

- [ ] **Step 1: Add IMPROVE_* event members**

In `skyn3t/core/events.py`, in the `EventType` enum, after the `STAGE_ARTIFACT_SNAPSHOT` / build members add:
```python
    # Operator mode: improve an already-built project
    IMPROVE_STARTED = "improve.started"
    IMPROVE_STAGE = "improve.stage"
    IMPROVE_COMPLETED = "improve.completed"
    IMPROVE_FAILED = "improve.failed"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_improve_engine.py
"""Offline tests for the headless improve engine. No network/LLM: the
orchestrator is faked; proof_run runs in static mode."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.studio.improve import ImproveEngine, ImproveOutcome


class _FakeOrchestrator:
    """Records the submitted task and returns a successful improver result."""
    def __init__(self):
        self.submitted = []

    async def submit(self, task):
        self.submitted.append(task)
        # simulate the improver touching main.py
        wt = Path(task.payload["worktree_dir"])
        (wt / "main.py").write_text("print('improved')\n")
        return SimpleNamespace(success=True, output={"files": ["main.py"], "backend": "stub"})


def _settings(tmp_path: Path):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        projects_dir=projects,
        execution_backend="inline",
        run_generated_tests=False,
        run_generated_build=False,
        generated_test_timeout=90,
        generated_build_timeout=300,
    )


def _seed_project(projects: Path, slug: str) -> Path:
    d = projects / slug
    d.mkdir(parents=True)
    (d / "main.py").write_text("print('original')\n")
    (d / "README.md").write_text("# demo\n")
    import json
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "brief": "demo", "stack": "python", "status": "completed"}))
    return d


def test_improve_delivers_change_and_records_history(tmp_path):
    settings = _settings(tmp_path)
    project = _seed_project(settings.projects_dir, "demo")
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)

    outcome = asyncio.run(engine.improve("demo", "make it say improved"))

    assert isinstance(outcome, ImproveOutcome)
    assert outcome.status == "completed"
    assert "main.py" in outcome.files_changed
    # delivered back to the real project dir
    assert (project / "main.py").read_text() == "print('improved')\n"
    # history recorded in the manifest
    import json
    man = json.loads((project / "skyn3t_manifest.json").read_text())
    assert man["extra"]["improve_history"][-1]["goal"] == "make it say improved"
    # no leftover worktree
    wt_root = settings.projects_dir.parent / ".skyn3t_worktrees"
    assert not any(p.name.startswith("improve-demo-") for p in wt_root.iterdir()) if wt_root.exists() else True


def test_improve_rejects_slug_traversal(tmp_path):
    settings = _settings(tmp_path)
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    with pytest.raises(ValueError):
        asyncio.run(engine.improve("../secrets", "x"))


def test_improve_missing_project_fails_cleanly(tmp_path):
    settings = _settings(tmp_path)
    engine = ImproveEngine(EventBus(), _FakeOrchestrator(), settings=settings)
    with pytest.raises(FileNotFoundError):
        asyncio.run(engine.improve("nope", "x"))


def test_improve_emits_lifecycle_events(tmp_path):
    settings = _settings(tmp_path)
    _seed_project(settings.projects_dir, "demo")
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.ALL, lambda ev: seen.append(ev.type))
    engine = ImproveEngine(bus, _FakeOrchestrator(), settings=settings)
    asyncio.run(engine.improve("demo", "g"))
    assert EventType.IMPROVE_STARTED in seen and EventType.IMPROVE_COMPLETED in seen
```

(If `EventBus.subscribe`/`EventType.ALL` differ in this codebase, adapt the event-assertion test to the real subscribe API — check `core/events.py`.)

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_improve_engine.py -v`
Expected: FAIL — `skyn3t.studio.improve` missing.

- [ ] **Step 4: Implement `skyn3t/studio/improve.py`**

```python
# skyn3t/studio/improve.py
"""Headless 'improve an existing project' engine (Spec 3, Layer A).

Loads an already-delivered project, runs the code_improver toward a goal in an
isolated worktree, verifies with proof_run, and delivers the change back —
never leaving a partial result. Emits IMPROVE_* events for the cockpit."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.agents.stack_detector import StackDetector
from skyn3t.config.settings import get_settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus, EventType
from skyn3t.rag.repo_map import get_repo_map
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.proof_run import proof_run
from skyn3t.worktree import cleanup_worktree, create_worktree, merge_back

import structlog

_log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ImproveOutcome:
    project_dir: str
    slug: str
    stack: str
    goal: str
    files_changed: list[str] = field(default_factory=list)
    proof_passed: bool = False
    score: float = 0.0
    status: str = "completed"  # completed | failed
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImproveEngine:
    """Improve an existing project toward a goal. Mirrors StudioRunner's deps."""

    def __init__(self, event_bus: EventBus, orchestrator: Any, *,
                 settings: Any | None = None, memory: Any | None = None,
                 skills: Any | None = None, rag: Any | None = None) -> None:
        self.event_bus = event_bus
        self.orchestrator = orchestrator
        self.settings = settings or get_settings()
        self.memory = memory
        self.skills = skills
        self.rag = rag

    def _resolve_project(self, project: str) -> Path:
        projects_root = Path(self.settings.projects_dir).resolve()
        cand = Path(project)
        if cand.is_absolute():
            resolved = cand.resolve()
        else:
            resolved = (projects_root / project).resolve()
            if not resolved.is_relative_to(projects_root):
                raise ValueError(f"project escapes projects_dir: {project!r}")
        if not resolved.is_dir():
            raise FileNotFoundError(f"no project at {resolved}")
        return resolved

    async def _emit(self, etype: EventType, payload: dict[str, Any], cid: str) -> None:
        try:
            await self.event_bus.emit(etype, "improve", payload, correlation_id=cid)
        except Exception as exc:  # noqa: BLE001 - never let events break a run
            if _log:
                _log.warning("improve.emit_failed", error=str(exc))

    async def improve(self, project: str, goal: str, *,
                      correlation_id: str | None = None) -> ImproveOutcome:
        project_dir = self._resolve_project(project)
        cid = correlation_id or uuid.uuid4().hex
        manifest = BuildManifest.load(project_dir)
        slug = manifest.slug if manifest else project_dir.name
        stack = (manifest.stack if manifest and manifest.stack
                 else StackDetector.detect(project_dir))
        await self._emit(EventType.IMPROVE_STARTED,
                         {"slug": slug, "stack": stack, "goal": goal,
                          "project_dir": str(project_dir)}, cid)

        wt = create_worktree(str(self.settings.projects_dir), f"improve-{slug}")
        try:
            # Seed the worktree with the existing project files.
            merge_back(str(project_dir), wt.dir, overwrite=True, clean=False)
            repo_ctx = get_repo_map(wt.dir, max_tokens=2000)
            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "localize",
                              "repo_map_chars": len(repo_ctx)}, cid)

            files_changed = await self._run_improver(wt.dir, slug, stack, goal, repo_ctx, cid)

            proof = proof_run(
                wt.dir, stack=stack,
                execution_backend=getattr(self.settings, "execution_backend", "auto"),
                run_tests=bool(getattr(self.settings, "run_generated_tests", False)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", False)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            delivered = merge_back(wt.dir, str(project_dir), overwrite=True, clean=True)
            self._record_history(manifest, project_dir, goal, delivered, proof, stack, slug)

            outcome = ImproveOutcome(
                project_dir=str(project_dir), slug=slug, stack=stack, goal=goal,
                files_changed=sorted(files_changed), proof_passed=bool(proof.passed),
                score=float(proof.score), status="completed",
                detail={"delivered": len(delivered), "proof": proof.to_dict()},
            )
            await self._emit(EventType.IMPROVE_COMPLETED, outcome.to_dict(), cid)
            return outcome
        except Exception as exc:  # noqa: BLE001 - report failure, never a partial deliver
            await self._emit(EventType.IMPROVE_FAILED,
                             {"slug": slug, "goal": goal, "error": str(exc)}, cid)
            return ImproveOutcome(project_dir=str(project_dir), slug=slug, stack=stack,
                                  goal=goal, status="failed", detail={"error": str(exc)})
        finally:
            cleanup_worktree(wt)

    async def _run_improver(self, worktree_dir: str, slug: str, stack: str,
                            goal: str, repo_ctx: str, cid: str) -> list[str]:
        task = TaskRequest(
            type="code_improver",
            payload={"worktree_dir": worktree_dir, "brief": goal, "slug": slug,
                     "stack": stack, "gaps": [goal], "repo_map": repo_ctx},
            capabilities_required=("code_improve",),
            correlation_id=cid,
        )
        result = await self.orchestrator.submit(task)
        if result and getattr(result, "success", False):
            return list((getattr(result, "output", None) or {}).get("files", []))
        return []

    def _record_history(self, manifest: BuildManifest | None, project_dir: Path,
                        goal: str, delivered: list[str], proof: Any,
                        stack: str, slug: str) -> None:
        man = manifest or BuildManifest(slug=slug, brief="", stack=stack, status="completed")
        hist = man.extra.setdefault("improve_history", [])
        hist.append({"goal": goal, "files": len(delivered),
                     "proof_passed": bool(proof.passed), "score": float(proof.score)})
        man.touch()
        man.save(project_dir)
```

NOTE: imports are confirmed — `TaskRequest` is in `skyn3t.core.agent`; the logger is `structlog`. `EventType.ALL` (`"*"`) and `EventBus.subscribe(event_type, handler)` both exist (core/events.py). If `subscribe` requires an *async* handler, make the event-assertion test's handler `async def`; the engine itself only ever calls `event_bus.emit`, which is confirmed.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_improve_engine.py -v`
Expected: PASS (4). Fix the two imports if an ImportError appears.

- [ ] **Step 6: Run the suite + commit**

Run: `python3 -m pytest -q` (expect 420 pass / 2 skip).
```bash
git add skyn3t/studio/improve.py skyn3t/core/events.py tests/test_improve_engine.py
git commit -m "feat: headless ImproveEngine — improve an existing project toward a goal"
```

---

### Task 2: `studio improve` CLI + `_run_improve`

**Files:**
- Modify: `skyn3t/cli/main.py` (command + helper)
- Test: `tests/test_cli_improve.py`

**Interfaces:**
- Consumes: `ImproveEngine` (Task 1); `_assemble_spine()`, `_build_intelligence()`, `_build_observability()` (existing, used by `_run_build`).
- Produces: `async def _run_improve(project: str, *, goal: str) -> dict[str, Any] | None` and a `studio improve` Typer command.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_improve.py
"""The `studio improve` CLI wires the spine to ImproveEngine and prints a result."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from skyn3t.cli import main as cli


def test_run_improve_returns_outcome(tmp_path, monkeypatch):
    projects = tmp_path / "Projects"; projects.mkdir()
    proj = projects / "demo"; proj.mkdir()
    (proj / "main.py").write_text("print('x')\n")
    (proj / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": "demo", "brief": "d", "stack": "python", "status": "completed"}))

    settings = SimpleNamespace(projects_dir=projects, execution_backend="inline",
                               run_generated_tests=False, run_generated_build=False,
                               generated_test_timeout=90, generated_build_timeout=300)

    class _Orch:
        async def submit(self, task):
            return SimpleNamespace(success=True, output={"files": []})

    from skyn3t.core.events import EventBus
    monkeypatch.setattr(cli, "_assemble_spine", lambda: _fake_spine(settings, _Orch(), EventBus()))
    monkeypatch.setattr(cli, "_build_intelligence", lambda *a, **k: (None, None, None, None))
    monkeypatch.setattr(cli, "_build_observability", lambda *a, **k: (None, None))

    out = asyncio.run(cli._run_improve("demo", goal="add a docstring"))
    assert out is not None and out["status"] == "completed" and out["slug"] == "demo"


async def _fake_spine(settings, orch, bus):
    return {"settings": settings, "event_bus": bus, "orchestrator": orch,
            "llm": None, "router": None, "memory": None}
```

(`_assemble_spine` is async in `_run_build`; if so, make the monkeypatch return the coroutine — adapt to its real shape, which Task 2's implementer must read from `cli/main.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cli_improve.py -v`
Expected: FAIL — `_run_improve` not defined.

- [ ] **Step 3: Implement `_run_improve` + the command**

In `skyn3t/cli/main.py`, add the helper (mirror `_run_build`'s wiring, but construct `ImproveEngine` and call `.improve`):
```python
async def _run_improve(project: str, *, goal: str) -> dict[str, Any] | None:
    from skyn3t.studio.improve import ImproveEngine
    spine = await _assemble_spine()
    settings = spine["settings"]
    _learning, _patterns, skills, rag = _build_intelligence(settings, spine["event_bus"], spine["memory"])
    engine = ImproveEngine(
        spine["event_bus"], spine["orchestrator"],
        settings=settings, memory=spine["memory"], skills=skills, rag=rag,
    )
    outcome = await engine.improve(project, goal)
    return outcome.to_dict()
```
And the command (near `studio_build`):
```python
@studio_app.command("improve")
def studio_improve(
    project: str = typer.Argument(..., help="Project slug (under Projects/) or an absolute path."),
    goal: str = typer.Option(..., "--goal", "-g", help="What to add/change, in plain English."),
) -> None:
    """Improve an already-built project toward a goal (audit -> edit -> verify -> deliver)."""
    console = _console()
    outcome = asyncio.run(_run_improve(project, goal=goal))
    if outcome is None:
        console.print("[red]Improve pipeline unavailable (studio package missing).[/red]")
        raise typer.Exit(code=1)
    color = "green" if outcome.get("proof_passed") else "yellow"
    table = _table("Improve result", ["field", "value"])
    table.add_row("slug", str(outcome.get("slug", "")))
    table.add_row("stack", str(outcome.get("stack", "")))
    table.add_row("goal", str(outcome.get("goal", "")))
    table.add_row("status", str(outcome.get("status", "")))
    table.add_row("files_changed", str(len(outcome.get("files_changed", []))))
    table.add_row("proof", f"[{color}]{'passed' if outcome.get('proof_passed') else 'check'}[/{color}]")
    table.add_row("score", str(outcome.get("score", "")))
    table.add_row("project", str(outcome.get("project_dir", "")))
    console.print(table)
    if outcome.get("status") != "completed":
        raise typer.Exit(code=2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_cli_improve.py -v`
Expected: PASS.

- [ ] **Step 5: Run the suite + commit**

Run: `python3 -m pytest -q` (expect 421 pass / 2 skip).
```bash
git add skyn3t/cli/main.py tests/test_cli_improve.py
git commit -m "feat: studio improve CLI — drive ImproveEngine on an existing project"
```

---

## Self-Review

**Spec coverage (Spec 3 Layer A — engine only):**
- Resolve+guard an existing project → Task 1 `_resolve_project` ✓
- Worktree-isolated, never-partial-deliver (try/finally + clean=True only on success) → Task 1 ✓
- repo-map context + code_improver toward goal + proof_run verify → Task 1 ✓
- `improve_history` recorded in the manifest → Task 1 ✓
- IMPROVE_* events for the future cockpit → Task 1 ✓
- `studio improve <project> --goal` CLI → Task 2 ✓

**Deferred to later Spec 3 slices (NOT in this plan):** live app runner (run the real app), visual self-inspection loop (Playwright screenshot → vision → iterate), structured search/replace diffs + localize step, the two-pane interactive cockpit + `/api/improve`, session/undo. This slice uses the existing whole-file `code_improver`; the structured-diff localize is a refinement slice.

**Placeholder scan:** none — real code throughout. Two imports (`TaskRequest`, `get_logger`) are flagged for the implementer to confirm against the codebase (grep `class TaskRequest`) before running; that is a verification step, not a placeholder.

**Type consistency:** `ImproveOutcome` fields used identically in engine + CLI + tests; `improve(project, goal, *, correlation_id)` signature consistent across Task 1 and Task 2's `_run_improve`.
