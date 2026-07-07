"""REST endpoints for the SkyN3t dashboard / control API.

FastAPI is a guarded optional dependency. :func:`build_router` only runs when
FastAPI is importable; importing this module never requires it. All handlers
read payloads defensively and degrade gracefully when a spine collaborator is
absent (design rule #6).
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import structlog

from skyn3t.atomic_io import atomic_write_text
from skyn3t.core.events import EventType
from skyn3t.studio.manifest import BuildManifest
from skyn3t.web.deps import (
    AppState,
    BuildRecord,
    ProposalRecord,
    check_auth,
    extract_bearer,
    is_loopback,
)
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
_BUILD_TASKS_BY_ID: dict[str, Any] = {}
_ENV_WRITE_LOCK = threading.RLock()
_MAX_REFERENCE_IMAGE_BYTES = 8 * 1024 * 1024


def _reap_build_task(task: Any) -> None:
    _BUILD_TASKS.discard(task)
    bid = getattr(task, "_skyn3t_build_id", "")
    if bid:
        _BUILD_TASKS_BY_ID.pop(str(bid), None)
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
    from fastapi.responses import FileResponse, Response
    _HAVE_FASTAPI = True
except Exception:  # noqa: BLE001
    APIRouter = Body = Depends = HTTPException = Query = Request = None  # type: ignore[assignment,misc]
    FileResponse = None  # type: ignore[assignment,misc]
    Response = None  # type: ignore[assignment,misc]
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


_PROJECT_REWRITE_EXTS = {".html", ".htm", ".css", ".js", ".mjs"}


def project_file_response(path: Path, slug: str) -> Any:
    """Serve a generated preview file with project-scoped asset URLs.

    The cockpit iframe loads generated HTML through `/api/projects/{slug}/...`.
    Absolute project asset refs like `/assets/hero.webp` would otherwise hit the
    dashboard's own `/assets` mount instead of the generated project. Text files
    are rewritten on the way out; binary images/fonts stay as normal file
    responses.
    """
    if path.suffix.lower() not in _PROJECT_REWRITE_EXTS:
        return FileResponse(str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return FileResponse(str(path))

    scoped = f"/api/projects/{slug}/assets/"
    for prefix in ('"/assets/', "'/assets/", "(/assets/", "url(/assets/"):
        text = text.replace(prefix, prefix.replace("/assets/", scoped))
    media_type = mimetypes.guess_type(str(path))[0] or "text/plain"
    return Response(content=text, media_type=media_type)


def _save_reference_image(
    state: AppState, build_id: str, data_url: str, *, index: int | None = None
) -> str:
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
            return ""
        import base64

        max_b64 = ((_MAX_REFERENCE_IMAGE_BYTES + 2) // 3) * 4 + 4
        if len(b64) > max_b64:
            log.warning("build.reference_image_rejected", note="reference_image too large")
            return ""
        raw = base64.b64decode(b64, validate=True)
        if len(raw) > _MAX_REFERENCE_IMAGE_BYTES:
            log.warning("build.reference_image_rejected", note="reference_image too large")
            return ""
        ext = "png"
        if "image/jpeg" in header or "image/jpg" in header:
            ext = "jpg"
        elif "image/webp" in header:
            ext = "webp"
        out_dir = Path(state.settings.data_dir) / "reference_images"
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"-{index}" if index is not None else ""
        out_path = out_dir / f"{build_id}{suffix}.{ext}"
        out_path.write_bytes(raw)
        return str(out_path)
    except Exception as exc:  # noqa: BLE001 - never let an image break a build
        log.warning("build.reference_image_save_failed", error=str(exc)[:160])
        return ""


_BUILD_PROFILES = {
    "cheap_learned",
    "fast",
    "balanced",
    "best_quality",
    "full_app",
    "manual",
}

_BUILD_TERMINAL_STATUSES = {
    "completed",
    "completed_no_go",
    "cancelled",
    "failed",
    "rejected",
    "approved",
    "interrupted",
}


def _normalize_status(value: str) -> str:
    return (value or "").strip().lower()


def _build_is_terminal(value: str) -> bool:
    return _normalize_status(value) in _BUILD_TERMINAL_STATUSES


def _normalize_build_profile(profile: str) -> str:
    profile = (profile or "cheap_learned").strip().lower().replace("-", "_")
    return profile if profile in _BUILD_PROFILES else "cheap_learned"


def _normalize_model_id(model: str, *, max_len: int = 240) -> str:
    """Normalize freeform model IDs from UI/pasteboard safely."""
    compact = "".join((model or "").split())
    if len(compact) > max_len:
        return ""
    return compact


def _is_free_model_id(model: str) -> bool:
    return (model or "").strip().lower().endswith(":free")


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _normalize_model_override(model: str) -> str:
    return _normalize_model_id(model)


def _profile_extra(profile: str) -> dict[str, Any]:
    if profile == "fast":
        return {"best_of_n": 1, "max_debug_attempts": 1, "agentic_timeout": 240}
    if profile == "balanced":
        return {
            "best_of_n": 2,
            "max_debug_attempts": 2,
            "agentic_timeout": 600,
            "asset_gen": False,
            "visual_self_heal": False,
        }
    if profile == "best_quality":
        return {
            "best_of_n": 2,
            "best_of_n_across_models": True,
            "max_debug_attempts": 3,
            "agentic_timeout": 900,
            "asset_gen": True,
            "visual_self_heal": True,
        }
    if profile == "full_app":
        return _full_app_extra()
    return {}


def _full_app_extra() -> dict[str, Any]:
    return {
        "full_app_contract": True,
        "asset_gen": True,
        "best_of_n": 2,
        "best_of_n_across_models": True,
        "max_debug_attempts": 4,
        "agentic_timeout": 1200,
        "visual_self_heal": True,
    }


async def submit_build(state: AppState, brief: str, stack: str = "", slug: str = "",
                       reference_image: str = "", reference_images: list[str] | None = None,
                       build_profile: str = "cheap_learned",
                       model_override: str = "", full_app: bool = False) -> dict[str, Any]:
    """Queue a build. Uses the studio if wired, else records + emits an event.

    ``reference_image`` is an optional base64 ``data:`` URL; ``reference_images``
    accepts a small ordered list. Valid images are saved and threaded into the
    build so the design agent can match them. Absent -> unchanged behavior.
    """
    if not brief or not brief.strip():
        raise ValueError("brief is required")
    build_id = state.new_build_id()
    profile = _normalize_build_profile(build_profile)
    model = _normalize_model_override(model_override)
    full_app_requested = bool(full_app) or profile == "full_app"
    rec = BuildRecord(
        build_id=build_id,
        brief=brief.strip(),
        slug=slug.strip(),
        stack=stack.strip(),
        status="queued",
        build_profile=profile,
        model_trace={
            "profile": profile,
            "model_override": model,
            "backend": getattr(state.settings, "llm_backend", ""),
            "full_app": full_app_requested,
        },
        correlation_id=build_id,
    )
    state.builds[build_id] = rec

    # Prefer a wired StudioRunner (async start(brief, slug=None, extra=None)),
    # falling back to a legacy submit(...) if present. The build runs as a
    # background task so the endpoint returns immediately with the build_id.
    # Optional reference image: decode + save (degrades to data-URL pass-through).
    ref_path = ""
    ref_paths: list[str] = []
    if reference_image and reference_image.strip():
        ref_path = _save_reference_image(state, build_id, reference_image.strip())
    raw_refs = [str(item) for item in (reference_images or [])[:4] if str(item).strip()]
    for idx, raw_ref in enumerate(raw_refs, start=1):
        saved = _save_reference_image(state, build_id, raw_ref.strip(), index=idx)
        if saved:
            ref_paths.append(saved)
    if ref_path and ref_path not in ref_paths:
        ref_paths.insert(0, ref_path)
    if ref_paths and not ref_path:
        ref_path = ref_paths[0]

    studio = state.studio
    dispatched = False
    runner = None
    build_extra = {
        "stack": stack,
        "build_id": build_id,
        "build_profile": profile,
        **_profile_extra(profile),
    }
    if full_app_requested:
        build_extra = {**build_extra, **_full_app_extra()}
    if model:
        build_extra["model_override"] = model
    if ref_paths:
        build_extra["reference_images"] = ref_paths
    if ref_path:
        build_extra["reference_image"] = ref_path
    if studio is not None:
        if hasattr(studio, "start"):
            def runner() -> Any:
                return studio.start(brief, slug=slug or None, extra=build_extra)
        elif hasattr(studio, "submit"):  # pragma: no cover - legacy shape
            def runner() -> Any:
                try:
                    return studio.submit(
                        brief=brief,
                        slug=slug,
                        stack=stack,
                        build_id=build_id,
                        extra=build_extra,
                    )
                except TypeError:
                    return studio.submit(brief=brief, slug=slug, stack=stack, build_id=build_id)
    if runner is not None:
        try:
            res = runner()
            if hasattr(res, "__await__"):
                import asyncio
                # Keep a strong reference so the build task isn't garbage-
                # collected mid-run, and retrieve any exception on completion.
                task = asyncio.ensure_future(res)
                task._skyn3t_build_id = build_id  # type: ignore[attr-defined]
                _BUILD_TASKS.add(task)
                _BUILD_TASKS_BY_ID[build_id] = task
                task.add_done_callback(_reap_build_task)
            dispatched = True
        except Exception:  # noqa: BLE001 - never let a build crash the API
            dispatched = False

    await state.event_bus.emit(
        EventType.BUILD_STARTED,
        source="web.api",
        payload={
            "build_id": build_id,
            "brief": rec.brief,
            "slug": rec.slug,
            "stack": rec.stack,
            "build_profile": profile,
            "model_trace": rec.model_trace,
        },
        correlation_id=build_id,
    )
    if not dispatched:
        rec.status = "queued_no_studio"
        await state.event_bus.emit(
            EventType.BUILD_FAILED,
            source="web.api",
            payload={
                "build_id": build_id,
                "brief": rec.brief,
                "slug": rec.slug,
                "stack": rec.stack,
                "status": rec.status,
                "error": "no StudioRunner is wired in this process",
            },
            correlation_id=build_id,
        )
    return {
        "build_id": build_id,
        "status": rec.status,
        "dispatched": dispatched,
        "build_profile": profile,
        "model_override": model,
        "full_app": full_app_requested,
    }


async def cancel_build(state: AppState, build_id: str, reason: str = "") -> dict[str, Any]:
    """Cancel a queued/running build and persist the terminal status.

    Cancellation is best-effort. If this process still owns the asyncio task, it
    is cancelled directly; if the process restarted and only a persisted row
    remains, the visible record is still marked cancelled so it does not keep
    showing as running forever.
    """
    bid = (build_id or "").strip()
    if not bid:
        raise ValueError("build_id is required")
    now = time.time()
    rec = state.builds.get(bid)
    db_row: dict[str, Any] | None = None
    if state.memory is not None and hasattr(state.memory, "get_build"):
        try:
            db_row = await state.memory.get_build(bid)
        except Exception:  # noqa: BLE001 - cancellation must not crash on store read
            db_row = None
    if rec is None and db_row is None:
        raise KeyError(bid)

    task = _BUILD_TASKS_BY_ID.pop(bid, None)
    task_cancelled = False
    if task is not None and not task.done():
        task.cancel()
        task_cancelled = True

    if rec is not None:
        rec.status = "cancelled"
        rec.updated_at = now

    if state.memory is not None and hasattr(state.memory, "save_build"):
        manifest = None
        if isinstance(db_row, dict) and isinstance(db_row.get("manifest"), dict):
            manifest = dict(db_row["manifest"])
            manifest["status"] = "cancelled"
            manifest["cancelled_at"] = now
            manifest["cancel_reason"] = reason
        try:
            fields: dict[str, Any] = {"build_id": bid, "status": "cancelled"}
            if manifest is not None:
                fields["manifest"] = manifest
            await state.memory.save_build(**fields)
        except Exception:  # noqa: BLE001
            pass

    await state.event_bus.emit(
        EventType.PROPOSAL_DECIDED,
        source="web.api",
        payload={
            "build_id": bid,
            "status": "cancelled",
            "reason": reason,
            "kind": "build_cancel",
            "task_cancelled": task_cancelled,
        },
        correlation_id=bid,
    )
    return {"build_id": bid, "status": "cancelled", "task_cancelled": task_cancelled}


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


def _cleanup_build_ids_from_payload(payload: dict[str, Any] | None) -> list[str]:
    if payload is None:
        return []
    ids = payload.get("build_ids")
    if ids is None:
        return []
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, (list, tuple)):
        return []
    out: list[str] = []
    for raw in ids:
        item = (str(raw) if raw is not None else "").strip()
        if item and item not in out:
            out.append(item)
    return out


async def _build_for_cleanup(state: AppState, build_id: str) -> dict[str, Any] | None:
    bid = (build_id or "").strip()
    if not bid:
        return None
    rec = state.builds.get(bid)
    if rec is not None:
        return rec.to_dict()
    memory = getattr(state, "memory", None)
    if memory is None or not hasattr(memory, "get_build"):
        return None
    try:
        return await memory.get_build(bid)  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001 - db read failures should not crash cleanup
        return None


async def delete_build(state: AppState, build_id: str) -> dict[str, Any]:
    bid = (build_id or "").strip()
    if not bid:
        raise ValueError("build_id is required")

    row = await _build_for_cleanup(state, bid)
    if row is None:
        raise KeyError(bid)

    if not _build_is_terminal(row.get("status", "")):
        raise ValueError("build is active")

    state.builds.pop(bid, None)
    memory = getattr(state, "memory", None)
    if memory is not None and hasattr(memory, "delete_build"):
        await memory.delete_build(bid)  # type: ignore[func-returns-value]

    return {"build_id": bid, "deleted": True}


async def cleanup_builds(
    state: AppState,
    *,
    build_ids: list[str] | None = None,
    all_terminal: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    requested = build_ids or []
    if not requested and not all_terminal:
        raise ValueError("build_id or all_terminal is required")

    if all_terminal:
        rows = (await list_builds(state, limit=limit)).get("builds", [])
        for row in rows:
            rid = str(row.get("build_id") or "").strip()
            if not rid or not _build_is_terminal(row.get("status", "")):
                continue
            if rid not in requested:
                requested.append(rid)

    deleted = []
    blocked = []
    missing = []

    for bid in requested:
        try:
            await delete_build(state, bid)
            deleted.append(bid)
        except ValueError:
            blocked.append(bid)
        except KeyError:
            missing.append(bid)

    return {
        "requested": requested,
        "deleted": deleted,
        "blocked": blocked,
        "missing": missing,
    }


async def list_projects(state: AppState) -> dict[str, Any]:
    from skyn3t.studio.cleanup import _dir_size, _load_manifest
    from skyn3t.studio.proof_run import detect_offline_starter_stub
    pdir = Path(state.settings.projects_dir)
    out: list[dict[str, Any]] = []
    if pdir.is_dir():
        for d in sorted(p for p in pdir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            man = _load_manifest(d)
            m = man or {}
            extra = m.get("extra") or {}
            skills_used = extra.get("skills_used") or []
            recall_used = extra.get("recall_used") or []
            stage_skills_used = extra.get("stage_skills_used") or {}
            prompts = extra.get("prompts") or []
            status = str(m.get("status", "") or "")
            verdict = str(m.get("verdict", "") or "")
            proof = extra.get("proof") if isinstance(extra, dict) else None
            proof_detail = proof.get("detail") if isinstance(proof, dict) else None
            scaffold_stub_reason = (
                proof_detail.get("scaffold_stub")
                if isinstance(proof_detail, dict)
                else None
            )
            if not scaffold_stub_reason:
                scaffold_stub_reason = detect_offline_starter_stub(
                    d, str(m.get("stack", "") or "")
                )
            score = m.get("score") or 0.0
            if (
                not scaffold_stub_reason
                and verdict == "go"
                and str(extra.get("llm_backend", "")).lower() == "stub"
                and isinstance(prompts, list)
                and len(prompts) == 0
                and float(score) >= 74.0
            ):
                scaffold_stub_reason = (
                    "legacy build used the stub backend with zero model prompts; "
                    "it was a deterministic fallback, not a completed model-built app"
                )
            scaffold_stub_gate = (
                {
                    "triggered": True,
                    "source": "legacy_project_list",
                    "score_cap": 49.0,
                    "reason": str(scaffold_stub_reason),
                }
                if scaffold_stub_reason
                else {}
            )
            if scaffold_stub_reason:
                verdict = "no_go"
                status = "completed_no_go" if status == "completed" else status
                score = min(float(score), 49.0)
            elif status == "completed" and verdict == "no_go":
                status = "completed_no_go"
            out.append({
                "slug": m.get("slug", d.name),
                "stack": m.get("stack", ""),
                "status": status,
                "verdict": verdict,
                "score": score,
                "created_at": m.get("created_at", ""),
                "updated_at": m.get("updated_at", ""),
                "size_bytes": _dir_size(d),
                "has_preview": (d / "index.html").exists(),
                "has_manifest": man is not None,
                # Spec 2 cost attribution (None when a build predates it).
                "cost_usd": extra.get("build_cost_usd"),
                "wasted_usd": extra.get("wasted_usd"),
                # Prompts are captured per-build but can be large (10-50 KB each),
                # so the list carries only a flag/count — the text loads lazily via
                # GET /projects/{slug}/prompts when the panel is expanded.
                "prompt_count": len(prompts) if isinstance(prompts, list) else 0,
                # Existing manifest evidence that skills/Cortex guidance actually
                # reached the build. Keep the row payload compact: full prompt text
                # still loads on demand via /prompts.
                "skills_used": list(skills_used) if isinstance(skills_used, list) else [],
                "recall_used": list(recall_used) if isinstance(recall_used, list) else [],
                "stage_skills_used": dict(stage_skills_used)
                if isinstance(stage_skills_used, dict) else {},
                "quality_scorecard": dict(extra.get("quality_scorecard") or {})
                if isinstance(extra.get("quality_scorecard"), dict) else {},
                "scaffold_stub_gate": scaffold_stub_gate,
                "deploy_plan": dict(extra.get("deploy_plan") or {})
                if isinstance(extra.get("deploy_plan"), dict) else {},
                "deployments": list(extra.get("deployments") or [])
                if isinstance(extra.get("deployments"), list) else [],
                "live_url": str(extra.get("live_url") or ""),
                "deploy_check": dict(extra.get("deploy_check") or {})
                if isinstance(extra.get("deploy_check"), dict) else {},
            })
    return {"projects": out}


async def get_project_prompts(state: AppState, slug: str) -> dict[str, Any]:
    """The exact prompt(s) a build sent the model — loaded on demand (they can be
    large). Read from the project manifest's ``extra['prompts']``."""
    from skyn3t.studio.cleanup import _load_manifest
    projects_root = Path(state.settings.projects_dir).resolve()
    target = (projects_root / slug).resolve()
    if target == projects_root or not target.is_relative_to(projects_root):
        raise ValueError(f"invalid slug: {slug!r}")
    man = _load_manifest(target) or {}
    prompts = (man.get("extra") or {}).get("prompts") or []
    return {"slug": slug, "prompts": prompts}


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


