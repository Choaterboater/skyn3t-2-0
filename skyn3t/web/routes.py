"""REST endpoints for the SkyN3t dashboard / control API.

FastAPI is a guarded optional dependency. :func:`build_router` only runs when
FastAPI is importable; importing this module never requires it. All handlers
read payloads defensively and degrade gracefully when a spine collaborator is
absent (design rule #6).
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import structlog

from skyn3t.core.events import EventType
from skyn3t.studio.manifest import BuildManifest
from skyn3t.web.deps import AppState, BuildRecord, ProposalRecord, check_auth
from skyn3t.worktree import PREVIEW_SUBDIR, list_files

log = structlog.get_logger(__name__)

# The process boot instant + a cached staleness probe. A long-running server keeps
# serving the code imported at boot; when the working tree moves on, features look
# broken while the fix sits unloaded on disk (this bit us three times — most
# recently 'Improve under Projects does nothing', 2026-07-01). status/health
# expose `started_at`/`stale_code` so the UI can tell the user to restart.
_PROCESS_STARTED = time.time()
_STALE_PRUNE = frozenset({"node_modules", "__pycache__", ".git", "dist", ".next", "ui"})
_STALE_TTL_S = 60.0
_stale_cache: tuple[float, bool] = (0.0, False)


def _source_newer_than(root: Path, started: float) -> bool:
    """True when any ``.py`` under ``root`` (pruned of vendored/derived dirs) has an
    mtime newer than ``started``. Never raises — an unreadable path is skipped and a
    broken walk reads as not-stale (a health probe must not invent failures)."""
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _STALE_PRUNE]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                try:
                    if os.path.getmtime(os.path.join(dirpath, fn)) > started:
                        return True
                except OSError:
                    continue
    except Exception:  # noqa: BLE001 - a health probe must never raise
        return False
    return False


def code_is_stale(force_refresh: bool = False) -> bool:
    """Whether any skyn3t package source file is newer than this process (cached
    ``_STALE_TTL_S`` so the health poll doesn't re-walk the tree every request).
    +1s slack absorbs filesystem mtime granularity around boot."""
    global _stale_cache
    now = time.time()
    if not force_refresh and now - _stale_cache[0] < _STALE_TTL_S:
        return _stale_cache[1]
    pkg_root = Path(__file__).resolve().parents[1]
    stale = _source_newer_than(pkg_root, _PROCESS_STARTED + 1.0)
    _stale_cache = (now, stale)
    return stale


# Strong references to in-flight background build tasks (prevent GC mid-run).
_BUILD_TASKS: set = set()


def _reap_build_task(task: Any) -> None:
    _BUILD_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # build task should never raise (runner catches), but log if it does
        log.error("web.build_task_crashed", error=str(exc))


# Strong references to in-flight background improve tasks (prevent GC mid-run).
_IMPROVE_TASKS: set = set()


def _reap_improve_task(task: Any) -> None:
    _IMPROVE_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # ImproveEngine catches internally, but log if it ever raises
        log.error("web.improve_task_crashed", error=str(exc))


# Strong references to in-flight background fan-out tasks (prevent GC mid-run).
_FANOUT_TASKS: set = set()


def _reap_fanout_task(task: Any) -> None:
    _FANOUT_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # fan_out catches per-candidate, but log infra faults
        log.error("web.fanout_task_crashed", error=str(exc))

try:  # pragma: no cover - exercised only when fastapi present
    from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
    from fastapi.responses import FileResponse
    _HAVE_FASTAPI = True
except Exception:  # noqa: BLE001
    APIRouter = Body = Depends = HTTPException = Query = Request = None  # type: ignore[assignment,misc]
    FileResponse = None  # type: ignore[assignment,misc]
    _HAVE_FASTAPI = False


# ---------------------------------------------------------------------------
# Backend-agnostic handler implementations. These take an AppState and plain
# kwargs so they are unit-testable without FastAPI or a running server.
# ---------------------------------------------------------------------------
async def status_payload(state: AppState) -> dict[str, Any]:
    return {**state.status(), "started_at": _PROCESS_STARTED, "stale_code": code_is_stale()}


async def agents_payload(state: AppState) -> dict[str, Any]:
    return {"agents": state.agents_snapshot()}


async def llm_backends_payload(state: AppState) -> dict[str, Any]:
    return state.llm_backends()


async def budget_payload(state: AppState) -> dict[str, Any]:
    return state.budget_snapshot()


# ---- live build preview (cockpit, Phase A) --------------------------------
def _preview_root(state: AppState, slug: str) -> Path:
    """The dir the cockpit serves: the live ``.preview`` snapshot while a build
    runs, else the delivered project root after delivery.

    Guards the SLUG against escaping ``projects_dir`` (e.g. ``slug='..'`` would
    otherwise resolve to the parent and leak a directory listing / arbitrary
    file read). Raises ``ValueError`` on escape — callers map it to HTTP 400."""
    projects_root = Path(state.settings.projects_dir).resolve()
    base = (projects_root / slug).resolve()
    if not base.is_relative_to(projects_root):
        raise ValueError(f"slug escapes projects_dir: {slug!r}")
    preview = base / PREVIEW_SUBDIR
    return preview if preview.is_dir() else base


async def preview_payload(state: AppState, slug: str) -> dict[str, Any]:
    """Manifest + file tree for a build's live (or delivered) artifact."""
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


def _save_reference_image(state: AppState, build_id: str, data_url: str) -> str:
    """Decode a base64 ``data:`` image URL and save it under data_dir so the
    build's agents can read it as a file path. Returns the saved PATH, or ``""``.

    SECURITY: only ``data:`` URLs are accepted from the (untrusted) API body. A
    bare filesystem path or http(s) URL is rejected — otherwise a request could
    set ``reference_image`` to e.g. ``/etc/passwd`` and have the server read an
    arbitrary local file (it later gets base64-encoded and sent to the model), or
    point at an attacker-chosen URL (SSRF).
    """
    s = (data_url or "").strip()
    if not s.startswith("data:"):
        if s:
            log.warning("build.reference_image_rejected",
                        note="non-data: reference_image ignored (path/url not allowed)")
        return ""  # reject paths / remote URLs from untrusted input
    try:
        header, _, b64 = s.partition(",")
        if not b64:
            return s
        import base64

        raw = base64.b64decode(b64, validate=False)
        ext = "png"
        if "image/jpeg" in header or "image/jpg" in header:
            ext = "jpg"
        elif "image/webp" in header:
            ext = "webp"
        out_dir = Path(state.settings.data_dir) / "reference_images"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{build_id}.{ext}"
        out_path.write_bytes(raw)
        return str(out_path)
    except Exception as exc:  # noqa: BLE001 - never let an image break a build
        log.warning("build.reference_image_save_failed", error=str(exc)[:160])
        return s  # a data: URL is safe inline data; the LLM client accepts it


async def submit_build(state: AppState, brief: str, stack: str = "", slug: str = "",
                       reference_image: str = "") -> dict[str, Any]:
    """Queue a build. Uses the studio if wired, else records + emits an event.

    ``reference_image`` is an optional base64 ``data:`` URL (or path); when
    present it is saved and threaded into the build so the design/architecture
    agents can match it ("build from a picture"). Absent -> unchanged behavior.
    """
    if not brief or not brief.strip():
        raise ValueError("brief is required")
    build_id = state.new_build_id()
    rec = BuildRecord(
        build_id=build_id,
        brief=brief.strip(),
        slug=slug.strip(),
        stack=stack.strip(),
        status="queued",
        correlation_id=build_id,
    )
    state.builds[build_id] = rec

    # Prefer a wired StudioRunner (async start(brief, slug=None, extra=None)),
    # falling back to a legacy submit(...) if present. The build runs as a
    # background task so the endpoint returns immediately with the build_id.
    # Optional reference image: decode + save (degrades to data-URL pass-through).
    ref_path = ""
    if reference_image and reference_image.strip():
        ref_path = _save_reference_image(state, build_id, reference_image.strip())

    studio = state.studio
    dispatched = False
    runner = None
    if studio is not None:
        if hasattr(studio, "start"):
            _extra = {"stack": stack, "build_id": build_id}
            if ref_path:
                _extra["reference_image"] = ref_path
            def runner() -> Any:
                return studio.start(brief, slug=slug or None, extra=_extra)
        elif hasattr(studio, "submit"):  # pragma: no cover - legacy shape
            def runner() -> Any:
                return studio.submit(brief=brief, slug=slug, stack=stack, build_id=build_id)
    if runner is not None:
        try:
            res = runner()
            if hasattr(res, "__await__"):
                import asyncio
                # Keep a strong reference so the build task isn't garbage-
                # collected mid-run, and retrieve any exception on completion.
                task = asyncio.ensure_future(res)
                _BUILD_TASKS.add(task)
                task.add_done_callback(_reap_build_task)
            dispatched = True
        except Exception:  # noqa: BLE001 - never let a build crash the API
            dispatched = False

    await state.event_bus.emit(
        EventType.BUILD_STARTED,
        source="web.api",
        payload={"build_id": build_id, "brief": rec.brief, "slug": rec.slug, "stack": rec.stack},
        correlation_id=build_id,
    )
    if not dispatched:
        rec.status = "queued_no_studio"
    return {"build_id": build_id, "status": rec.status, "dispatched": dispatched}


async def list_builds(state: AppState, limit: int = 25) -> dict[str, Any]:
    builds: list[dict[str, Any]] = []
    # Live cache first.
    cached = sorted(state.builds.values(), key=lambda r: r.updated_at, reverse=True)
    builds.extend(r.to_dict() for r in cached[:limit])
    # Augment with persisted history when memory is available.
    seen = {b["build_id"] for b in builds}
    if state.memory is not None and hasattr(state.memory, "recent_builds"):
        try:  # pragma: no cover - depends on memory backend
            for row in await state.memory.recent_builds(limit=limit):
                bid = str(row.get("build_id", ""))
                if bid and bid not in seen:
                    builds.append(row)
                    seen.add(bid)
        except Exception:  # noqa: BLE001
            pass
    return {"builds": builds[:limit]}


async def list_projects(state: AppState) -> dict[str, Any]:
    from skyn3t.studio.cleanup import _dir_size, _load_manifest
    pdir = Path(state.settings.projects_dir)
    out: list[dict[str, Any]] = []
    if pdir.is_dir():
        for d in sorted(p for p in pdir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            man = _load_manifest(d)
            m = man or {}
            extra = m.get("extra") or {}
            out.append({
                "slug": m.get("slug", d.name),
                "stack": m.get("stack", ""),
                "status": m.get("status", ""),
                "verdict": m.get("verdict", ""),
                "score": m.get("score") or 0.0,
                "created_at": m.get("created_at", ""),
                "updated_at": m.get("updated_at", ""),
                "size_bytes": _dir_size(d),
                "has_preview": (d / "index.html").exists(),
                "has_manifest": man is not None,
                # Spec 2 cost attribution (None when a build predates it).
                "cost_usd": extra.get("build_cost_usd"),
                "wasted_usd": extra.get("wasted_usd"),
            })
    return {"projects": out}


async def delete_project(state: AppState, slug: str) -> dict[str, Any]:
    projects_root = Path(state.settings.projects_dir).resolve()
    target = (projects_root / slug).resolve()
    if target == projects_root or not target.is_relative_to(projects_root):
        raise ValueError(f"invalid slug: {slug!r}")
    if not target.is_dir():
        raise FileNotFoundError(slug)
    _TERMINAL = frozenset(
        {"failed", "completed", "completed_no_go", "cancelled", "approved", "rejected"}
    )
    active = {getattr(r, "slug", "") for r in state.builds.values()
              if getattr(r, "status", "") not in _TERMINAL}
    if target.name not in active and slug not in active:
        # Also check the persisted store for non-terminal builds not in memory
        # (e.g. the server restarted mid-build).
        _store = getattr(state, "memory", None)
        if _store is not None and hasattr(_store, "recent_builds"):
            try:
                rows = await _store.recent_builds(limit=200)
                for row in rows:
                    if row.get("slug") in (target.name, slug):
                        if row.get("status") not in _TERMINAL:
                            active.add(row["slug"])
                            break
            except Exception:  # noqa: BLE001 - guard must never crash the delete path
                pass
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


# ---------------------------------------------------------------------------
# Live workspace: serve a delivered project + improve it toward a goal.
# Wires studio/app_runner.py (Slice 2) + studio/improve.py (Slice 1) into the
# dashboard so the cockpit can show a running app next to an improve chat.
# ---------------------------------------------------------------------------

def _resolve_project_dir(state: AppState, slug: str) -> Path:
    """Resolve a project slug to its directory under projects_dir, refusing
    escapes. Raises ValueError on escape/root, FileNotFoundError when absent."""
    projects_root = Path(state.settings.projects_dir).resolve()
    target = (projects_root / slug).resolve()
    if target == projects_root or not target.is_relative_to(projects_root):
        raise ValueError(f"invalid slug: {slug!r}")
    if not target.is_dir():
        raise FileNotFoundError(slug)
    return target


def _serve_registry(state: AppState) -> dict[str, Any]:
    """slug -> RunningApp for live previews. Lazily created so the function works
    with both AppState and the SimpleNamespace used in tests."""
    reg = getattr(state, "running_apps", None)
    if reg is None:
        reg = {}
        try:
            state.running_apps = reg
        except Exception:  # noqa: BLE001 - a read-only state must not break serving
            pass
    return reg


def _app_runner(state: AppState) -> Any:
    runner = getattr(state, "app_runner", None)
    if runner is None:
        from skyn3t.studio.app_runner import AppRunner
        runner = AppRunner()
        try:
            state.app_runner = runner
        except Exception:  # noqa: BLE001
            pass
    return runner


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def serve_project(state: AppState, slug: str) -> dict[str, Any]:
    """Start a delivered project as a live localhost server, registering the
    handle so a later stop can find it. Restarting a slug supersedes the prior
    run.

    Concurrency-safe without a lock (which would bind to one event loop): a
    synchronous claim token guards the pop -> ``await start`` -> register window.
    If a racing serve takes the slot, or a stop pops our claim, while we await
    start(), we detect the lost claim afterward and tear down our own process
    instead of leaking it (review findings #1, #2)."""
    pdir = _resolve_project_dir(state, slug)
    from skyn3t.studio.app_runner import RunningApp, cleanup_serve
    registry = _serve_registry(state)
    runner = _app_runner(state)
    # Synchronously (before any await) claim the slot, superseding a *running*
    # predecessor. A bare object() is our claim identity.
    prev = registry.get(slug)
    if isinstance(prev, RunningApp):
        runner.stop(prev)
        cleanup_serve(prev)
    claim = object()
    registry[slug] = claim

    man = BuildManifest.load(pdir)
    stack = man.stack if man is not None else ""
    app = await runner.start(pdir, stack)

    if registry.get(slug) is not claim:
        # A concurrent serve superseded us, or a stop cancelled us, mid-start:
        # we no longer own the slot, so tear down our own app rather than leak it.
        if app.status == "running":
            runner.stop(app)
        cleanup_serve(app)
        return {**app.to_dict(), "slug": slug, "superseded": True}

    if app.status == "running":
        registry[slug] = app
        await state.event_bus.emit(
            EventType.SERVE_STARTED, source="web.api",
            payload={"slug": slug, "url": app.url, "port": app.port, "kind": app.kind},
        )
    else:
        registry.pop(slug, None)  # release the claim
        cleanup_serve(app)        # unlink a failed start's temp logfile (#7)
    return {**app.to_dict(), "slug": slug}


async def stop_serve(state: AppState, slug: str) -> dict[str, Any]:
    """Stop a live preview server and release its child + temp logfile. Popping
    an in-flight start *claim* cancels that start — serve_project tears itself
    down when it finds the slot gone (review finding #2)."""
    from skyn3t.studio.app_runner import RunningApp, cleanup_serve
    registry = _serve_registry(state)
    app = registry.pop(slug, None)
    if app is None:
        return {"slug": slug, "stopped": False}
    if not isinstance(app, RunningApp):
        # Popped an in-flight claim: the in-progress serve will self-cancel.
        return {"slug": slug, "stopped": True}
    runner = _app_runner(state)
    runner.stop(app)
    cleanup_serve(app)
    await state.event_bus.emit(
        EventType.SERVE_STOPPED, source="web.api",
        payload={"slug": slug, "port": app.port},
    )
    return {"slug": slug, "stopped": True}


async def serve_status(state: AppState) -> dict[str, Any]:
    """List live preview servers, skipping in-flight claims and pruning any
    whose process has died."""
    from skyn3t.studio.app_runner import RunningApp, cleanup_serve
    registry = _serve_registry(state)
    running: list[dict[str, Any]] = []
    for slug, app in list(registry.items()):
        if not isinstance(app, RunningApp):
            continue  # an in-flight start claim, not yet a live app
        if app.pid is not None and not _pid_alive(app.pid):
            cleanup_serve(app)  # reap the zombie + unlink the temp logfile (no leak)
            registry.pop(slug, None)
            continue
        running.append({**app.to_dict(), "slug": slug})
    return {"running": running}


async def improve_project(state: AppState, slug: str, goal: str) -> dict[str, Any]:
    """Dispatch an 'improve this project toward a goal' run as a background task,
    streaming IMPROVE_* events to the cockpit. Returns immediately with a
    correlation id; the project is validated up front so a bad slug 404s before
    any work starts."""
    if not goal or not goal.strip():
        raise ValueError("goal is required")
    _resolve_project_dir(state, slug)  # validate; raises ValueError/FileNotFoundError
    if getattr(state, "orchestrator", None) is None:
        return {"accepted": False, "slug": slug, "reason": "orchestrator unavailable"}
    from skyn3t.studio.improve import ImproveEngine
    engine = ImproveEngine(
        state.event_bus, state.orchestrator,
        settings=state.settings,
        memory=getattr(state, "memory", None),
        skills=getattr(state, "skills", None),
        rag=getattr(state, "rag", None),
    )
    cid = uuid.uuid4().hex
    import asyncio
    task = asyncio.ensure_future(engine.improve(slug, goal.strip(), correlation_id=cid))
    _IMPROVE_TASKS.add(task)
    task.add_done_callback(_reap_improve_task)
    return {"accepted": True, "slug": slug, "goal": goal.strip(), "correlation_id": cid}


async def fanout_project(state: AppState, brief: str, stacks: list[str]) -> dict[str, Any]:
    """Dispatch a Spec 4 fan-out — build N divergent stack candidates for one
    brief in parallel — as a background task streaming FANOUT_* events to the
    cockpit. Each candidate builds to a distinct slug so they don't collide."""
    if not brief or not brief.strip():
        raise ValueError("brief is required")
    ids = [str(s).strip() for s in (stacks or []) if str(s).strip()]
    if len(ids) < 2:
        raise ValueError("at least two stacks are required to fan out")
    if getattr(state, "studio", None) is None:
        return {"accepted": False, "brief": brief.strip(), "reason": "studio unavailable"}
    from skyn3t.studio.fanout import FanCandidate, fan_out
    from skyn3t.studio.runner import _slugify
    cands = [FanCandidate(id=s, label=s, spec={"stack": s}) for s in ids]
    cid = uuid.uuid4().hex
    base = _slugify(brief)

    async def build_fn(c):
        stack = (c.spec or {}).get("stack", "")
        return await state.studio.start(
            brief.strip(), slug=f"{base}-{c.id}",
            extra={"stack": stack} if stack else {})

    import asyncio
    task = asyncio.ensure_future(
        fan_out(cands, build_fn, event_bus=state.event_bus, correlation_id=cid))
    _FANOUT_TASKS.add(task)
    task.add_done_callback(_reap_fanout_task)
    return {"accepted": True, "brief": brief.strip(), "stacks": ids, "correlation_id": cid}


def _resolve_live_gate(state: AppState, build_id: str, approved: bool, reason: str) -> int:
    """Resolve pending approval(s) on the live runner's in-process gate, so an
    out-of-band approve/reject actually UNBLOCKS the waiting build instead of
    only recording a status + event (and then waiting out the gate timeout).
    Returns how many gate decisions were applied (0 if nothing was pending)."""
    gate = getattr(getattr(state, "studio", None), "approval_gate", None)
    if gate is None:
        return 0
    resolved = 0
    try:
        for pending in gate.pending(build_id):
            approval_id = pending.get("approval_id")
            if not approval_id:
                continue
            ok = gate.approve(approval_id, reason) if approved else gate.reject(approval_id, reason)
            if ok:
                resolved += 1
    except Exception:  # noqa: BLE001 - gate resolution must never break the API
        pass
    return resolved


async def approve_build(state: AppState, build_id: str, approved: bool = True, reason: str = "") -> dict[str, Any]:
    rec = state.builds.get(build_id)
    db_row: dict | None = None
    if rec is None and state.memory is not None and hasattr(state.memory, "get_build"):
        # Build not in the live in-memory cache (e.g. server restarted) — look it
        # up in the persisted store so a dashboard-visible build is always approvable.
        try:
            db_row = await state.memory.get_build(build_id)
        except Exception:  # noqa: BLE001 - store lookup must never crash the endpoint
            db_row = None
    if rec is None and db_row is None:
        raise KeyError(build_id)
    new_status = "approved" if approved else "rejected"
    correlation_id: str | None = None
    if rec is not None:
        rec.status = new_status
        rec.updated_at = time.time()
        correlation_id = rec.correlation_id
    else:
        # Persist the status change back into the store so the DB reflects it.
        correlation_id = build_id
        if state.memory is not None and hasattr(state.memory, "save_build"):
            try:
                await state.memory.save_build(build_id=build_id, status=new_status)
            except Exception:  # noqa: BLE001
                pass
    # Reattach to the live gated build: resolve its in-process approval gate so a
    # blocked build resumes at once rather than waiting out the gate timeout.
    gate_resolved = _resolve_live_gate(state, build_id, approved, reason)
    await state.event_bus.emit(
        EventType.PROPOSAL_DECIDED,
        source="web.api",
        payload={"build_id": build_id, "approved": approved, "reason": reason,
                 "kind": "build_approval", "gate_resolved": gate_resolved},
        correlation_id=correlation_id,
    )
    return {"build_id": build_id, "status": new_status, "gate_resolved": gate_resolved}


async def list_proposals(state: AppState, status: str = "") -> dict[str, Any]:
    items = list(state.proposals.values())
    if status:
        items = [p for p in items if p.status == status]
    items.sort(key=lambda p: p.created_at, reverse=True)
    return {"proposals": [p.to_dict() for p in items]}


async def stacks_payload(state: AppState) -> dict[str, Any]:
    """The real-builder stacks the build console can fan out across.

    Source of truth is ``REAL_BUILDER_STACKS`` (id -> one-line "best for" hint).
    Imported lazily so this module stays import-light; any import failure degrades
    to an empty list rather than breaking the dashboard. Dict order is preserved
    so the picker lists stacks in the curated order.
    """
    try:
        from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS

        return {"stacks": [{"id": k, "description": v} for k, v in REAL_BUILDER_STACKS.items()]}
    except Exception:  # noqa: BLE001
        return {"stacks": []}


async def cortex_effects_payload(state: AppState) -> dict[str, Any]:
    """What cortex has actually changed — the visible proof the loops took effect.

    Reads three durable sources under ``data/``: the learned-router leaderboard
    (fed per successful build stage), persisted tuning overrides, and evolved
    agent instructions. Each source degrades to ``{}`` independently so a missing
    or corrupt file never breaks the dashboard.
    """
    data_dir = state.settings.data_dir
    leaderboard: dict[str, Any] = {}
    try:
        from skyn3t.intelligence.model_tournament import ModelTournament

        leaderboard = ModelTournament(data_dir / "model_tournament.json").snapshot()
    except Exception:  # noqa: BLE001
        leaderboard = {}
    tuning: dict[str, Any] = {}
    try:
        from skyn3t.cortex.tuning_store import load_overrides

        tuning = load_overrides(data_dir)
    except Exception:  # noqa: BLE001
        tuning = {}
    prompts: dict[str, Any] = {}
    try:
        from skyn3t.cortex.prompt_store import load_prompt_overrides

        prompts = load_prompt_overrides(data_dir)
    except Exception:  # noqa: BLE001
        prompts = {}
    return {"leaderboard": leaderboard, "tuning": tuning, "prompts": prompts}


async def decide_proposal(state: AppState, proposal_id: str, approved: bool, reason: str = "", decided_by: str = "api") -> dict[str, Any]:
    rec = state.proposals.get(proposal_id)
    if rec is None:
        # Allow deciding an unseen proposal id so cortex can be authoritative.
        rec = ProposalRecord(proposal_id=proposal_id, kind="unknown", summary="")
        state.proposals[proposal_id] = rec
    rec.status = "approved" if approved else "rejected"
    rec.reason = reason
    rec.decided_by = decided_by
    rec.decided_at = time.time()

    cortex = state.cortex
    if cortex is not None and hasattr(cortex, "decide"):
        try:  # pragma: no cover - depends on sibling package
            res = cortex.decide(proposal_id=proposal_id, approved=approved, reason=reason)
            if hasattr(res, "__await__"):
                await res
        except Exception:  # noqa: BLE001
            pass

    await state.event_bus.emit(
        EventType.PROPOSAL_DECIDED,
        source="web.api",
        payload={"proposal_id": proposal_id, "approved": approved, "reason": reason, "kind": rec.kind},
    )
    return {"proposal_id": proposal_id, "status": rec.status}


async def list_skills(state: AppState) -> dict[str, Any]:
    skills = state.skills
    patterns: list[dict[str, Any]] = []
    scoreboard = getattr(getattr(state, "patterns", None), "scoreboard", None)
    if scoreboard is not None:
        try:
            raw_patterns = scoreboard()
            if hasattr(raw_patterns, "__await__"):
                raw_patterns = await raw_patterns
            patterns = [p for p in list(raw_patterns or []) if isinstance(p, dict)]
        except Exception:  # noqa: BLE001
            patterns = []
    # SkillLibrary exposes .all() -> list[Skill]; serialize for the SPA.
    getter = getattr(skills, "all", None) or getattr(skills, "list_skills", None)
    if skills is not None and getter is not None:
        try:
            res = getter()
            if hasattr(res, "__await__"):
                res = await res
            out = []
            for s in res:
                if isinstance(s, dict):
                    out.append(s)
                else:
                    title = getattr(s, "title", "")
                    body = str(getattr(s, "body", "") or "")
                    out.append({
                        "slug": getattr(s, "slug", ""),
                        "title": title,
                        "name": title,  # SPA card reads s.name
                        "stack": getattr(s, "stack", ""),
                        "description": body[:160] + ("…" if len(body) > 160 else ""),
                        "tags": list(getattr(s, "tags", []) or []),
                        "score": getattr(s, "score", 0),
                        "source": getattr(s, "source", ""),
                    })
            return {"skills": out, "patterns": patterns}
        except Exception:  # noqa: BLE001
            pass
    # Degraded: surface configured skill-hub paths from settings.
    paths = [p for p in state.settings.skills_hub_paths.split(",") if p.strip()]
    return {"skills": [], "patterns": patterns, "hub_paths": paths}


async def knowledge_search(state: AppState, q: str, limit: int = 10) -> dict[str, Any]:
    knowledge = state.knowledge
    if knowledge is not None and hasattr(knowledge, "search"):
        try:  # pragma: no cover - depends on sibling package
            res = knowledge.search(q, limit=limit)
            if hasattr(res, "__await__"):
                res = await res
            return {"query": q, "results": list(res)}
        except Exception:  # noqa: BLE001
            pass
    # Degraded keyword scan over recent lessons when memory is present.
    results: list[dict[str, Any]] = []
    if state.memory is not None and hasattr(state.memory, "relevant_lessons"):
        try:  # pragma: no cover
            lessons = await state.memory.relevant_lessons(stack="", stage="", limit=limit)
            ql = q.lower()
            # No `or lessons` fallback: a query that matches nothing must return
            # an empty result set, not every recent lesson (false positives).
            results = [lesson for lesson in lessons if ql in str(lesson.get("text", "")).lower()]
        except Exception:  # noqa: BLE001
            results = []
    return {"query": q, "results": results[:limit], "degraded": True}


def _event_counts(state: AppState) -> dict[str, int]:
    """Return monotonic per-type event counts.

    Avoids the O(history) rescan on every scrape and, unlike scanning the
    bounded history ring, does not silently undercount once the buffer wraps.
    A counter dict is attached to the event bus and kept current via a single
    ALL-subscription registered lazily on first use; it is seeded once from the
    current history so counts reconcile with ``events_published_total``.
    """
    bus = state.event_bus
    # The bus keeps an unbounded per-type counter, incremented on every publish —
    # so this never undercounts once the bounded history ring wraps (the old
    # lazy seed-from-history did). Fall back to a history scan only for a bus
    # build that predates the counter.
    type_counts = getattr(bus, "type_counts", None)
    if type_counts is not None:
        return type_counts
    counts: dict[str, int] = {}
    for ev in bus.history():
        counts[ev.type.value] = counts.get(ev.type.value, 0) + 1
    return counts


async def metrics_payload(state: AppState) -> dict[str, Any]:
    s = state.status()
    counts = _event_counts(state)
    return {
        "events_published": state.event_bus.published_count,
        "event_counts": counts,
        "agents": s["agents"],
        "builds": s["builds"],
        "proposals_pending": s["proposals_pending"],
        "budget": state.budget_snapshot(),
    }


def render_prometheus(metrics: dict[str, Any]) -> str:
    """Render the metrics dict in Prometheus text exposition format."""
    lines: list[str] = []
    seen: set[str] = set()

    def _metric(name: str, value: Any, help_text: str, labels: str = "") -> None:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return
        # HELP/TYPE must appear at most once per metric family — the event_count
        # loop reuses one name across many label sets, and a second TYPE line for
        # the same metric makes a strict Prometheus parser reject the whole scrape.
        if name not in seen:
            lines.append(f"# HELP skyn3t_{name} {help_text}")
            lines.append(f"# TYPE skyn3t_{name} gauge")
            seen.add(name)
        lines.append(f"skyn3t_{name}{labels} {num}")

    _metric("events_published_total", metrics.get("events_published", 0), "Total events published")
    _metric("agents", metrics.get("agents", 0), "Registered agents")
    _metric("builds", metrics.get("builds", 0), "Known builds")
    _metric("proposals_pending", metrics.get("proposals_pending", 0), "Pending proposals")
    budget = metrics.get("budget", {})
    _metric("budget_spent_day_usd", budget.get("spent_day", 0.0), "USD spent today")
    _metric("budget_tokens_day", budget.get("tokens_day", 0), "Tokens used today")
    for et, count in metrics.get("event_counts", {}).items():
        safe = et.replace(".", "_").replace("*", "all")
        _metric("event_count", count, "Events by type", labels=f'{{type="{safe}"}}')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Runtime LLM configuration (set keys / switch backend from the dashboard).
# ---------------------------------------------------------------------------
_PROVIDER_FIELDS = {
    "openrouter": "openrouter_api_key",
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "kimi": "kimi_api_key",
}


def _persist_env_var(name: str, value: str) -> None:
    """Upsert ``NAME=value`` in the repo .env (best-effort; never raises)."""
    try:
        from skyn3t.config.settings import REPO_ROOT

        env = REPO_ROOT / ".env"
        lines = env.read_text().splitlines() if env.exists() else []
        out: list[str] = []
        found = False
        for ln in lines:
            stripped = ln.strip()
            # Only a real (non-comment) ``KEY=value`` assignment can match — a
            # commented line (``# KEY=...``) must be preserved verbatim, never
            # uncommented/overwritten.
            if stripped.startswith("#") or "=" not in stripped:
                out.append(ln)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key == name:
                out.append(f"{name}={value}")
                found = True
            else:
                out.append(ln)
        if not found:
            out.append(f"{name}={value}")
        env.write_text("\n".join(out) + "\n")
    except Exception:  # noqa: BLE001
        pass


async def llm_secrets_payload(state: AppState) -> dict[str, Any]:
    import os

    s = state.settings
    backend = state.llm_client.backend if state.llm_client is not None else "n/a"
    github = bool(
        getattr(s, "github_token", "")
        or os.environ.get("SKYN3T_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    return {
        "providers": {p: bool(getattr(s, f, "")) for p, f in _PROVIDER_FIELDS.items()},
        "backend": backend,
        "backend_pref": getattr(s, "llm_backend", "auto"),
        "cli_provider": getattr(s, "cli_llm_provider", "claude"),
        "github": github,
        # Image generation (Replicate): report presence only (never the token).
        # ``model`` is shown so the operator can see/override the active model.
        "replicate": bool(getattr(s, "replicate_api_token", "")),
        "replicate_model": getattr(s, "replicate_model", "") or "",
        # The pinned OpenRouter model (empty = auto). Surfaced so the model
        # dropdown can show the current selection.
        "preferred_model": getattr(s, "preferred_model", "") or "",
        # The asset-gen STEP additionally needs asset_gen on — surface it so the
        # UI can tell the user real assets won't be generated until it's enabled.
        "asset_gen": bool(getattr(s, "asset_gen", False)),
        "visual_self_heal": bool(getattr(s, "visual_self_heal", False)),
        # Whole-project agentic improve (multi-file Improve goals) — surfaced so
        # the Settings UI can toggle it without env editing.
        "improve_agentic": bool(getattr(s, "improve_agentic", True)),
    }


async def set_github_token(state: AppState, key: str, persist: bool = True) -> dict[str, Any]:
    """Set the GitHub token used by RepoScout search + repo ingest.

    Updates the live Settings object AND os.environ (so a running cortex picks it
    up on its next build) and persists to .env. Mirrors :func:`set_llm_key`.
    """
    import os

    key = (key or "").strip()
    try:
        state.settings.github_token = key
    except Exception:  # noqa: BLE001
        pass
    if key:
        os.environ["SKYN3T_GITHUB_TOKEN"] = key
    else:
        os.environ.pop("SKYN3T_GITHUB_TOKEN", None)
    if persist:
        _persist_env_var("SKYN3T_GITHUB_TOKEN", key)
    return {"configured": bool(key)}


async def set_replicate_token(
    state: AppState, token: str, model: str = "", persist: bool = True
) -> dict[str, Any]:
    """Set the Replicate token (+ optional model) used for image generation.

    Updates the live Settings object AND os.environ (so a running cortex picks it
    up on its next build) and persists to .env. ``model`` is only updated when a
    non-empty value is supplied — passing "" leaves the configured model intact.
    Mirrors :func:`set_github_token`. Returns presence + the active model (never
    the token itself).
    """
    import os

    token = (token or "").strip()
    try:
        state.settings.replicate_api_token = token
    except Exception:  # noqa: BLE001
        pass
    if token:
        os.environ["SKYN3T_REPLICATE_API_TOKEN"] = token
    else:
        os.environ.pop("SKYN3T_REPLICATE_API_TOKEN", None)
    if persist:
        _persist_env_var("SKYN3T_REPLICATE_API_TOKEN", token)

    model = (model or "").strip()
    if model:
        try:
            state.settings.replicate_model = model
        except Exception:  # noqa: BLE001
            pass
        os.environ["SKYN3T_REPLICATE_MODEL"] = model
        if persist:
            _persist_env_var("SKYN3T_REPLICATE_MODEL", model)
    return {
        "configured": bool(token),
        "model": getattr(state.settings, "replicate_model", "") or "",
    }


async def set_asset_gen(state: AppState, enabled: bool, persist: bool = True) -> dict[str, Any]:
    """Toggle Replicate asset generation on/off.

    Updates the live Settings flag AND os.environ (so a running cortex picks it
    up on its next build) and persists to .env. Mirrors :func:`set_replicate_token`.
    The asset-gen STEP additionally requires a Replicate token to actually run;
    this switch only controls the opt-in flag. Returns the new state.
    """
    import os

    enabled = bool(enabled)
    try:
        state.settings.asset_gen = enabled
    except Exception:  # noqa: BLE001
        pass
    os.environ["SKYN3T_ASSET_GEN"] = "true" if enabled else "false"
    if persist:
        _persist_env_var("SKYN3T_ASSET_GEN", "true" if enabled else "false")
    return {
        "asset_gen": enabled,
        "replicate": bool(getattr(state.settings, "replicate_api_token", "")),
    }


async def set_visual_self_heal(
    state: AppState, enabled: bool, persist: bool = True
) -> dict[str, Any]:
    """Toggle rendered-UI visual self-heal for future builds."""
    import os

    enabled = bool(enabled)
    try:
        state.settings.visual_self_heal = enabled
    except Exception:  # noqa: BLE001
        pass
    os.environ["SKYN3T_VISUAL_SELF_HEAL"] = "true" if enabled else "false"
    if persist:
        _persist_env_var("SKYN3T_VISUAL_SELF_HEAL", "true" if enabled else "false")
    return {
        "visual_self_heal": enabled,
        "visual_self_heal_max_rounds": int(
            getattr(state.settings, "visual_self_heal_max_rounds", 2)
        ),
    }


async def set_improve_agentic(
    state: AppState, enabled: bool, persist: bool = True
) -> dict[str, Any]:
    """Toggle whole-project agentic improve for future dashboard Improve runs
    (multi-file goals via the agentic tool-loop; OFF = classic single-file
    rewrites only). Same live+env+persist idiom as set_visual_self_heal."""
    import os

    enabled = bool(enabled)
    try:
        state.settings.improve_agentic = enabled
    except Exception:  # noqa: BLE001
        pass
    os.environ["SKYN3T_IMPROVE_AGENTIC"] = "true" if enabled else "false"
    if persist:
        _persist_env_var("SKYN3T_IMPROVE_AGENTIC", "true" if enabled else "false")
    return {
        "improve_agentic": enabled,
        "improve_agentic_timeout": int(
            getattr(state.settings, "improve_agentic_timeout", 900)
        ),
    }


async def set_build_metadata_overrides(
    state: AppState, app_type: str = "auto", engine: str = "auto", persist: bool = True
) -> dict[str, Any]:
    """Set optional app-type/engine labels used by future builds.

    These are metadata overrides only: they do not pin the implementation stack.
    Use "auto" to let SkyN3t infer from the brief and selected stack.
    """
    import os

    app_type = (app_type or "auto").strip().lower().replace(" ", "_")
    engine = (engine or "auto").strip().lower().replace(" ", "_")
    app_type = app_type or "auto"
    engine = engine or "auto"
    try:
        state.settings.app_type_override = app_type
        state.settings.engine_override = engine
    except Exception:  # noqa: BLE001
        pass
    os.environ["SKYN3T_APP_TYPE_OVERRIDE"] = app_type
    os.environ["SKYN3T_ENGINE_OVERRIDE"] = engine
    if persist:
        _persist_env_var("SKYN3T_APP_TYPE_OVERRIDE", app_type)
        _persist_env_var("SKYN3T_ENGINE_OVERRIDE", engine)
    return {"app_type_override": app_type, "engine_override": engine}


_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "models": None}


async def list_openrouter_models(state: AppState) -> dict[str, Any]:
    """The LIVE OpenRouter model list — always current, so the newest models show
    up automatically with no maintenance. Needs an OpenRouter key; returns [] with
    a note otherwise. Cached 5 min so opening Settings doesn't re-hit OpenRouter."""
    import time

    key = (getattr(state.settings, "openrouter_api_key", "") or "").strip()
    if not key:
        return {"models": [], "note": "set an OpenRouter key to load the model list"}
    now = time.time()
    if _MODELS_CACHE["models"] is not None and now - _MODELS_CACHE["ts"] < 300:
        return {"models": _MODELS_CACHE["models"], "cached": True}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            r.raise_for_status()
            ids = sorted(
                str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id")
            )
        _MODELS_CACHE.update(ts=now, models=ids)
        return {"models": ids, "count": len(ids)}
    except Exception as exc:  # noqa: BLE001 - degrade to cached/empty, never 500
        return {"models": _MODELS_CACHE.get("models") or [],
                "note": f"could not load models: {exc}"}


async def set_preferred_model(state: AppState, model: str = "") -> dict[str, Any]:
    """Pin the OpenRouter model skyn3t uses (empty = auto: the learned router picks
    per tier/task). Persisted so it survives a restart."""
    import os

    model = (model or "").strip()
    try:
        state.settings.preferred_model = model
    except Exception:  # noqa: BLE001
        pass
    os.environ["SKYN3T_PREFERRED_MODEL"] = model
    _persist_env_var("SKYN3T_PREFERRED_MODEL", model)
    return {"preferred_model": model}


async def clear_proposals(state: AppState, scope: str = "resolved") -> dict[str, Any]:
    """Drop cached proposals. ``scope='resolved'`` keeps genuinely-pending ones;
    ``scope='all'`` clears everything. In-memory only — never raises."""
    if scope == "all":
        n = len(state.proposals)
        state.proposals.clear()
        return {"cleared": n, "remaining": 0}
    drop = [pid for pid, rec in state.proposals.items() if rec.status != "pending"]
    for pid in drop:
        state.proposals.pop(pid, None)
    return {"cleared": len(drop), "remaining": len(state.proposals)}


async def scout_now(state: AppState, topic: str = "") -> dict[str, Any]:
    """Trigger one GitHub RepoScout pass immediately (on-demand), bypassing the
    periodic timer. Returns how many ingest proposals it filed. Never raises."""
    cortex = state.cortex
    if cortex is None:
        return {"scouted": 0, "error": "cortex not running"}
    scout = next(
        (c for c in getattr(cortex, "_components", []) if type(c).__name__ == "RepoScout"),
        None,
    )
    if scout is None:
        return {"scouted": 0, "error": "repo scout unavailable"}
    # No explicit topic -> rotate through the scout's topic list so repeated
    # clicks vary instead of re-proposing the same repos for one fixed topic.
    topic = (topic or "").strip()
    if not topic and hasattr(scout, "_next_topic"):
        topic = scout._next_topic()
    topic = topic or "python cli tool"
    try:
        proposals = await scout.scout(topic)
        return {"scouted": len(proposals), "topic": topic}
    except Exception as exc:  # noqa: BLE001
        return {"scouted": 0, "topic": topic, "error": str(exc)[:160]}


async def set_llm_key(state: AppState, provider: str, key: str, persist: bool = True) -> dict[str, Any]:
    field = _PROVIDER_FIELDS.get((provider or "").lower())
    if field is None:
        raise ValueError(f"unknown provider {provider!r}")
    key = (key or "").strip()
    setattr(state.settings, field, key)
    if state.llm_client is not None:
        try:
            state.llm_client.settings = state.settings  # same singleton, kept explicit
        except Exception:  # noqa: BLE001
            pass
    if persist:
        _persist_env_var(f"SKYN3T_{field.upper()}", key)
    backend = state.llm_client.backend if state.llm_client is not None else "n/a"
    return {"provider": provider.lower(), "configured": bool(key), "backend": backend}


# Messaging channels read their credentials from the environment via
# integrations.channels.env_token(); configuring one means persisting to .env
# AND setting os.environ so it takes effect without a restart. (Live bot
# delivery is wired separately — this is the config surface.)
_CHANNEL_ENV = {
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "discord": ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"),
    "slack": ("SLACK_BOT_TOKEN", "SLACK_CHANNEL"),
}


async def integrations_payload(state: AppState) -> dict[str, Any]:
    import os

    channels = {}
    for name, (tok, tgt) in _CHANNEL_ENV.items():
        configured = bool(os.environ.get(f"SKYN3T_{tok}") or os.environ.get(tok))
        target_set = bool(os.environ.get(f"SKYN3T_{tgt}") or os.environ.get(tgt))
        channels[name] = {"configured": configured, "target_set": target_set}
    listener = {"running": False, "available": state.messaging is not None}
    if state.messaging is not None:
        try:
            listener.update(state.messaging.status())
        except Exception:  # noqa: BLE001
            pass
    return {"channels": channels, "listener": listener}


async def messaging_control(state: AppState, action: str) -> dict[str, Any]:
    if state.messaging is None:
        raise ValueError("messaging service unavailable")
    if action == "start":
        res = state.messaging.start_listeners()
        return await res if hasattr(res, "__await__") else res
    if action == "stop":
        return state.messaging.stop()
    if action == "test":
        sent = await state.messaging.notify("🔔 SkyN3t test notification — messaging is wired.")
        return {"sent": sent}
    raise ValueError(f"unknown action {action!r}")


async def set_integration_credential(
    state: AppState, channel: str, token: str = "", target: str = "", persist: bool = True
) -> dict[str, Any]:
    import os

    env = _CHANNEL_ENV.get((channel or "").lower())
    if env is None:
        raise ValueError(f"unknown channel {channel!r}")
    tok_var, tgt_var = env
    if token:
        os.environ[f"SKYN3T_{tok_var}"] = token.strip()
        if persist:
            _persist_env_var(f"SKYN3T_{tok_var}", token.strip())
    if target:
        os.environ[f"SKYN3T_{tgt_var}"] = target.strip()
        if persist:
            _persist_env_var(f"SKYN3T_{tgt_var}", target.strip())
    return {
        "channel": channel.lower(),
        "configured": bool(token) or bool(os.environ.get(f"SKYN3T_{tok_var}")),
        "target_set": bool(target) or bool(os.environ.get(f"SKYN3T_{tgt_var}")),
    }


async def set_llm_backend(state: AppState, backend: str, persist: bool = True) -> dict[str, Any]:
    backend = (backend or "auto").lower()
    state.settings.llm_backend = backend
    if state.llm_client is not None:
        try:
            state.llm_client.settings = state.settings
        except Exception:  # noqa: BLE001
            pass
    if persist:
        _persist_env_var("SKYN3T_LLM_BACKEND", backend)
    active = state.llm_client.backend if state.llm_client is not None else backend
    return {"requested": backend, "active": active}


# ---------------------------------------------------------------------------
# Compatibility payloads for the SPA's endpoint names.
# ---------------------------------------------------------------------------
async def health_payload(state: AppState) -> dict[str, Any]:
    base = await status_payload(state)
    backend = state.llm_client.backend if state.llm_client is not None else "stub"
    active = sum(1 for b in state.builds.values() if b.status in ("queued", "running"))
    return {**base, "ok": True, "backend": backend, "llm_backend": backend,
            "active_builds": active, "agent_count": base.get("agents", 0)}


async def brain_payload(state: AppState) -> dict[str, Any]:
    agents = len(state.orchestrator.agents) if state.orchestrator else 0
    lessons = 0
    documents = 0
    try:
        if state.memory is not None:
            rows = await state.memory.recent_builds(limit=200)
            documents = len(rows)
            if hasattr(state.memory, "count_lessons"):
                lessons = await state.memory.count_lessons()
    except Exception:  # noqa: BLE001
        pass
    return {
        "activity": state.event_bus.published_count,
        "agents": agents,
        "builds": len(state.builds),
        "documents": documents,
        "lessons": lessons,
        "proposals": len(state.proposals),
    }


async def gates_payload(state: AppState) -> dict[str, Any]:
    """Every end-of-build gate with its enable flag and stack set — driven by
    the registry (core.stacks.GATES), so a NEW gate surfaces in the GUI with
    zero route changes (the settings_payload curated-list drift, avoided)."""
    from skyn3t.core.stacks import GATES

    return {"gates": [
        {
            "gate": spec.name,
            "flag": spec.settings_flag,
            "enabled": bool(getattr(state.settings, spec.settings_flag, True)),
            "stacks": sorted(spec.stacks),
            "via_headless_gate": spec.via_headless_gate,
        }
        for spec in GATES
    ]}


async def set_gate_enabled(
    state: AppState, gate: str, enabled: bool, persist: bool = True
) -> dict[str, Any]:
    """Toggle one gate's enable flag by gate NAME or settings flag — GUI-first
    config (the user rule), allowlisted against the registry so this can never
    setattr an arbitrary settings field. Persists via the same env idiom as the
    other toggles (SKYN3T_<FLAG>)."""
    import os

    from skyn3t.core.stacks import GATES

    wanted = (gate or "").strip().lower()
    spec = next(
        (s for s in GATES if s.name == wanted or s.settings_flag == wanted), None)
    if spec is None:
        known = sorted({s.name for s in GATES})
        raise ValueError(f"unknown gate {gate!r}; known gates: {', '.join(known)}")
    enabled = bool(enabled)
    try:
        setattr(state.settings, spec.settings_flag, enabled)
    except Exception:  # noqa: BLE001 - a frozen/validated model must not 500 the route
        pass
    env_key = f"SKYN3T_{spec.settings_flag.upper()}"
    os.environ[env_key] = "true" if enabled else "false"
    if persist:
        _persist_env_var(env_key, "true" if enabled else "false")
    return await gates_payload(state)


async def settings_payload(state: AppState) -> dict[str, Any]:
    s = state.settings
    keys = ("free_only", "no_claude", "execution_backend", "autonomous_builds",
            "approval_gates", "per_build_usd_cap", "daily_usd_cap", "llm_backend",
            "auto_route", "model_evolution", "app_type_override", "engine_override",
            "visual_self_heal", "visual_self_heal_max_rounds",
            "improve_agentic", "improve_agentic_timeout")
    return {k: getattr(s, k, None) for k in keys}


# ---------------------------------------------------------------------------
# FastAPI wiring (only constructed when FastAPI is importable).
# ---------------------------------------------------------------------------
def build_router(state: AppState) -> Any:
    """Build and return an ``APIRouter`` bound to ``state``.

    Raises :class:`RuntimeError` if FastAPI is not installed — callers should
    only reach this from within an app whose creation already required FastAPI.
    """
    if not _HAVE_FASTAPI:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "FastAPI is not installed; install 'fastapi' to use the web API router."
        )

    router = APIRouter(prefix="/api")

    async def require_auth(request: Request) -> None:
        client_host = request.client.host if request.client else None
        ok = check_auth(
            state.settings,
            authorization=request.headers.get("authorization"),
            client_host=client_host,
        )
        if not ok:
            raise HTTPException(status_code=401, detail="unauthorized")

    auth = Depends(require_auth)
    empty_body: Any = Body(default_factory=dict)

    @router.get("/status", dependencies=[auth])
    async def _status() -> dict[str, Any]:
        return await status_payload(state)

    @router.get("/agents", dependencies=[auth])
    async def _agents() -> dict[str, Any]:
        return await agents_payload(state)

    @router.get("/llm/backends", dependencies=[auth])
    async def _llm_backends() -> dict[str, Any]:
        return await llm_backends_payload(state)

    @router.get("/budget", dependencies=[auth])
    async def _budget() -> dict[str, Any]:
        return await budget_payload(state)

    # ---- SPA compatibility aliases (frontend endpoint names) ------------
    @router.get("/health", dependencies=[auth])
    async def _health() -> dict[str, Any]:
        return await health_payload(state)

    @router.get("/brain", dependencies=[auth])
    async def _brain() -> dict[str, Any]:
        return await brain_payload(state)

    @router.get("/settings", dependencies=[auth])
    async def _settings() -> dict[str, Any]:
        return await settings_payload(state)

    @router.get("/builds", dependencies=[auth])
    async def _builds_alias(limit: int = Query(default=25, ge=1, le=200)) -> dict[str, Any]:
        return await list_builds(state, limit=limit)

    @router.post("/builds", dependencies=[auth])
    async def _build_alias(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await submit_build(
                state,
                brief=str(body.get("brief", "")),
                stack=str(body.get("stack", "")),
                slug=str(body.get("slug", "")),
                reference_image=str(body.get("reference_image", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/preview/{slug}", dependencies=[auth])
    async def _preview(slug: str) -> dict[str, Any]:
        try:
            return await preview_payload(state, slug)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid slug") from None

    @router.get("/projects/cleanup", dependencies=[auth])
    async def _cleanup_report() -> dict[str, Any]:
        from skyn3t.studio.cleanup import scan as cleanup_scan
        wt = state.settings.projects_dir.parent / ".skyn3t_worktrees"
        active = sorted({getattr(r, "slug", "") for r in state.builds.values()
                         if getattr(r, "status", "") == "running" and getattr(r, "slug", "")})
        rep = cleanup_scan(state.settings.projects_dir, wt, active_slugs=active)
        return {n: [{"path": str(i.path), "reason": i.reason, "size_bytes": i.size_bytes}
                    for i in getattr(rep, n)]
                for n in ("failed", "superseded", "orphaned_worktrees",
                          "orphaned_projects", "stray_previews")}

    @router.post("/projects/cleanup", dependencies=[auth])
    async def _cleanup_apply(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        from skyn3t.studio.cleanup import apply as cleanup_apply
        from skyn3t.studio.cleanup import scan as cleanup_scan
        wt = state.settings.projects_dir.parent / ".skyn3t_worktrees"
        active = sorted({getattr(r, "slug", "") for r in state.builds.values()
                         if getattr(r, "status", "") == "running" and getattr(r, "slug", "")})
        rep = cleanup_scan(state.settings.projects_dir, wt, active_slugs=active)
        trash = state.settings.projects_dir.parent / ".skyn3t_trash"
        res = cleanup_apply(rep, trash_dir=trash,
                            dry_run=bool(body.get("dry_run", True)),
                            categories=body.get("categories"))
        return {"moved": res.moved, "freed_bytes": res.freed_bytes, "dry_run": res.dry_run}

    @router.get("/projects", dependencies=[auth])
    async def _projects() -> dict[str, Any]:
        try:
            return await list_projects(state)
        except OSError as exc:
            # An unreadable projects_dir must not leak a 500 with internals.
            raise HTTPException(status_code=500, detail="unable to read projects directory") from exc

    @router.delete("/projects/{slug}", dependencies=[auth])
    async def _delete_project(slug: str) -> dict[str, Any]:
        try:
            return await delete_project(state, slug)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid or active project") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None
        except OSError as exc:
            # e.g. the trash dir isn't writable — a controlled error, not a 500 leak.
            # (FileNotFoundError is caught above; this covers the other OSErrors.)
            raise HTTPException(status_code=500, detail="failed to trash project") from exc

    @router.get("/projects/{slug}/{path:path}", dependencies=[auth])
    async def _project_file(slug: str, path: str) -> Any:
        try:
            resolved = resolve_project_file(state, slug, path)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid path") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None
        return FileResponse(str(resolved))

    @router.get("/cortex/proposals", dependencies=[auth])
    async def _cortex_proposals(status: str = Query(default="")) -> dict[str, Any]:
        return await list_proposals(state, status=status)

    @router.get("/cortex/effects", dependencies=[auth])
    async def _cortex_effects() -> dict[str, Any]:
        return await cortex_effects_payload(state)

    @router.get("/stacks", dependencies=[auth])
    async def _stacks() -> dict[str, Any]:
        return await stacks_payload(state)

    @router.post("/cortex/proposals/{proposal_id}/decide", dependencies=[auth])
    async def _cortex_decide(
        proposal_id: str, body: dict[str, Any] = empty_body
    ) -> dict[str, Any]:
        decision = str(body.get("decision", body.get("approved", ""))).lower()
        approved = decision in ("approve", "approved", "accept", "true", "yes", "1")
        return await decide_proposal(
            state, proposal_id=proposal_id, approved=approved,
            reason=str(body.get("reason", "")), decided_by="dashboard",
        )

    @router.get("/llm/secrets", dependencies=[auth])
    async def _llm_secrets() -> dict[str, Any]:
        return await llm_secrets_payload(state)

    @router.post("/llm/key", dependencies=[auth])
    async def _set_llm_key(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await set_llm_key(state, str(body.get("provider", "")), str(body.get("key", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/llm/backend", dependencies=[auth])
    async def _set_llm_backend(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_llm_backend(state, str(body.get("backend", "auto")))

    @router.post("/settings/github", dependencies=[auth])
    async def _set_github(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_github_token(state, str(body.get("token", body.get("key", ""))))

    @router.post("/settings/replicate", dependencies=[auth])
    async def _set_replicate(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_replicate_token(
            state,
            str(body.get("token", body.get("key", ""))),
            model=str(body.get("model", "")),
        )

    @router.post("/settings/asset_gen", dependencies=[auth])
    async def _set_asset_gen(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_asset_gen(state, bool(body.get("enabled", body.get("on", False))))

    @router.post("/settings/visual_self_heal", dependencies=[auth])
    async def _set_visual_self_heal(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_visual_self_heal(
            state, bool(body.get("enabled", body.get("on", False))))

    @router.post("/settings/improve_agentic", dependencies=[auth])
    async def _set_improve_agentic(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_improve_agentic(
            state, bool(body.get("enabled", body.get("on", False))))

    @router.get("/gates", dependencies=[auth])
    async def _gates() -> dict[str, Any]:
        return await gates_payload(state)

    @router.post("/settings/gate", dependencies=[auth])
    async def _set_gate(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await set_gate_enabled(
                state,
                str(body.get("gate", body.get("flag", ""))),
                bool(body.get("enabled", body.get("on", False))),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/settings/build_metadata", dependencies=[auth])
    async def _set_build_metadata(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_build_metadata_overrides(
            state,
            app_type=str(body.get("app_type", body.get("app_type_override", "auto"))),
            engine=str(body.get("engine", body.get("engine_override", "auto"))),
        )

    @router.get("/models", dependencies=[auth])
    async def _models() -> dict[str, Any]:
        return await list_openrouter_models(state)

    @router.post("/settings/model", dependencies=[auth])
    async def _set_model(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_preferred_model(state, str(body.get("model", "")))

    @router.post("/proposals/clear", dependencies=[auth])
    async def _clear_proposals(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await clear_proposals(state, scope=str(body.get("scope", "resolved")))

    @router.post("/cortex/proposals/clear", dependencies=[auth])
    async def _clear_proposals_alias(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await clear_proposals(state, scope=str(body.get("scope", "resolved")))

    @router.post("/cortex/scout", dependencies=[auth])
    async def _scout_now(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await scout_now(state, topic=str(body.get("topic", "")))

    @router.get("/integrations", dependencies=[auth])
    async def _integrations() -> dict[str, Any]:
        return await integrations_payload(state)

    @router.post("/integrations/credential", dependencies=[auth])
    async def _set_integration(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await set_integration_credential(
                state, str(body.get("channel", "")),
                token=str(body.get("token", "")), target=str(body.get("target", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/integrations/listener", dependencies=[auth])
    async def _messaging_control(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await messaging_control(state, str(body.get("action", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/studio/build", dependencies=[auth])
    async def _build(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await submit_build(
                state,
                brief=str(body.get("brief", "")),
                stack=str(body.get("stack", "")),
                slug=str(body.get("slug", "")),
                reference_image=str(body.get("reference_image", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/studio/builds", dependencies=[auth])
    async def _builds(limit: int = Query(default=25, ge=1, le=200)) -> dict[str, Any]:
        return await list_builds(state, limit=limit)

    @router.post("/studio/approve", dependencies=[auth])
    async def _approve(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await approve_build(
                state,
                build_id=str(body.get("build_id", "")),
                approved=bool(body.get("approved", True)),
                reason=str(body.get("reason", "")),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="build not found") from None

    @router.post("/studio/serve", dependencies=[auth])
    async def _serve(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await serve_project(state, str(body.get("slug", "")))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid slug") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None

    @router.post("/studio/serve/stop", dependencies=[auth])
    async def _serve_stop(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await stop_serve(state, str(body.get("slug", "")))

    @router.get("/studio/serve", dependencies=[auth])
    async def _serve_status() -> dict[str, Any]:
        return await serve_status(state)

    @router.post("/studio/improve", dependencies=[auth])
    async def _improve(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await improve_project(
                state, str(body.get("slug", "")), str(body.get("goal", "")))
        except ValueError:
            raise HTTPException(status_code=422, detail="slug and goal are required") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None

    @router.post("/studio/fanout", dependencies=[auth])
    async def _fanout(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        stacks = body.get("stacks")
        if isinstance(stacks, str):
            stacks = [s.strip() for s in stacks.split(",") if s.strip()]
        try:
            return await fanout_project(state, str(body.get("brief", "")), stacks or [])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/proposals", dependencies=[auth])
    async def _proposals(status: str = Query(default="")) -> dict[str, Any]:
        return await list_proposals(state, status=status)

    @router.post("/proposals/decide", dependencies=[auth])
    async def _decide(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        pid = str(body.get("proposal_id", ""))
        if not pid:
            raise HTTPException(status_code=422, detail="proposal_id is required")
        return await decide_proposal(
            state,
            proposal_id=pid,
            approved=bool(body.get("approved", False)),
            reason=str(body.get("reason", "")),
            decided_by=str(body.get("decided_by", "api")),
        )

    @router.get("/skills", dependencies=[auth])
    async def _skills() -> dict[str, Any]:
        return await list_skills(state)

    @router.get("/knowledge/search", dependencies=[auth])
    async def _knowledge(
        q: str = Query(default=""),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict[str, Any]:
        return await knowledge_search(state, q=q, limit=limit)

    @router.get("/metrics", dependencies=[auth])
    async def _metrics(request: Request) -> Any:
        data = await metrics_payload(state)
        accept = request.headers.get("accept", "")
        if "text/plain" in accept or request.query_params.get("format") == "prometheus":
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(render_prometheus(data))
        return data

    # Trajectory replay / time-travel backend hooks (2.0 backlog P2).
    @router.get("/trajectory", dependencies=[auth])
    async def _trajectory(
        limit: int = Query(default=200, ge=1, le=2000),
        type: str = Query(default=""),
        correlation_id: str = Query(default=""),
        since: float | None = Query(default=None),
        until: float | None = Query(default=None),
    ) -> dict[str, Any]:
        et: EventType | None = None
        if type:
            try:
                et = EventType(type)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"unknown event type: {type}") from None
            # ALL ('*') is a subscription-only wildcard — no real event has that
            # type, so filtering on it returns nothing. Treat it as "no filter".
            if et == EventType.ALL:
                et = None
        events = state.trajectory(
            limit=limit,
            event_type=et,
            correlation_id=correlation_id or None,
            since=since,
            until=until,
        )
        return {"events": events, "count": len(events)}

    return router
