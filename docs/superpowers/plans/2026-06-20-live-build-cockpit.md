# Live Build Cockpit (Phase A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SkyN3t debug each build stage autonomously (verify→fix→re-check) before the next, and stream that — plus a live file/preview of the app — to the dashboard with no human prompts.

**Architecture:** A new `debug_stage` pass runs inside the existing `StudioRunner` stage loop, reusing `proof_run` + the `code_improver` agent. It emits four new events and snapshots the in-progress worktree to `Projects/<slug>/.preview/`. Two new read-only API routes serve that preview, and new event-driven panels in the existing `Studio.jsx` render the debug timeline, files-so-far, and a preview. Phases B (learning) and C (mobile) are out of scope.

**Tech Stack:** Python 3.11, pytest (sync wrappers around `asyncio.run`, offline stub backend), FastAPI (guarded optional dep), React 18 + Vite + Tailwind (`skyn3t/web/ui`).

## Global Constraints

- Offline-first: every Python test runs on `Settings(llm_backend="stub")` — no API keys, no Docker, no network.
- Event values are **lowercase-dotted** strings (e.g. `build.stage.debug.started`), never the enum NAME.
- New events must round-trip the **enum → `ConnectionHub` wrap (`{"event": …}`) → SPA unwrap** contract; a contract test guards it (the `5029b10` bug class).
- Budget: the per-stage loop relies on the existing hard backstop (`LLMClient.budget` → `BudgetExceeded`) and `per_build_usd_cap=0.50`; it adds no new spend path beyond bounded `code_improver` calls.
- Security: preview routes are auth-gated via the existing `require_auth` dependency, honor loopback-only posture, serve **nothing** outside `Projects/<slug>/`, and reject path traversal.
- `Projects/` is a sibling of the repo (`REPO_ROOT.parent / "Projects"`), so `.preview/` is **not** tracked by git — no `.gitignore` change needed.
- Degrade, don't crash: missing FastAPI, Docker, or `code_improve` capability each degrade to a working subset, never an exception.

---

### Task 1: New debug/snapshot event types + contract test

**Files:**
- Modify: `skyn3t/core/events.py:50-55` (the `# Build pipeline` block of `EventType`)
- Test: `tests/test_stage_debug_events.py`

**Interfaces:**
- Produces: `EventType.STAGE_DEBUG_STARTED` (`"build.stage.debug.started"`), `EventType.STAGE_DEBUG_ATTEMPT` (`"build.stage.debug.attempt"`), `EventType.STAGE_DEBUG_RESOLVED` (`"build.stage.debug.resolved"`), `EventType.STAGE_ARTIFACT_SNAPSHOT` (`"build.stage.artifact.snapshot"`). Used by Tasks 3, 4, 7.
- Consumes: `ConnectionHub` (`skyn3t/web/websockets.py`) subscribes to `EventType.ALL`, so new types fan out to the `"all"` channel automatically — no hub change required.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stage_debug_events.py
"""New per-stage debug + artifact-snapshot events round-trip the enum -> WS wrap contract."""

from __future__ import annotations

import asyncio
import json

from skyn3t.core.events import EventBus, EventType
from skyn3t.web.websockets import ConnectionHub


def test_debug_event_values_are_lowercase_dotted():
    assert EventType.STAGE_DEBUG_STARTED.value == "build.stage.debug.started"
    assert EventType.STAGE_DEBUG_ATTEMPT.value == "build.stage.debug.attempt"
    assert EventType.STAGE_DEBUG_RESOLVED.value == "build.stage.debug.resolved"
    assert EventType.STAGE_ARTIFACT_SNAPSHOT.value == "build.stage.artifact.snapshot"


class _FakeSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, msg: str) -> None:
        self.sent.append(msg)


def test_debug_events_fan_out_wrapped():
    async def go():
        bus = EventBus()
        hub = ConnectionHub(bus)
        ws = _FakeSocket()
        await hub.add("all", ws)
        await bus.emit(
            EventType.STAGE_DEBUG_ATTEMPT, "studio",
            {"build_id": "b1", "stage": "code", "attempt": 1},
        )
        assert ws.sent, "the hub should forward the new event to the 'all' channel"
        frame = json.loads(ws.sent[-1])
        assert "event" in frame  # hub wraps as {"event": {...}}
        assert frame["event"]["type"] == "build.stage.debug.attempt"
        assert frame["event"]["payload"]["attempt"] == 1

    asyncio.run(go())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_stage_debug_events.py -v`
Expected: FAIL with `AttributeError: STAGE_DEBUG_STARTED` (member does not exist yet).

- [ ] **Step 3: Add the enum members**

In `skyn3t/core/events.py`, extend the `# Build pipeline` block (currently ends at line 55 `BUILD_FAILED = "build.failed"`):