async def deploy_plan_project(
    state: AppState,
    slug: str,
    *,
    target: str = "",
) -> dict[str, Any]:
    """Return the deploy plan for a delivered project without firing a deploy."""
    pdir = _resolve_project_dir(state, slug)
    man = BuildManifest.load(pdir)
    stack = man.stack if man is not None else ""
    from skyn3t.studio.deploy import plan_deploy

    plan = plan_deploy(pdir, stack, target=target or None)
    extra = man.extra if man is not None and isinstance(man.extra, dict) else {}
    return {
        "slug": slug,
        "stack": stack,
        "plan": plan.to_dict(),
        "live_url": str(extra.get("live_url") or ""),
        "deployments": list(extra.get("deployments") or [])
        if isinstance(extra.get("deployments"), list) else [],
    }


async def deploy_project(
    state: AppState,
    slug: str,
    *,
    target: str = "",
    write: bool = False,
) -> dict[str, Any]:
    """Fire the existing token-gated deploy path from the web surface."""
    pdir = _resolve_project_dir(state, slug)
    man = BuildManifest.load(pdir)
    stack = man.stack if man is not None else ""
    from skyn3t.agents.deploy_agent import DeployAgent
    from skyn3t.studio.deploy import plan_deploy, record_deployment, write_deploy_artifacts

    plan = plan_deploy(pdir, stack, target=target or None)
    if write:
        write_deploy_artifacts(plan, pdir)
    if not plan.deployable:
        return {
            "slug": slug,
            "ok": False,
            "plan": plan.to_dict(),
            "result": {"ok": False, "url": None, "error": plan.notes},
        }
    if not plan.serves_url:
        return {
            "slug": slug,
            "ok": False,
            "plan": plan.to_dict(),
            "result": {
                "ok": False,
                "url": None,
                "error": "deploy plan creates an artifact, not a live URL",
            },
        }
    provider = target or (plan.targets[0] if plan.targets else "")
    if not provider:
        return {
            "slug": slug,
            "ok": False,
            "plan": plan.to_dict(),
            "result": {"ok": False, "url": None, "error": "no deploy target available"},
        }
    agent = DeployAgent(event_bus=state.event_bus)
    result = agent.deploy(pdir, target=provider, plan=plan)
    record: dict[str, Any] | None = None
    deploy_check: dict[str, Any] | None = None
    if result.get("ok") and result.get("url"):
        record = record_deployment(pdir, result=result, plan=plan, target=provider)
        if getattr(state.settings, "deploy_check_enabled", False):
            try:
                from skyn3t.studio.deploy_check import check_deploy

                verdict = await check_deploy(str(result.get("url") or ""), stack)
                deploy_check = verdict.to_dict()
                manifest = BuildManifest.load(pdir)
                if manifest is not None:
                    manifest.extra["deploy_check"] = deploy_check
                    manifest.save(pdir)
            except Exception as exc:  # noqa: BLE001 - deploy check is advisory
                deploy_check = {
                    "ok": False,
                    "skipped": True,
                    "issues": [],
                    "checked": {},
                    "reason": f"deploy check unavailable: {str(exc)[:160]}",
                    "gaps": [],
                }
        await state.event_bus.emit(
            EventType.SYSTEM,
            source="web.api",
            payload={
                "event": "deploy.completed",
                "slug": slug,
                "url": result.get("url"),
                "target": provider,
            },
        )
    return {
        "slug": slug,
        "ok": bool(result.get("ok") and result.get("url")),
        "plan": plan.to_dict(),
        "target": provider,
        "result": result,
        "deployment": record,
        "deploy_check": deploy_check,
    }


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

    Reads durable/self-improving sources: the learned-router leaderboard (fed per
    successful build stage), persisted tuning overrides, evolved agent
    instructions, and the live learned/imported skill library. Each source
    degrades independently so a missing or corrupt file never breaks the
    dashboard.
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
    skills_effect: dict[str, Any] = {"count": 0, "items": []}
    skills = getattr(state, "skills", None)
    getter = getattr(skills, "all", None) or getattr(skills, "list_skills", None)
    if skills is not None and getter is not None:
        try:
            res = getter()
            if hasattr(res, "__await__"):
                res = await res
            items: list[dict[str, Any]] = []
            for s in list(res or []):
                if isinstance(s, dict):
                    row = {
                        "slug": s.get("slug", ""),
                        "title": s.get("title") or s.get("name", ""),
                        "stack": s.get("stack", ""),
                        "score": s.get("score", 0),
                        "source": s.get("source", ""),
                    }
                else:
                    row = {
                        "slug": getattr(s, "slug", ""),
                        "title": getattr(s, "title", ""),
                        "stack": getattr(s, "stack", ""),
                        "score": getattr(s, "score", 0),
                        "source": getattr(s, "source", ""),
                    }
                items.append(row)
            items.sort(key=lambda r: (float(r.get("score") or 0), str(r.get("title") or "")), reverse=True)
            skills_effect = {"count": len(items), "items": items[:12]}
        except Exception:  # noqa: BLE001
            skills_effect = {"count": 0, "items": []}
    return {
        "leaderboard": leaderboard,
        "tuning": tuning,
        "prompts": prompts,
        "skills": skills_effect,
    }


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


def _catalog_entry_payload(entry: Any) -> dict[str, Any]:
    return {
        "id": getattr(entry, "id", ""),
        "title": getattr(entry, "title", ""),
        "description": getattr(entry, "description", ""),
        "source_path": getattr(entry, "source_path", ""),
        "source_kind": getattr(entry, "source_kind", ""),
        "stages": list(getattr(entry, "stages", []) or []),
        "stacks": list(getattr(entry, "stacks", []) or []),
        "tags": list(getattr(entry, "tags", []) or []),
        "risk": getattr(entry, "risk", "low"),
    }


async def agent_catalog_preview(
    state: AppState, path: str, limit: int = 100
) -> dict[str, Any]:
    """Preview a local external agent catalog without executing or importing it."""
    raw = (path or "").strip()
    if not raw:
        return {"path": "", "summary": {"entries": 0, "by_stack": {}, "by_stage": {}, "by_risk": {}}, "entries": []}
    catalog_path = Path(raw).expanduser()
    if not catalog_path.is_absolute():
        catalog_path = (Path.cwd() / catalog_path).resolve()
    if not catalog_path.is_dir():
        raise ValueError("catalog path must be a readable directory")
    from skyn3t.intelligence.agent_catalog import catalog_summary, discover_catalog_entries

    entries = discover_catalog_entries(catalog_path, limit=max(1, min(int(limit or 100), 500)))
    return {
        "path": str(catalog_path),
        "summary": catalog_summary(entries),
        "entries": [_catalog_entry_payload(e) for e in entries[:100]],
    }