```python
    # Build pipeline
    BUILD_STARTED = "build.started"
    BUILD_STAGE_STARTED = "build.stage.started"
    BUILD_STAGE_COMPLETED = "build.stage.completed"
    BUILD_COMPLETED = "build.completed"
    BUILD_FAILED = "build.failed"
    # Per-stage autonomous debug loop + live artifact snapshots (cockpit, Phase A)
    STAGE_DEBUG_STARTED = "build.stage.debug.started"
    STAGE_DEBUG_ATTEMPT = "build.stage.debug.attempt"
    STAGE_DEBUG_RESOLVED = "build.stage.debug.resolved"
    STAGE_ARTIFACT_SNAPSHOT = "build.stage.artifact.snapshot"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_stage_debug_events.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add skyn3t/core/events.py tests/test_stage_debug_events.py
git commit -m "events: add per-stage debug + artifact-snapshot event types"
```

---

### Task 2: `sync_preview` worktree → `.preview/` helper

**Files:**
- Modify: `skyn3t/worktree.py` (add after `list_files`, ~line 171)
- Test: `tests/test_preview_sync.py`

**Interfaces:**
- Consumes: existing `merge_back(worktree_dir, project_dir, *, overwrite=True, clean=False)`.
- Produces: `PREVIEW_SUBDIR = ".preview"`; `sync_preview(worktree_dir, project_dir, *, subdir=PREVIEW_SUBDIR) -> list[str]` (relative paths now mirrored under `project_dir/<subdir>`). Used by Task 4 (runner) and Tasks 5–6 (preview API read this dir).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preview_sync.py
from pathlib import Path

from skyn3t.worktree import PREVIEW_SUBDIR, sync_preview


def test_sync_preview_mirrors_worktree(tmp_path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "main.py").write_text("print('hi')\n")
    proj = tmp_path / "proj"

    copied = sync_preview(str(wt), str(proj))

    assert "src/main.py" in copied
    assert (proj / PREVIEW_SUBDIR / "src" / "main.py").read_text() == "print('hi')\n"