async def import_agent_catalog(
    state: AppState, path: str, limit: int = 100
) -> dict[str, Any]:
    """Import a local external agent catalog as compact advisory skills."""
    if state.skills is None or not hasattr(state.skills, "add"):
        raise ValueError("a writable skill library is required to import catalogs")
    preview = await agent_catalog_preview(state, path, limit=limit)
    from skyn3t.intelligence.agent_catalog import import_catalog_as_skills

    imported = import_catalog_as_skills(
        preview["path"],
        state.skills,
        limit=max(1, min(int(limit or 100), 500)),
    )
    return {
        "path": preview["path"],
        "imported": imported,
        "summary": preview["summary"],
    }


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


async def auth_self_test_payload(
    state: AppState,
    *,
    authorization: str | None = None,
    client_host: str | None = None,
) -> dict[str, Any]:
    """Report the effective auth path for a request that already passed auth."""
    bearer = extract_bearer(authorization)
    token_configured = bool(getattr(state.settings, "auth_token", "").strip())
    return {
        "ok": check_auth(
            state.settings,
            authorization=authorization,
            client_host=client_host,
        ),
        "token_configured": token_configured,
        "method": "bearer" if bearer else "loopback" if is_loopback(client_host) else "none",
        "client_host": client_host or "",
    }


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
        with _ENV_WRITE_LOCK:
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
            atomic_write_text(env, "\n".join(out) + "\n")
    except Exception:  # noqa: BLE001
        pass