def test_sync_preview_replaces_stale_files(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a.py").write_text("a")
    proj = tmp_path / "proj"
    sync_preview(str(wt), str(proj))

    (wt / "a.py").unlink()
    (wt / "b.py").write_text("b")
    sync_preview(str(wt), str(proj))

    assert not (proj / PREVIEW_SUBDIR / "a.py").exists()  # clean replace
    assert (proj / PREVIEW_SUBDIR / "b.py").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_preview_sync.py -v`
Expected: FAIL with `ImportError: cannot import name 'sync_preview'`.

- [ ] **Step 3: Implement the helper**

Append to `skyn3t/worktree.py`:

```python
# Subdirectory under a delivered project that holds the live, read-only preview
# snapshot the cockpit watches while a build is still running. Disposable; the
# final clean merge_back removes it at delivery (it serves project root after).
PREVIEW_SUBDIR = ".preview"


def sync_preview(
    worktree_dir: str | Path,
    project_dir: str | Path,
    *,
    subdir: str = PREVIEW_SUBDIR,
) -> list[str]:
    """Mirror the in-progress worktree into ``project_dir/<subdir>`` for the
    cockpit. Read-only snapshot, replaced (clean) each call so it reflects the
    current state. Reuses :func:`merge_back`; never raises for a missing source.
    """
    preview_dir = Path(project_dir) / subdir
    return merge_back(worktree_dir, preview_dir, clean=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_preview_sync.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add skyn3t/worktree.py tests/test_preview_sync.py
git commit -m "worktree: add sync_preview snapshot for the live cockpit"
```

---

### Task 3: `debug_stage` per-stage debug pass

**Files:**
- Create: `skyn3t/studio/stage_debug.py`
- Test: `tests/test_stage_debug.py`

**Interfaces:**
- Consumes: `proof_run` (`skyn3t/studio/proof_run.py`); `EventType` (Task 1); duck-typed `spec` (`.name`, `.agent_type`, `.capability`), `record` (`StageRecord`: `.status`, `.score`), `plan` (`.checklist`, `.stack`), `settings` (read via `getattr`).
- Produces:
  - `StageDebugResult(passed: bool, degraded: bool, attempts: int, score: float | None, detail: dict)`
  - `async debug_stage(*, build_id: str, spec, record, worktree_dir: str, plan, settings, emit: Callable[[EventType, dict], Awaitable[None]], improve: Callable[[list[str]], Awaitable[bool]] | None = None, max_attempts: int = 3) -> StageDebugResult`
  - Used by Task 4.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stage_debug.py
import asyncio
from types import SimpleNamespace

from skyn3t.core.events import EventType
from skyn3t.studio.manifest import StageRecord
from skyn3t.studio.stage_debug import StageDebugResult, debug_stage


def _ctx(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    spec = SimpleNamespace(name="code", agent_type="code", capability="codegen")
    record = StageRecord(name="code", agent_type="code", capability="codegen", status="completed")
    plan = SimpleNamespace(checklist=["main.py"], stack="python")
    settings = SimpleNamespace(
        execution_backend="inline", run_generated_tests=False,
        generated_test_timeout=90, run_generated_build=False, generated_build_timeout=300,
    )
    emitted: list[tuple] = []

    async def emit(et, payload):
        emitted.append((et, payload))

    return wt, spec, record, plan, settings, emitted, emit


def test_code_stage_fixes_then_passes(tmp_path):
    async def go():
        wt, spec, record, plan, settings, emitted, emit = _ctx(tmp_path)

        async def improve(_gaps):
            (wt / "main.py").write_text("def main():\n    return 0\n")
            return True

        result = await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=improve, max_attempts=3,
        )
        assert isinstance(result, StageDebugResult)
        assert result.passed and result.attempts == 1
        types = [t for t, _ in emitted]
        assert types[0] == EventType.STAGE_DEBUG_STARTED
        assert EventType.STAGE_DEBUG_ATTEMPT in types
        assert types[-1] == EventType.STAGE_DEBUG_RESOLVED
        resolved = [p for t, p in emitted if t == EventType.STAGE_DEBUG_RESOLVED][-1]
        assert resolved["status"] == "passed"

    asyncio.run(go())


def test_code_stage_degrades_when_unfixable(tmp_path):
    async def go():
        wt, spec, record, plan, settings, emitted, emit = _ctx(tmp_path)

        async def improve(_gaps):
            return False  # writes nothing -> stays empty -> proof keeps failing

        result = await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=improve, max_attempts=2,
        )
        assert result.degraded and not result.passed
        resolved = [p for t, p in emitted if t == EventType.STAGE_DEBUG_RESOLVED][-1]
        assert resolved["status"] == "degraded"

    asyncio.run(go())


def test_non_code_stage_passes_through(tmp_path):
    async def go():
        wt, _spec, _record, plan, settings, emitted, emit = _ctx(tmp_path)
        spec = SimpleNamespace(name="architect", agent_type="architect", capability="architecture")
        record = StageRecord(name="architect", agent_type="architect",
                             capability="architecture", status="completed")

        result = await debug_stage(
            build_id="b1", spec=spec, record=record, worktree_dir=str(wt),
            plan=plan, settings=settings, emit=emit, improve=None,
        )
        assert result.passed and result.attempts == 0
        types = [t for t, _ in emitted]
        assert EventType.STAGE_DEBUG_ATTEMPT not in types  # no fix loop for non-code

    asyncio.run(go())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_stage_debug.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skyn3t.studio.stage_debug'`.

- [ ] **Step 3: Implement the module**

```python
# skyn3t/studio/stage_debug.py
"""Per-stage debug pass — verify each build step, fix it, then proceed.

The pipeline used to debug only ONCE, at the end (a single proof + fix loop on
the merged tree), so a broken early stage poisoned everything downstream. This
pass runs after each productive stage: it checks the stage's output, and for the
code stage runs a bounded fix loop (re-using the ``code_improver`` agent), then
emits the events the cockpit renders. Fully autonomous — never prompts a human;
an unfixable step is flagged ``degraded`` and the build proceeds best-effort.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from skyn3t.core.events import EventType
from skyn3t.studio.proof_run import proof_run

# Stage agent_types whose output gets a full proof + fix loop. Other stages get
# a light "did it produce output" check with no auto-fix (Phase A scope).
_CODE_AGENT_TYPES = frozenset({"code"})

EmitFn = Callable[[EventType, dict[str, Any]], Awaitable[None]]
ImproveFn = Callable[[list[str]], Awaitable[bool]]


@dataclass(slots=True)
class StageDebugResult:
    passed: bool
    degraded: bool
    attempts: int
    score: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _Check:
    passed: bool
    score: float | None
    gaps: list[str]


def _run_check(spec: Any, record: Any, worktree_dir: str, plan: Any, settings: Any) -> _Check:
    """Stage-appropriate pass/fail. Code stages get a real proof; others a light check."""
    if spec.agent_type in _CODE_AGENT_TYPES:
        proof = proof_run(
            worktree_dir,
            checklist=list(getattr(plan, "checklist", []) or []),
            execution_backend=getattr(settings, "execution_backend", "auto"),
            stack=getattr(plan, "stack", ""),
            run_tests=bool(getattr(settings, "run_generated_tests", True)),
            test_timeout=int(getattr(settings, "generated_test_timeout", 90)),
            run_build=bool(getattr(settings, "run_generated_build", True)),
            build_timeout=int(getattr(settings, "generated_build_timeout", 300)),
        )
        gaps = list(proof.missing) + list(proof.syntax_errors)
        return _Check(passed=proof.passed, score=proof.score, gaps=gaps)
    passed = record.status == "completed"
    gaps = [] if passed else [f"stage {spec.name} status={record.status}"]
    return _Check(passed=passed, score=record.score, gaps=gaps)


async def debug_stage(
    *,
    build_id: str,
    spec: Any,
    record: Any,
    worktree_dir: str,
    plan: Any,
    settings: Any,
    emit: EmitFn,
    improve: ImproveFn | None = None,
    max_attempts: int = 3,
) -> StageDebugResult:
    """Run the per-stage debug loop, emitting STAGE_DEBUG_* events. Never raises."""
    base = {"build_id": build_id, "stage": spec.name, "capability": spec.capability}
    check_kind = "proof" if spec.agent_type in _CODE_AGENT_TYPES else "light"
    await emit(EventType.STAGE_DEBUG_STARTED, {**base, "check": check_kind})

    check = _run_check(spec, record, worktree_dir, plan, settings)
    attempts = 0
    while not check.passed and improve is not None and attempts < max_attempts:
        attempts += 1
        score_before = check.score
        try:
            ran = await improve(check.gaps)
        except Exception:  # noqa: BLE001 - a failed fix must not crash the build
            ran = False
        nxt = _run_check(spec, record, worktree_dir, plan, settings)
        await emit(EventType.STAGE_DEBUG_ATTEMPT, {
            **base, "agent_type": spec.agent_type, "attempt": attempts,
            "errors": check.gaps[:10], "fix_applied": bool(ran),
            "passed": nxt.passed, "score_before": score_before, "score_after": nxt.score,
        })
        check = nxt

    status = "passed" if check.passed else "degraded"
    await emit(EventType.STAGE_DEBUG_RESOLVED, {
        **base, "status": status, "reason": "; ".join(check.gaps[:3]),
    })
    return StageDebugResult(
        passed=check.passed, degraded=not check.passed, attempts=attempts,
        score=check.score, detail={"gaps": check.gaps[:10]},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_stage_debug.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/stage_debug.py tests/test_stage_debug.py
git commit -m "studio: add per-stage debug pass (proof+fix loop, no prompts)"
```

---

### Task 4: Wire the debug pass + snapshot into the runner stage loop

**Files:**
- Modify: `skyn3t/studio/runner.py` — add `_improve_once` (above `_fix_loop`, ~375); add `_debug_and_snapshot` (near `_emit_stage_done`, ~931); call it in the stage loop (~732, right after `_emit_stage_done`, before the approval gate). `_fix_loop` is left untouched.
- Test: `tests/test_debug_and_snapshot.py`

**Interfaces:**
- Consumes: `debug_stage`, `StageDebugResult` (Task 3); `sync_preview` (Task 2); existing `self.event_bus`, `self.settings`, `self._has_capability`, `self.orchestrator`, `TaskRequest`.
- Produces: `async StudioRunner._improve_once(self, *, work_dir, plan, gaps, correlation_id, extra, label, brief='', slug='') -> bool`; `async StudioRunner._debug_and_snapshot(self, build_id, spec, record, main_wt, project_dir, plan, correlation_id, extra, *, brief='', slug='') -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_debug_and_snapshot.py
import asyncio
from pathlib import Path
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import StageRecord
from skyn3t.studio.runner import StudioRunner


def test_debug_and_snapshot_emits_events_and_writes_preview(tmp_path):
    async def go():
        bus = EventBus()
        runner = StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.ALL, cap)

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "main.py").write_text("def main():\n    return 0\n")  # passes the code check
        proj = tmp_path / "proj"
        spec = SimpleNamespace(name="code", agent_type="code", capability="codegen")
        record = StageRecord(name="code", agent_type="code", capability="codegen", status="completed")
        plan = SimpleNamespace(checklist=["main.py"], stack="python")

        await runner._debug_and_snapshot(
            "b1", spec, record, SimpleNamespace(dir=str(wt)), str(proj), plan, "cid", {}
        )

        types = [e.type for e in seen]
        assert EventType.STAGE_DEBUG_STARTED in types
        assert EventType.STAGE_DEBUG_RESOLVED in types
        assert EventType.STAGE_ARTIFACT_SNAPSHOT in types
        snap = [e for e in seen if e.type == EventType.STAGE_ARTIFACT_SNAPSHOT][-1]
        assert "main.py" in snap.payload["files"]
        assert (proj / ".preview" / "main.py").exists()
        assert record.output_summary.get("debug", {}).get("passed") is True

    asyncio.run(go())


def test_debug_and_snapshot_skips_unrun_stage(tmp_path):
    async def go():
        bus = EventBus()
        runner = StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.ALL, cap)
        wt = tmp_path / "wt"
        wt.mkdir()
        spec = SimpleNamespace(name="code", agent_type="code", capability="codegen")
        record = StageRecord(name="code", agent_type="code", capability="codegen", status="skipped")
        plan = SimpleNamespace(checklist=[], stack="python")

        await runner._debug_and_snapshot(
            "b1", spec, record, SimpleNamespace(dir=str(wt)), str(tmp_path / "p"), plan, "cid", {}
        )
        assert not seen  # a stage that did not run is not debugged

    asyncio.run(go())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_debug_and_snapshot.py -v`
Expected: FAIL with `AttributeError: 'StudioRunner' object has no attribute '_debug_and_snapshot'`.

- [ ] **Step 3a: Add a standalone `_improve_once` helper**

In `skyn3t/studio/runner.py`, add this method just above `_fix_loop` (line 375). It is used only by the new debug pass — `_fix_loop` is left untouched to avoid regressing its existing test.

```python
    async def _improve_once(
        self, *, work_dir: str, plan, gaps: list[str], correlation_id: str,
        extra: dict | None, label: str, brief: str = "", slug: str = "",
    ) -> bool:
        """Run the code-improver once against ``work_dir`` for the flagged gaps.

        Returns True if an improver task was dispatched. Best-effort: a missing
        capability or a failed submission returns False and never raises.
        """
        if not self._has_capability("code_improve"):
            return False
        payload = {
            "brief": brief, "slug": slug,
            "worktree_dir": work_dir, "project_dir": work_dir,
            "stack": plan.stack, "plan": plan.to_dict() if hasattr(plan, "to_dict") else {},
            "gaps": list(gaps),
        }
        if extra:
            payload["extra"] = extra
        task = TaskRequest(
            type="code_improver", payload=payload,
            capabilities_required=("code_improve",),
            correlation_id=correlation_id, metadata={"stage": label},
        )
        try:
            await asyncio.wait_for(self.orchestrator.submit(task), timeout=self.stage_exec_timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("debug.improve_failed", label=label, error=str(exc))
            return False
```

- [ ] **Step 3b: Add `_debug_and_snapshot`**

Add near `_emit_stage_done` (line 931). Add the imports `from skyn3t.studio.stage_debug import debug_stage` and `from skyn3t.worktree import sync_preview` to the top of the file (the existing `from skyn3t.worktree import ... merge_back, list_files ...` import can be extended with `sync_preview`).

```python
    async def _debug_and_snapshot(
        self, build_id: str, spec, record, main_wt, project_dir: str,
        plan, correlation_id: str, extra: dict, *, brief: str = "", slug: str = "",
    ) -> None:
        """Per-stage: debug the just-run stage (autonomous), then snapshot the
        worktree into ``.preview`` so the cockpit can show files-so-far. Only
        stages that actually ran are debugged. Never raises."""
        if record.status != "completed":
            return

        async def emit(event_type, payload):
            await self.event_bus.emit(event_type, "studio", payload, correlation_id=correlation_id)

        improve = None
        if spec.agent_type == "code":
            async def improve(gaps):  # noqa: E306 - closure over loop vars is intended
                return await self._improve_once(
                    work_dir=main_wt.dir, plan=plan, gaps=gaps,
                    correlation_id=correlation_id, extra=extra,
                    label=f"debug:{spec.name}", brief=brief, slug=slug,
                )

        result = await debug_stage(
            build_id=build_id, spec=spec, record=record, worktree_dir=main_wt.dir,
            plan=plan, settings=self.settings, emit=emit, improve=improve,
            max_attempts=int((extra or {}).get("max_debug_attempts", 3)),
        )
        summary = dict(record.output_summary or {})
        summary["debug"] = {"passed": result.passed, "degraded": result.degraded, "attempts": result.attempts}
        record.output_summary = summary

        files = sync_preview(main_wt.dir, project_dir)
        await emit(EventType.STAGE_ARTIFACT_SNAPSHOT,
                   {"build_id": build_id, "stage": spec.name, "files": files[:200]})
```

- [ ] **Step 3c: Call it in the stage loop**

In `start`, immediately after `await self._emit_stage_done(build_id, record, correlation_id)` (line 732) and before the `if spec.gated:` approval block (line 735), add:

```python
                await self._debug_and_snapshot(
                    build_id, spec, record, main_wt, project_dir, plan,
                    correlation_id, extra, brief=brief, slug=slug,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_debug_and_snapshot.py tests/test_fix_loop.py -v`
Expected: PASS — the new tests, plus the existing `test_fix_loop.py` (untouched, confirms no regression).

- [ ] **Step 5: Commit**

```bash
git add skyn3t/studio/runner.py tests/test_debug_and_snapshot.py
git commit -m "studio: run the per-stage debug pass + preview snapshot in the build loop"
```

---

### Task 5: Preview API — backend-agnostic handlers

**Files:**
- Modify: `skyn3t/web/routes.py` (add handlers near the other `*_payload` functions, ~line 47-61; reuse the existing `BuildManifest` import path)
- Test: `tests/test_preview_api.py`

**Interfaces:**
- Consumes: `state.settings.projects_dir`; `skyn3t.worktree.PREVIEW_SUBDIR`; `skyn3t.studio.manifest.BuildManifest`.
- Produces:
  - `_preview_root(state, slug) -> Path` — `<projects_dir>/<slug>/.preview` if it exists, else `<projects_dir>/<slug>`.
  - `async preview_payload(state, slug: str) -> dict` — `{slug, root, files: [rel...], manifest: {...} | None}`.
  - `resolve_project_file(state, slug: str, rel_path: str) -> Path` — validated path inside the preview root; raises `ValueError` on traversal/escape, `FileNotFoundError` if absent. Used by Task 6.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preview_api.py
import asyncio

import pytest

from skyn3t.config.settings import Settings
from skyn3t.web.deps import AppState
from skyn3t.web.routes import preview_payload, resolve_project_file


def _state(tmp_path):
    state = AppState(settings=Settings(projects_dir=tmp_path))
    proj = tmp_path / "demo" / ".preview"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text("print('hi')\n")
    return state


def test_preview_payload_lists_files(tmp_path):
    state = _state(tmp_path)
    payload = asyncio.run(preview_payload(state, "demo"))
    assert "src/main.py" in payload["files"]
    assert payload["slug"] == "demo"


def test_resolve_project_file_returns_path(tmp_path):
    state = _state(tmp_path)
    path = resolve_project_file(state, "demo", "src/main.py")
    assert path.read_text() == "print('hi')\n"


def test_resolve_project_file_rejects_traversal(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        resolve_project_file(state, "demo", "../../../../etc/passwd")


def test_resolve_project_file_missing(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(FileNotFoundError):
        resolve_project_file(state, "demo", "nope.py")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_preview_api.py -v`
Expected: FAIL with `ImportError: cannot import name 'preview_payload'`.

- [ ] **Step 3: Implement the handlers**

Add to `skyn3t/web/routes.py` (after `budget_payload`, ~line 61). Add `from pathlib import Path` and `from skyn3t.worktree import PREVIEW_SUBDIR, list_files` and `from skyn3t.studio.manifest import BuildManifest` to the imports.

```python
def _preview_root(state: AppState, slug: str) -> Path:
    """The dir the cockpit serves: the live ``.preview`` snapshot while a build
    runs, else the delivered project root after delivery."""
    base = Path(state.settings.projects_dir) / slug
    preview = base / PREVIEW_SUBDIR
    return preview if preview.is_dir() else base


async def preview_payload(state: AppState, slug: str) -> dict[str, Any]:
    root = _preview_root(state, slug)
    files = list_files(root) if root.is_dir() else []
    manifest = BuildManifest.load(Path(state.settings.projects_dir) / slug)
    return {
        "slug": slug,
        "root": str(root),
        "files": sorted(files),
        "manifest": manifest.to_dict() if manifest is not None else None,
    }


def resolve_project_file(state: AppState, slug: str, rel_path: str) -> Path:
    """Resolve a preview-relative path to an absolute file, refusing escapes.

    Raises ``ValueError`` if the path escapes the preview root, ``FileNotFoundError``
    if no such file exists. This is the security boundary for the file route."""
    root = _preview_root(state, slug).resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes preview root: {rel_path!r}")
    if not candidate.is_file():
        raise FileNotFoundError(rel_path)
    return candidate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_preview_api.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add skyn3t/web/routes.py tests/test_preview_api.py
git commit -m "web: preview payload + path-safe project file resolver"
```

---

### Task 6: Preview API — FastAPI routes + auth

**Files:**
- Modify: `skyn3t/web/routes.py` — inside `build_router` (after the `/builds` routes, ~line 652)
- Test: `tests/test_preview_routes.py`

**Interfaces:**
- Consumes: `preview_payload`, `resolve_project_file` (Task 5); existing `require_auth`/`auth` dependency, `HTTPException`, `FileResponse` (import from `fastapi.responses`).
- Produces: `GET /api/preview/{slug}` (JSON) and `GET /api/projects/{slug}/{path:path}` (`FileResponse`), both `dependencies=[auth]`. The cockpit iframe/code-view fetch these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preview_routes.py
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from skyn3t.config.settings import Settings  # noqa: E402
from skyn3t.web.app import create_app  # noqa: E402
from skyn3t.web.deps import AppState  # noqa: E402


def _client(tmp_path, token=""):
    state = AppState(settings=Settings(projects_dir=tmp_path, auth_token=token))
    proj = tmp_path / "demo" / ".preview"
    proj.mkdir(parents=True)
    (proj / "index.html").write_text("<h1>hi</h1>")
    app = create_app(state=state)
    return TestClient(app)


def test_preview_lists_files(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/preview/demo")
    assert res.status_code == 200
    assert "index.html" in res.json()["files"]


def test_project_file_serves_content(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/projects/demo/index.html")
    assert res.status_code == 200
    assert "<h1>hi</h1>" in res.text


def test_project_file_traversal_rejected(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/projects/demo/../../../../etc/passwd")
    assert res.status_code in (400, 404)


def test_preview_requires_auth_when_token_set(tmp_path):
    client = _client(tmp_path, token="secret")
    # No Authorization header + non-loopback is rejected; TestClient is treated as
    # loopback, so assert the authed path works and a bad token is refused.
    assert client.get("/api/preview/demo", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert client.get("/api/preview/demo", headers={"Authorization": "Bearer wrong"}).status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_preview_routes.py -v`
Expected: FAIL with 404 on `/api/preview/demo` (routes not registered yet).

- [ ] **Step 3: Register the routes**

In `build_router` (after the `/builds` POST alias, ~line 652), add. Ensure `from fastapi.responses import FileResponse` is imported inside the `try` FastAPI-import block at the top of `routes.py`.

```python
    @router.get("/preview/{slug}", dependencies=[auth])
    async def _preview(slug: str) -> dict[str, Any]:
        return await preview_payload(state, slug)

    @router.get("/projects/{slug}/{path:path}", dependencies=[auth])
    async def _project_file(slug: str, path: str) -> Any:
        try:
            resolved = resolve_project_file(state, slug, path)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid path")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(str(resolved))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_preview_routes.py -v`
Expected: PASS (4 tests). (If FastAPI is absent, the module is skipped via `importorskip`.)

- [ ] **Step 5: Commit**

```bash
git add skyn3t/web/routes.py tests/test_preview_routes.py
git commit -m "web: serve /api/preview + /api/projects (auth-gated, traversal-safe)"
```

---

### Task 7: Cockpit UI — debug timeline, files-so-far, preview panel

**Files:**
- Create: `skyn3t/web/ui/src/components/cockpit.jsx`
- Modify: `skyn3t/web/ui/src/routes/Studio.jsx` (consume the new components)

**Interfaces:**
- Consumes: `stream.events` (already provided to `Studio` via `useEventStream`); event types `build.stage.debug.started|attempt|resolved` and `build.stage.artifact.snapshot`; the `/api/preview/{slug}` and `/api/projects/{slug}/{path}` routes (Task 6).
- Produces: `DebugTimeline`, `FilesSoFar`, `PreviewPanel` React components; helpers `debugRowsFromEvents(events)`, `latestSnapshot(events)`, `latestRunningSlug(events)`.

**Note on testing:** the SPA has no JS unit-test runner (no vitest/jest in `package.json`). Verification for this task is a successful production build (`npm run build`) plus a manual smoke check. Keep the components **event-driven** (render whatever stages arrive) — do NOT hardcode a stage list; the existing `STAGES` constant already drifted from the backend vocabulary.

- [ ] **Step 1: Create the cockpit components**

```jsx
// skyn3t/web/ui/src/components/cockpit.jsx
import React, { useMemo } from "react";

// Build per-stage debug rows from the live event stream. Event-driven: we render
// whatever stages appear, so we never drift from the backend stage vocabulary.
export function debugRowsFromEvents(events) {
  const rows = new Map();
  for (const e of events) {
    const stage = e.payload?.stage;
    if (!stage) continue;
    if (e.type === "build.stage.debug.started") {
      if (!rows.has(stage)) rows.set(stage, { stage, state: "running", attempts: [] });
    } else if (e.type === "build.stage.debug.attempt") {
      const r = rows.get(stage) || { stage, state: "running", attempts: [] };
      r.attempts.push({
        n: e.payload.attempt,
        passed: e.payload.passed,
        fix: e.payload.fix_applied,
        errors: e.payload.errors || [],
      });
      rows.set(stage, r);
    } else if (e.type === "build.stage.debug.resolved") {
      const r = rows.get(stage) || { stage, state: "running", attempts: [] };
      r.state = e.payload.status; // "passed" | "degraded"
      r.reason = e.payload.reason;
      rows.set(stage, r);
    }
  }
  return Array.from(rows.values());
}

export function latestSnapshot(events) {
  let snap = null;
  for (const e of events) {
    if (e.type === "build.stage.artifact.snapshot") snap = e.payload;
  }
  return snap; // { build_id, stage, files: [...] } | null
}

export function latestRunningSlug(events) {
  let slug = null;
  for (const e of events) {
    if (e.type === "build.started" && e.payload?.slug) slug = e.payload.slug;
  }
  return slug;
}

export function DebugTimeline({ events }) {
  const rows = useMemo(() => debugRowsFromEvents(events), [events]);
  if (rows.length === 0) {
    return <p className="px-4 py-3 font-mono text-[11px] text-ash/70">No debug activity yet.</p>;
  }
  return (
    <div className="flex flex-col divide-y divide-hairline/60">
      {rows.map((r) => (
        <div key={r.stage} className="px-4 py-2">
          <div className="flex items-center justify-between">
            <span className="font-mono text-[12px] text-bone">{r.stage}</span>
            <span
              className={`eyebrow text-[10px] ${
                r.state === "passed"
                  ? "text-plasma"
                  : r.state === "degraded"
                  ? "text-ember"
                  : "text-ash"
              }`}
            >
              {r.state}
            </span>
          </div>
          {r.attempts.map((a) => (
            <div key={a.n} className="pl-3 font-mono text-[10px] text-ash/80">
              #{a.n} {a.fix ? "fix→" : ""}
              {a.passed ? "✓" : "✗"}
              {a.errors.length ? ` · ${a.errors[0]}` : ""}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export function FilesSoFar({ events }) {
  const snap = useMemo(() => latestSnapshot(events), [events]);
  const files = snap?.files || [];
  if (files.length === 0) {
    return <p className="px-4 py-3 font-mono text-[11px] text-ash/70">No files yet.</p>;
  }
  return (
    <ul className="max-h-64 overflow-y-auto px-4 py-2 font-mono text-[11px] text-ash">
      {files.map((f) => (
        <li key={f} className="truncate">
          {f}
        </li>
      ))}
    </ul>
  );
}

export function PreviewPanel({ events }) {
  const slug = useMemo(() => latestRunningSlug(events), [events]);
  const snap = useMemo(() => latestSnapshot(events), [events]);
  const hasIndex = (snap?.files || []).includes("index.html");
  if (!slug) {
    return <p className="px-4 py-3 font-mono text-[11px] text-ash/70">Submit a brief to preview.</p>;
  }
  if (!hasIndex) {
    return (
      <p className="px-4 py-3 font-mono text-[11px] text-ash/70">
        No rendered preview for this stack — see Files + Debug. ({slug})
      </p>
    );
  }
  // Rendered preview works for relative-asset apps (e.g. static_html) and in
  // loopback (no-token) mode. With a token set, the iframe may not authenticate.
  return (
    <iframe
      title="live preview"
      src={`/api/projects/${slug}/index.html`}
      className="h-72 w-full rounded-md border border-hairline bg-white"
    />
  );
}
```

- [ ] **Step 2: Wire the panels into `Studio.jsx`**

Add the import after line 12, then insert the panels between the "Forge line" panel (ends ~line 190) and the "Recent builds" panel (line 192).

```jsx
// after the existing imports (line 12)
import { DebugTimeline, FilesSoFar, PreviewPanel } from "../components/cockpit.jsx";
```

```jsx
      {/* Cockpit: per-stage debug + live artifact (insert before Recent builds) */}
      <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel className="overflow-hidden">
          <PanelHead label="Stage debug" />
          <DebugTimeline events={events} />
        </Panel>
        <Panel className="overflow-hidden">
          <PanelHead label="Live preview" />
          <PreviewPanel events={events} />
          <PanelHead label="Files so far" />
          <FilesSoFar events={events} />
        </Panel>
      </div>
```

- [ ] **Step 3: Build the SPA to verify it compiles**

Run: `cd skyn3t/web/ui && npm install && npm run build`
Expected: Vite build completes with no errors; `dist/` is regenerated.

- [ ] **Step 4: Commit**

```bash
git add skyn3t/web/ui/src/components/cockpit.jsx skyn3t/web/ui/src/routes/Studio.jsx
git commit -m "ui: cockpit panels — per-stage debug timeline, files-so-far, live preview"
```

---

### Task 8: Full-suite verification + dist rebuild

**Files:**
- Modify: `STATUS.md` (note the cockpit; one line under cross-package wiring)
- (Verification only; no new source.)

- [ ] **Step 1: Run the entire Python suite**

Run: `pytest -q`
Expected: all prior tests still pass (baseline was 330 passed / 1 skipped) plus the new tests from Tasks 1–6. No failures, no errors.

- [ ] **Step 2: Lint the changed Python**

Run: `ruff check skyn3t/studio/stage_debug.py skyn3t/studio/runner.py skyn3t/web/routes.py skyn3t/worktree.py skyn3t/core/events.py`
Expected: no errors (the repo selects E, F, I, UP, B; line-length 100, E501 ignored).

- [ ] **Step 3: Rebuild the SPA bundle**

Run: `cd skyn3t/web/ui && npm run build`
Expected: clean build; `dist/` updated (untracked/local).

- [ ] **Step 4: Smoke-test the live cockpit (manual)**

Run: `skyn3t start --web` then open the dashboard `/studio`, submit `a simple python cli that greets the user`, and confirm: the Stage-debug panel populates, Files-so-far lists files as stages complete, and a completed build is viewable via `/api/preview/<slug>`.
Expected: debug rows appear; files stream in; `/api/preview/<slug>` returns JSON with the delivered files.

- [ ] **Step 5: Update STATUS.md + commit**

Add one line under "Cross-package wiring — status / Done": `- **Live build cockpit (wired):** per-stage autonomous debug pass + STAGE_DEBUG_* / STAGE_ARTIFACT_SNAPSHOT events + /api/preview + /api/projects + cockpit panels (Phase A).`

```bash
git add STATUS.md
git commit -m "docs: note the live build cockpit (Phase A) in STATUS"
```

---

## Notes for the implementer

- **Out of scope (do not build):** cross-build learning that consumes the debug events (Phase B), a mobile target stack (Phase C), human steering/edits mid-build (excluded by "no prompts").
- **Honest limitations to preserve (don't paper over):**
  - The rendered iframe preview only works for relative-asset apps (e.g. `static_html`) and in loopback/no-token mode; the Files + Debug panels are the always-works baseline. Full Vite-`base` rewriting for `react_vite` dist rendering is a deliberate follow-up.
  - The per-stage proof runs on the **worktree** (`main_wt.dir`), before merge — that is intentional (catch errors pre-delivery).
- **Deliberate Phase-A simplifications vs. the spec (stated, not silent):**
  - §4.2 **tier-escalation is deferred** — the loop does bounded attempts then flags `degraded`. The critical-step → `completed_no_go` outcome is already handled by the existing delivery gate / `_final_build_status`, so no new abort logic is added.
  - §5 **transcript fields** `instruction` / `input_digest` / `output_digest` are **not** in `STAGE_DEBUG_ATTEMPT` yet — `debug_stage` does not receive agent transcripts. They are added in Phase B when transcript collection is wired. The available signal (errors, fix_applied, passed, score_before/after, agent_type, stage) ships now.
  - Routes live under the existing `/api` prefix (`/api/preview`, `/api/projects`) — the spec's `/preview` is shorthand; using `/api` reuses the router's auth dependency and the SPA catch-all's `api/` guard.
- **Review checkpoints:** Tasks 1–4 (backend loop + events) form one reviewable unit; Tasks 5–6 (preview API) a second; Task 7 (UI) a third.