async def llm_secrets_payload(state: AppState) -> dict[str, Any]:
    import os

    s = state.settings
    backend = state.llm_client.backend if state.llm_client is not None else "n/a"
    routing = {}
    if state.llm_client is not None and hasattr(state.llm_client, "backend_status"):
        try:
            routing = state.llm_client.backend_status()
        except Exception:  # noqa: BLE001
            routing = {}
    github = bool(
        getattr(s, "github_token", "")
        or os.environ.get("SKYN3T_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    providers = {
        p: bool(
            getattr(s, f, "")
            or os.environ.get(f"SKYN3T_{f.upper()}")
            or os.environ.get(f.upper())
        )
        for p, f in _PROVIDER_FIELDS.items()
    }
    return {
        "providers": providers,
        "backend": backend,
        "routing": routing,
        "backend_pref": getattr(s, "llm_backend", "auto"),
        "cli_provider": getattr(s, "cli_llm_provider", "claude"),
        "codegen_cli_provider": getattr(s, "codegen_cli_provider", "") or "",
        "codegen_cli_model": getattr(s, "codegen_cli_model", "") or "",
        "openrouter_codegen_model": getattr(s, "openrouter_codegen_model", "") or "",
        "free_only": bool(getattr(s, "free_only", True)),
        "model_pins": {
            "cheap": getattr(s, "model_cheap", "") or "",
            "ui": getattr(s, "model_ui", "") or "",
            "backend": getattr(s, "model_backend", "") or "",
            "strong": getattr(s, "model_strong", "") or "",
            "docs": getattr(s, "model_docs", "") or "",
        },
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


_MODELS_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "models": None,
    "catalog": None,
    "note": None,
}


def _format_openrouter_unit_cost(raw: Any) -> str | None:
    cost = _parse_openrouter_unit_cost(raw)
    if cost is None:
        return None
    if cost <= 0:
        return "free"
    # OpenRouter pricing is often "$/token"; convert to per-1M-token labels for
    # quick human comparison in UI.
    return f"${cost * 1_000_000:.6g}/M"


def _parse_openrouter_unit_cost(raw: Any) -> float | None:
    try:
        cost = float(raw)
    except (TypeError, ValueError):
        return None
    if cost <= 0:
        return 0.0
    return cost


def _normalize_openrouter_pricing(model_record: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(model_record, dict):
        return {}
    raw = model_record.get("pricing")
    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, float] = {}
    for key in (
        "prompt",
        "completion",
        "input",
        "output",
        "request",
        "image",
        "search",
        "cached_prompt",
        "cached_completion",
    ):
        value = _parse_openrouter_unit_cost(raw.get(key))
        if value is None:
            continue
        normalized[key] = value
    return normalized


def _estimate_openrouter_cost(
    pricing: dict[str, float],
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    if not pricing:
        return None

    prompt_rate = pricing.get("prompt")
    if prompt_rate is None:
        prompt_rate = pricing.get("input")
    completion_rate = pricing.get("completion")
    if completion_rate is None:
        completion_rate = pricing.get("output")

    if prompt_rate is None and completion_rate is None:
        if "request" in pricing:
            return pricing.get("request")
        return None

    prompt_cost = max(0, int(prompt_tokens)) * float(prompt_rate or 0.0)
    completion_cost = max(0, int(completion_tokens)) * float(completion_rate or 0.0)
    return round(prompt_cost + completion_cost, 10)


def _model_pricing_summary(model_record: dict[str, Any] | None) -> str:
    if not isinstance(model_record, dict):
        return "price unknown"
    model_id = str(model_record.get("id") or "")
    if model_id.endswith(":free"):
        return "free"

    pricing = model_record.get("pricing")
    if not isinstance(pricing, dict):
        return "price unknown"

    keys = (
        "prompt",
        "completion",
        "input",
        "output",
        "request",
        "image",
        "search",
        "cached_prompt",
        "cached_completion",
    )
    parts: list[str] = []
    for key in keys:
        if key not in pricing:
            continue
        label = key.replace("_", " ")
        val = _format_openrouter_unit_cost(pricing.get(key))
        if val is not None:
            parts.append(f"{label}:{val}")
    if not parts:
        return "price unknown"
    return " · ".join(parts)


def _catalog_model_provider(model_id: str) -> str:
    return (model_id.split("/", 1)[0].strip() if "/" in model_id else "").lower()


def _catalog_model_family(model_id: str) -> str:
    return model_id.split("/", 1)[1].split(":", 1)[0].split("-", 1)[0].strip() if "/" in model_id else ""


def _build_model_catalog_item(model_record: dict[str, Any]) -> dict[str, Any]:
    model_id = str(model_record.get("id") or "").strip()
    pricing = _normalize_openrouter_pricing(model_record)
    prompt_rate = pricing.get("prompt", pricing.get("input"))
    completion_rate = pricing.get("completion", pricing.get("output"))
    request_rate = pricing.get("request")
    return {
        "id": model_id,
        "provider": _catalog_model_provider(model_id),
        "family": _catalog_model_family(model_id),
        "created": int(model_record.get("created", 0) or 0),
        "is_free": model_id.endswith(":free"),
        "pricing_summary": _model_pricing_summary(model_record),
        "pricing_raw": pricing,
        "prompt_rate": prompt_rate,
        "completion_rate": completion_rate,
        "request_rate": request_rate,
    }

async def _load_openrouter_catalog(
    state: AppState,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    import time

    from skyn3t.adapters.llm import openrouter_key

    now = time.time()
    if (
        not force_refresh
        and _MODELS_CACHE["catalog"] is not None
        and now - float(_MODELS_CACHE["ts"]) < 300
    ):
        return _MODELS_CACHE["catalog"], _MODELS_CACHE.get("note")

    try:
        import httpx

        key = openrouter_key(state.settings).strip()
        attempts: list[dict[str, dict[str, str]]] = [
            {"headers": {"User-Agent": "skyn3t"}},
        ]
        if key:
            attempts.insert(
                0,
                {"headers": {"Authorization": f"Bearer {key}", "User-Agent": "skyn3t"}},
            )

        async with httpx.AsyncClient(timeout=10) as client:
            last_exc = None
            for attempt in attempts:
                try:
                    r = await client.get(
                        "https://openrouter.ai/api/v1/models",
                        headers=attempt["headers"],
                    )
                    r.raise_for_status()
                    raw = r.json().get("data") or []
                    catalog = [
                        dict(item)
                        for item in raw
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    ]
                    _MODELS_CACHE.update(
                        ts=now,
                        models=[str(m.get("id")) for m in catalog if isinstance(m.get("id"), str)],
                        catalog=catalog,
                        note="ok",
                    )
                    return catalog, "ok"
                except Exception as exc:  # noqa: BLE001 - try each auth mode, then fail safely
                    last_exc = exc
                    continue

            note = f"could not load OpenRouter catalog: {last_exc}"
            catalog = _MODELS_CACHE.get("catalog") or []
            _MODELS_CACHE.update(ts=now, note=note)
            return catalog, note
    except Exception as exc:  # noqa: BLE001 - degrade to cache/empty on API/network
        note = f"could not load OpenRouter catalog: {exc}"
        catalog = _MODELS_CACHE.get("catalog") or []
        _MODELS_CACHE.update(ts=now, note=note)
        return catalog, note


async def list_openrouter_models(
    state: AppState,
    refresh: bool = False,
) -> dict[str, Any]:
    """The LIVE OpenRouter model list — always current, so the newest models show
    up automatically with no maintenance. Cached 5 min so opening Settings
    doesn't re-hit OpenRouter."""
    try:
        catalog, note = await _load_openrouter_catalog(state, refresh)
    except TypeError:
        catalog, note = await _load_openrouter_catalog(state)
    models = sorted({str(m.get("id")) for m in catalog if isinstance(m.get("id"), str)})
    payload = {"models": models, "count": len(models)}
    if note:
        payload["note"] = note
    return payload


async def list_openrouter_model_catalog(
    state: AppState,
    *,
    query: str = "",
    provider: str = "",
    family: str = "",
    only_free: bool = False,
    sort: str = "id",
    order: str = "asc",
    limit: int = 200,
    offset: int = 0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    try:
        catalog, note = await _load_openrouter_catalog(state, force_refresh)
    except TypeError:
        catalog, note = await _load_openrouter_catalog(state)
    normalized_query = (query or "").strip().lower()
    normalized_provider = (provider or "").strip().lower()
    normalized_family = (family or "").strip().lower()
    normalized_order = (order or "asc").strip().lower()
    normalized_sort = (sort or "id").strip().lower()

    filtered: list[dict[str, Any]] = []
    for m in catalog:
        if not isinstance(m, dict):
            continue
        raw_model_id = m.get("id")
        if not isinstance(raw_model_id, str):
            continue
        model_id = raw_model_id.strip()
        if not model_id:
            continue
        model_id_l = model_id.lower()
        provider_name = _catalog_model_provider(model_id)
        family_name = _catalog_model_family(model_id)
        if only_free and not model_id_l.endswith(":free"):
            continue
        if normalized_query and normalized_query not in model_id_l:
            continue
        if normalized_provider and normalized_provider != provider_name:
            continue
        if normalized_family and normalized_family not in family_name.lower():
            continue

        filtered.append(_build_model_catalog_item(m))

    def _sort_key(item: dict[str, Any]) -> tuple[Any, Any, Any]:
        if normalized_sort in {"created", "newest"}:
            return (item.get("created", 0), item["provider"], item["id"])
        if normalized_sort == "provider":
            return (item["provider"], item["family"], item["id"])
        if normalized_sort == "price":
            unknown = float("inf")
            prompt_rate = item.get("prompt_rate")
            completion_rate = item.get("completion_rate")
            request_rate = item.get("request_rate")
            primary: float | int
            secondary: float | int
            if isinstance(request_rate, (int, float)):
                primary = float(request_rate)
                secondary = 0.0
            else:
                primary = float(prompt_rate) if isinstance(prompt_rate, (int, float)) else unknown
                secondary = float(completion_rate) if isinstance(completion_rate, (int, float)) else unknown
            return (primary, secondary, item["id"])
        return (item["provider"], item["id"])

    filtered.sort(key=_sort_key, reverse=(normalized_order == "desc"))
    start = max(0, offset)
    if limit <= 0:
        page: list[dict[str, Any]] = filtered[:0]
    else:
        page = filtered[start:start + limit]

    return {
        "items": page,
        "count": len(filtered),
        "returned": len(page),
        "offset": start,
        "limit": limit,
        "note": note or "",
    }

async def model_routing_preview_payload(
    state: AppState,
    build_profile: str = "cheap_learned",
    model_override: str = "",
) -> dict[str, Any]:
    """Resolve the currently selected model per router tier and attach pricing.

    This lets Studio show exactly what will be sent to OpenRouter before submit,
    including manual/model-preference override, backend/policy context, and pricing tags.
    """
    from skyn3t.core.model_router import (
        _FREE_DEFAULTS,
        _PAID_DEFAULTS,
        ModelRouter,
        Tier,
    )

    profile = _normalize_build_profile(build_profile)
    override = _normalize_model_id(model_override)
    preferred = _normalize_model_id(str(getattr(state.settings, "preferred_model", "") or ""))
    free_only = bool(getattr(state.settings, "free_only", True))
    backend = getattr(
        state.llm_client,
        "backend",
        "stub" if state.llm_client is None else "unknown",
    )
    catalog, catalog_note = await _load_openrouter_catalog(state)
    catalog_map = {str(m.get("id")): m for m in catalog if isinstance(m, dict) and m.get("id")}
    catalog_age_seconds = None
    try:
        if _MODELS_CACHE.get("ts"):
            import time

            catalog_age_seconds = max(0.0, time.time() - float(_MODELS_CACHE["ts"]))
    except Exception:  # noqa: BLE001 - defensive
        catalog_age_seconds = None

    tier_models: list[dict[str, Any]] = []
    try:
        router = state.router
        if router is None:
            router = ModelRouter(state.settings)
    except Exception:
        router = None

    for tier in Tier:
        model = ""
        source = ""
        blocked_paid_pin = False
        if override:
            if not free_only or _is_free_model_id(override):
                model = override
                source = "manual"
            else:
                blocked_paid_pin = True
        elif preferred:
            if not free_only or _is_free_model_id(preferred):
                model = preferred
                source = "preferred"
            else:
                blocked_paid_pin = True
        if not model and router is not None:
            try:
                try:
                    model = router.resolve(tier, profile=profile)
                except TypeError:
                    model = router.resolve(tier)
            except Exception as exc:  # noqa: BLE001 - fallback on failures
                log.warning("routing_preview_resolve_error", error=str(exc)[:120], tier=tier.value)
                model = _normalize_model_id(
                    _PAID_DEFAULTS[tier]
                    if not free_only
                    else _FREE_DEFAULTS[tier]
                )
            source = "free_only" if blocked_paid_pin else "learned"
        elif not model:
            model = _normalize_model_id(
                _PAID_DEFAULTS[tier]
                if not free_only
                else _FREE_DEFAULTS[tier]
            )
            # Ensure no empty model propagates to the UI when fallback data is
            # unexpectedly missing.
            if not model:
                model = _FREE_DEFAULTS[tier]
            source = "free_only" if blocked_paid_pin else "fallback"

        pricing = _normalize_openrouter_pricing(catalog_map.get(str(model)))
        tier_models.append({
            "tier": tier.value,
            "model": str(model),
            "source": source,
            "pricing": _model_pricing_summary(catalog_map.get(str(model))),
            "pricing_raw": pricing,
            "cost_estimates_usd": {
                "prompt_1k_completion_1k": _estimate_openrouter_cost(
                    pricing,
                    prompt_tokens=1000,
                    completion_tokens=1000,
                ),
                "prompt_5k_completion_2k": _estimate_openrouter_cost(
                    pricing,
                    prompt_tokens=5000,
                    completion_tokens=2000,
                ),
                "prompt_20k_completion_8k": _estimate_openrouter_cost(
                    pricing,
                    prompt_tokens=20_000,
                    completion_tokens=8_000,
                ),
            },
        })

    return {
        "backend": backend,
        "build_profile": profile,
        "model_override": override,
        "preferred_model": preferred,
        "free_only": free_only,
        "catalog_note": catalog_note,
        "catalog_age_seconds": catalog_age_seconds,
        "catalog_model_count": len(catalog),
        "tiers": tier_models,
    }


async def resolve_openrouter_model(state: AppState, model: str) -> dict[str, Any]:
    """Check if a model id is in the live/saved OpenRouter catalog.

    This is intentionally permissive: when catalog access is unavailable (missing
    key or temporary API issues), resolution returns ``available=False`` with a
    hint. Build inputs are still accepted — this is validation, not a hard stop.
    """
    normalized = _normalize_model_id(model)
    if not normalized:
        return {"model": "", "available": True, "status": "auto"}

    listed = await list_openrouter_models(state)
    catalog = listed.get("models")
    if not isinstance(catalog, list):
        catalog = []
    available = normalized in catalog
    if available:
        return {
            "model": normalized,
            "available": True,
            "status": "known",
            "note": "exact match in OpenRouter catalog",
        }

    lower = normalized.lower()
    suggestions = []
    for model_id in catalog:
        if lower in model_id.lower():
            suggestions.append(model_id)
    if not suggestions:
        short = lower.split("/", 1)[-1]
        suggestions = [m for m in catalog if short and short in m.lower()]
    if not suggestions:
        # Deterministic fallback keeps the payload small and stable.
        suggestions = catalog[:3]
    if len(suggestions) > 5:
        suggestions = suggestions[:5]

    note = listed.get("note") or "not in cached OpenRouter catalog"
    if listed.get("note") and listed.get("count") is None and listed.get("cached") is None:
        # If catalog resolution failed for network reasons, keep the reason explicit.
        note = str(listed.get("note"))
    return {
        "model": normalized,
        "available": False,
        "status": "unknown",
        "suggestions": suggestions,
        "note": note,
    }


async def set_preferred_model(state: AppState, model: str = "") -> dict[str, Any]:
    """Pin the OpenRouter model skyn3t uses (empty = auto: the learned router picks
    per tier/task). Persisted so it survives a restart."""
    import os

    model = _normalize_model_id(model)
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
    import os

    field = _PROVIDER_FIELDS.get((provider or "").lower())
    if field is None:
        raise ValueError(f"unknown provider {provider!r}")
    key = (key or "").strip()
    setattr(state.settings, field, key)
    env_name = f"SKYN3T_{field.upper()}"
    if state.llm_client is not None:
        try:
            state.llm_client.settings = state.settings  # same singleton, kept explicit
        except Exception:  # noqa: BLE001
            pass
    if persist:
        if key:
            os.environ[env_name] = key
        else:
            os.environ.pop(env_name, None)
        _persist_env_var(env_name, key)
    if field == "openrouter_api_key":
        # Avoid serving stale model lists after key rotation.
        _MODELS_CACHE.update(ts=0.0, models=None)
    backend = state.llm_client.backend if state.llm_client is not None else "n/a"
    routing = (
        state.llm_client.backend_status()
        if state.llm_client is not None and hasattr(state.llm_client, "backend_status")
        else {}
    )
    return {
        "provider": provider.lower(),
        "configured": bool(key),
        "backend": backend,
        "routing": routing,
    }


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
    import os

    backend = (backend or "auto").lower()
    state.settings.llm_backend = backend
    os.environ["SKYN3T_LLM_BACKEND"] = backend
    if state.llm_client is not None:
        try:
            state.llm_client.settings = state.settings
        except Exception:  # noqa: BLE001
            pass
    if persist:
        _persist_env_var("SKYN3T_LLM_BACKEND", backend)
    active = state.llm_client.backend if state.llm_client is not None else backend
    routing = (
        state.llm_client.backend_status()
        if state.llm_client is not None and hasattr(state.llm_client, "backend_status")
        else {}
    )
    return {"requested": backend, "active": active, "routing": routing}


_MODEL_PIN_FIELDS = {
    "cheap": "model_cheap",
    "ui": "model_ui",
    "backend": "model_backend",
    "strong": "model_strong",
    "docs": "model_docs",
}
_CODEGEN_CLI_PROVIDERS = {"", "claude", "kimi", "copilot"}


async def set_llm_routing(
    state: AppState,
    *,
    codegen_cli_provider: str | None = None,
    codegen_cli_model: str | None = None,
    openrouter_codegen_model: str | None = None,
    model_pins: dict[str, Any] | None = None,
    free_only: bool | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Set model-routing controls used by future builds.

    Missing fields are left unchanged; explicit empty strings clear a
    pin/override. Values are written to the live Settings object, process env,
    and optionally .env.
    """
    import os

    updates: dict[str, str] = {}
    if codegen_cli_provider is not None:
        provider = (codegen_cli_provider or "").strip().lower()
        if provider not in _CODEGEN_CLI_PROVIDERS:
            allowed = ", ".join(sorted(p or "none" for p in _CODEGEN_CLI_PROVIDERS))
            raise ValueError(f"Unsupported codegen_cli_provider {provider!r}; use one of: {allowed}")
        updates["codegen_cli_provider"] = provider
    if codegen_cli_model is not None:
        updates["codegen_cli_model"] = _normalize_model_id(codegen_cli_model)
    if openrouter_codegen_model is not None:
        updates["openrouter_codegen_model"] = _normalize_model_id(openrouter_codegen_model)
    if model_pins is not None:
        for tier, field in _MODEL_PIN_FIELDS.items():
            if tier in model_pins:
                updates[field] = _normalize_model_id(str(model_pins.get(tier) or ""))

    for field, value in updates.items():
        try:
            setattr(state.settings, field, value)
        except Exception:  # noqa: BLE001
            pass
        env_key = f"SKYN3T_{field.upper()}"
        if persist:
            if value:
                os.environ[env_key] = value
            else:
                os.environ.pop(env_key, None)
            _persist_env_var(env_key, value)

    if free_only is not None:
        free = bool(free_only)
        try:
            state.settings.free_only = free
        except Exception:  # noqa: BLE001
            pass
        if persist:
            value = "true" if free else "false"
            os.environ["SKYN3T_FREE_ONLY"] = value
            _persist_env_var("SKYN3T_FREE_ONLY", value)

    if state.llm_client is not None:
        try:
            state.llm_client.settings = state.settings
        except Exception:  # noqa: BLE001
            pass
    if getattr(state.llm_client, "router", None) is not None:
        state.router = state.llm_client.router

    routing = (
        state.llm_client.backend_status()
        if state.llm_client is not None and hasattr(state.llm_client, "backend_status")
        else {}
    )
    tiers = {}
    if state.router is not None:
        try:
            tiers = state.router.describe()
        except Exception:  # noqa: BLE001
            tiers = {}
    return {
        "routing": routing,
        "tiers": tiers,
        "free_only": bool(getattr(state.settings, "free_only", True)),
    }


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
            "approval_gates", "per_build_usd_cap", "daily_usd_cap",
            "daily_token_cap", "autonomous_daily_build_cap", "llm_backend",
            "codegen_cli_provider", "codegen_cli_model", "openrouter_codegen_model",
            "model_cheap", "model_ui", "model_backend", "model_strong", "model_docs",
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

    @router.get("/auth/self-test", dependencies=[auth])
    async def _auth_self_test(request: Request) -> dict[str, Any]:
        client_host = request.client.host if request.client else None
        return await auth_self_test_payload(
            state,
            authorization=request.headers.get("authorization"),
            client_host=client_host,
        )

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
                reference_images=body.get("reference_images")
                if isinstance(body.get("reference_images"), list) else None,
                build_profile=str(body.get("build_profile", "")),
                model_override=str(body.get("model_override", "")),
                full_app=bool(body.get("full_app", False)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/builds/cancel", dependencies=[auth])
    async def _cancel_build_alias(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await cancel_build(
                state,
                build_id=str(body.get("build_id", "")),
                reason=str(body.get("reason", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError:
            raise HTTPException(status_code=404, detail="build not found") from None

    @router.post("/builds/rebuild", dependencies=[auth])
    async def _rebuild_alias(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await rebuild_build(
                state,
                build_id=str(body.get("build_id") or ""),
                reuse_slug=bool(body.get("reuse_slug", False)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError:
            raise HTTPException(status_code=404, detail="build not found") from None

    @router.post("/builds/cleanup", dependencies=[auth])
    async def _build_cleanup_alias(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        payload = dict(body)
        build_id = str(payload.get("build_id", "")).strip()
        ids = _cleanup_build_ids_from_payload(payload)
        if build_id and build_id not in ids:
            ids.append(build_id)
        all_terminal = bool(payload.get("all_terminal", False))
        if not ids and not all_terminal:
            raise HTTPException(status_code=422, detail="build_id or all_terminal is required")
        try:
            limit = int(payload.get("limit", 200) or 200)
        except (TypeError, ValueError):
            limit = 200

        res = await cleanup_builds(state, build_ids=ids, all_terminal=all_terminal, limit=limit)
        if len(ids) == 1 and res["deleted"]:
            return {"build_id": ids[0], "deleted": True}
        if len(ids) == 1 and not res["deleted"]:
            if res["missing"]:
                raise HTTPException(status_code=404, detail="build not found") from None
            if res["blocked"]:
                raise HTTPException(status_code=409, detail="build is active") from None
        return res

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

    # MUST be registered before the /{slug}/{path:path} catch-all below, or the
    # catch-all treats "prompts" as a file path and this never matches.
    @router.get("/projects/{slug}/prompts", dependencies=[auth])
    async def _project_prompts(slug: str) -> dict[str, Any]:
        try:
            return await get_project_prompts(state, slug)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid project") from None

    @router.get("/projects/{slug}/{path:path}", dependencies=[auth])
    async def _project_file(slug: str, path: str) -> Any:
        try:
            resolved = resolve_project_file(state, slug, path)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid path") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None
        return project_file_response(resolved, slug)

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

    @router.post("/llm/routing", dependencies=[auth])
    async def _set_llm_routing(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            free_only = _coerce_bool(body["free_only"]) if "free_only" in body else None
            return await set_llm_routing(
                state,
                codegen_cli_provider=str(body["codegen_cli_provider"]) if "codegen_cli_provider" in body else None,
                codegen_cli_model=str(body["codegen_cli_model"]) if "codegen_cli_model" in body else None,
                openrouter_codegen_model=str(body["openrouter_codegen_model"]) if "openrouter_codegen_model" in body else None,
                model_pins=body.get("model_pins") if isinstance(body.get("model_pins"), dict) else None,
                free_only=free_only,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
    async def _models(
        refresh: bool = Query(default=False),
    ) -> dict[str, Any]:
        return await list_openrouter_models(state, refresh=refresh)

    @router.get("/models/catalog", dependencies=[auth])
    async def _catalog(
        q: str = Query(default=""),
        provider: str = Query(default=""),
        family: str = Query(default=""),
        only_free: bool = Query(default=False),
        refresh: bool = Query(default=False),
        sort: str = Query(default="id"),
        order: str = Query(default="asc"),
        limit: int = Query(default=200, ge=0, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return await list_openrouter_model_catalog(
            state,
            query=q,
            provider=provider,
            family=family,
            only_free=only_free,
            force_refresh=refresh,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )

    @router.get("/models/routing-preview", dependencies=[auth])
    async def _routing_preview(
        build_profile: str = Query(default="cheap_learned"),
        model_override: str = Query(default=""),
    ) -> dict[str, Any]:
        return await model_routing_preview_payload(
            state,
            build_profile=build_profile,
            model_override=model_override,
        )

    @router.get("/models/resolve", dependencies=[auth])
    async def _resolve_model(model: str = Query(default="")) -> dict[str, Any]:
        return await resolve_openrouter_model(state, model=model)

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
                reference_images=body.get("reference_images")
                if isinstance(body.get("reference_images"), list) else None,
                build_profile=str(body.get("build_profile", "")),
                model_override=str(body.get("model_override", "")),
                full_app=bool(body.get("full_app", False)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/studio/builds", dependencies=[auth])
    async def _builds(limit: int = Query(default=25, ge=1, le=200)) -> dict[str, Any]:
        return await list_builds(state, limit=limit)

    @router.post("/studio/build/cancel", dependencies=[auth])
    async def _cancel_build(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await cancel_build(
                state,
                build_id=str(body.get("build_id", "")),
                reason=str(body.get("reason", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError:
            raise HTTPException(status_code=404, detail="build not found") from None

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

    @router.post("/studio/builds/cleanup", dependencies=[auth])
    async def _studio_build_cleanup_alias(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        payload = dict(body)
        build_id = str(payload.get("build_id", "")).strip()
        ids = _cleanup_build_ids_from_payload(payload)
        if build_id and build_id not in ids:
            ids.append(build_id)
        all_terminal = bool(payload.get("all_terminal", False))
        if not ids and not all_terminal:
            raise HTTPException(status_code=422, detail="build_id or all_terminal is required")
        try:
            limit = int(payload.get("limit", 200) or 200)
        except (TypeError, ValueError):
            limit = 200

        res = await cleanup_builds(state, build_ids=ids, all_terminal=all_terminal, limit=limit)
        if len(ids) == 1 and res["deleted"]:
            return {"build_id": ids[0], "deleted": True}
        if len(ids) == 1 and not res["deleted"]:
            if res["missing"]:
                raise HTTPException(status_code=404, detail="build not found") from None
            if res["blocked"]:
                raise HTTPException(status_code=409, detail="build is active") from None
        return res

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

    @router.get("/studio/deploy/plan", dependencies=[auth])
    async def _deploy_plan(slug: str, target: str = "") -> dict[str, Any]:
        try:
            return await deploy_plan_project(state, slug, target=target)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid slug") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None

    @router.post("/studio/deploy", dependencies=[auth])
    async def _deploy(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await deploy_project(
                state,
                str(body.get("slug", "")),
                target=str(body.get("target") or ""),
                write=bool(body.get("write", False)),
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid slug") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None

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

    @router.get("/agent-catalog", dependencies=[auth])
    async def _agent_catalog(
        path: str = Query(default=""),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        try:
            return await agent_catalog_preview(state, path, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/agent-catalog/import", dependencies=[auth])
    async def _agent_catalog_import(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await import_agent_catalog(
                state,
                str(body.get("path", "")),
                limit=int(body.get("limit", 100) or 100),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
