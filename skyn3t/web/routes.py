"""REST endpoints for the SkyN3t dashboard / control API.

FastAPI is a guarded optional dependency. :func:`build_router` only runs when
FastAPI is importable; importing this module never requires it. All handlers
read payloads defensively and degrade gracefully when a spine collaborator is
absent (design rule #6).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import structlog

from skyn3t.atomic_io import atomic_write_text
from skyn3t.core.events import EventType
from skyn3t.core.model_router import is_free_model_id as router_is_free_model_id
from skyn3t.core.model_router import prime_live_catalog
from skyn3t.process_utils import is_process_alive
from skyn3t.security.secrets import scrub_text
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.manifest import MANIFEST_FILENAME, BuildManifest
from skyn3t.web.deps import (
    AppState,
    BuildRecord,
    ProposalRecord,
    check_auth,
    extract_bearer,
    is_cross_origin_browser_request,
    is_loopback,
)
from skyn3t.web.model_value import (
    catalog_value_annotations,
    model_value_annotation,
    workload_payload,
)
from skyn3t.worktree import (
    PREVIEW_SUBDIR,
    SOURCE_TREE_EXCLUDED_DIR_NAMES,
    list_files,
    source_tree_snapshot,
)

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
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_REPLICATE_MODEL_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
)


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
_REVERIFYING_PROJECTS: set[str] = set()
_REVERIFY_WORKERS: dict[str, Any] = {}


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
_DELIVERED_PROJECT_STATUSES = frozenset({"completed", "completed_no_go"})
_ACTIVE_PROJECT_STATUSES = frozenset({"pending", "queued", "running"})
_NON_SHIPPABLE_PROJECT_STATUSES = frozenset({
    "cancelled",
    "completed_no_go",
    "failed",
    "interrupted",
    "rejected",
})
_PROJECT_SIZE_EXCLUDED_ROOTS = SOURCE_TREE_EXCLUDED_DIR_NAMES


class ProjectNotDeliveredError(RuntimeError):
    """A project directory exists, but no completed build owns it yet."""


class ProjectReverifyError(RuntimeError):
    """A project cannot safely enter the local reverify workflow."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        preserve_staging: bool = False,
        recovery_path: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.preserve_staging = preserve_staging
        self.recovery_path = recovery_path


class ProductSpecUnavailableError(RuntimeError):
    """A delivered project predates, or has lost, its durable product contract."""


class DeployPreflightError(RuntimeError):
    """A live deploy request failed validation before any side effect."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _manifest_is_delivered(manifest: BuildManifest | dict[str, Any] | None) -> bool:
    if manifest is None:
        return False
    raw = manifest.status if isinstance(manifest, BuildManifest) else manifest.get("status", "")
    return str(raw or "").strip().lower() in _DELIVERED_PROJECT_STATUSES


def _require_delivered_project(state: AppState, slug: str) -> tuple[Path, BuildManifest]:
    project = _resolve_project_dir(state, slug)
    manifest = BuildManifest.load(project)
    if not _manifest_is_delivered(manifest):
        raise ProjectNotDeliveredError(slug)
    assert manifest is not None
    return project, manifest


def _preview_root(state: AppState, slug: str) -> Path:
    """The directory served for a *delivered* project's static preview.

    Guards the SLUG against escaping ``projects_dir`` (e.g. ``slug='..'`` would
    otherwise resolve to the parent and leak a directory listing / arbitrary
    file read). Raises ``ValueError`` on escape — callers map it to HTTP 400."""
    projects_root = Path(state.settings.projects_dir).resolve()
    base = (projects_root / slug).resolve()
    if not base.is_relative_to(projects_root):
        raise ValueError(f"slug escapes projects_dir: {slug!r}")
    preview = base / PREVIEW_SUBDIR
    if not preview.is_dir():
        return base
    resolved_preview = preview.resolve()
    # Generated projects may contain symlinks/junctions. The selected preview
    # root itself must remain inside that project; checking only child paths
    # later would bless an escaped root and expose allowlisted host files.
    if not resolved_preview.is_relative_to(base):
        raise ValueError(f"preview root escapes project: {slug!r}")
    return resolved_preview


def _project_preview_capability(state: AppState, slug: str) -> str:
    key = getattr(state, "preview_signing_key", b"")
    if not isinstance(key, bytes) or len(key) < 32:
        raise RuntimeError("preview signing key is unavailable")
    digest = hmac.new(key, slug.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _project_preview_url(state: AppState, slug: str, path: str = "index.html") -> str:
    cap = _project_preview_capability(state, slug)
    return (
        f"/api/project-previews/{cap}/{quote(slug, safe='')}/"
        f"{quote(path.lstrip('/'), safe='/')}"
    )


def _valid_project_preview_capability(state: AppState, slug: str, cap: str) -> bool:
    try:
        expected = _project_preview_capability(state, slug)
    except (RuntimeError, UnicodeError):
        return False
    return hmac.compare_digest(expected, cap)


async def preview_payload(state: AppState, slug: str) -> dict[str, Any]:
    """Manifest + file tree for a completed build artifact."""
    _require_delivered_project(state, slug)
    root = _preview_root(state, slug)
    files = list_files(root) if root.is_dir() else []
    manifest = BuildManifest.load(Path(state.settings.projects_dir) / slug)
    return {
        "slug": slug,
        "root": str(root),
        "files": sorted(files),
        "manifest": manifest.to_dict() if manifest is not None else None,
        "preview_url": _project_preview_url(state, slug),
    }


def resolve_project_file(state: AppState, slug: str, rel_path: str) -> Path:
    """Resolve a preview-relative path to an absolute file, refusing escapes.

    Raises ``ValueError`` if the path escapes the preview root, ``FileNotFoundError``
    if no such file exists. This is the security boundary for the file route."""
    _require_delivered_project(state, slug)
    root = _preview_root(state, slug).resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes preview root: {rel_path!r}")
    if not candidate.is_file():
        raise FileNotFoundError(rel_path)
    relative = candidate.relative_to(root)
    lowered_parts = tuple(part.casefold() for part in relative.parts)
    if (
        any(part.startswith(".") for part in relative.parts)
        or candidate.name.casefold() in _PROJECT_PRIVATE_FILES
        or candidate.suffix.casefold() not in _PROJECT_SERVABLE_EXTS
        or any(part in {"node_modules", "__pycache__"} for part in lowered_parts)
    ):
        raise PermissionError(rel_path)
    return candidate


_PROJECT_REWRITE_EXTS = {".html", ".htm", ".css", ".js", ".mjs"}
_PROJECT_SERVABLE_EXTS = frozenset({
    ".html", ".htm", ".css", ".js", ".mjs", ".cjs",
    ".json", ".webmanifest", ".wasm",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm",
    ".gltf", ".glb", ".obj", ".mtl", ".bin",
    ".txt", ".csv", ".xml",
})
_PROJECT_PRIVATE_FILES = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "credentials.json", "secrets.json",
})
_PROJECT_SANDBOX_CSP = (
    "sandbox allow-scripts allow-modals allow-downloads allow-pointer-lock; "
    "frame-ancestors 'self'"
)


def _project_response_headers() -> dict[str, str]:
    """Isolation headers for every untrusted generated-project response.

    CSP sandbox also covers active non-HTML documents such as SVG/XML when
    opened directly. CORS/CORP remain permissive only for these project files so
    ES modules, fonts, and images can load from the sandbox's opaque origin.
    """
    return {
        "Content-Security-Policy": _PROJECT_SANDBOX_CSP,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "cross-origin",
        # Opaque-origin sandboxed documents send Origin:null for modules, fonts,
        # and fetches. Do not use "*" here: that would make guessable project
        # paths readable by every website the operator visits.
        "Access-Control-Allow-Origin": "null",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


def project_file_response(
    path: Path, slug: str, *, route_prefix: str | None = None
) -> Any:
    """Serve a generated preview file with project-scoped asset URLs.

    The cockpit iframe loads generated HTML through `/api/projects/{slug}/...`.
    Absolute project asset refs like `/assets/hero.webp` would otherwise hit the
    dashboard's own `/assets` mount instead of the generated project. Text files
    are rewritten on the way out; binary images/fonts stay as normal file
    responses.
    """
    headers = _project_response_headers()
    if path.suffix.lower() not in _PROJECT_REWRITE_EXTS:
        return FileResponse(str(path), headers=headers)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return FileResponse(str(path), headers=headers)

    scoped = route_prefix or f"/api/projects/{quote(slug, safe='')}/"
    scoped = f"{scoped.rstrip('/')}/assets/"
    for prefix in ('"/assets/', "'/assets/", "(/assets/", "url(/assets/"):
        text = text.replace(prefix, prefix.replace("/assets/", scoped))
    media_type = mimetypes.guess_type(str(path))[0] or "text/plain"
    return Response(content=text, media_type=media_type, headers=headers)


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


def _save_reference_images(
    state: AppState,
    build_id: str,
    reference_image: str = "",
    reference_images: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Persist the validated reference-image inputs shared by build entrypoints."""
    primary = ""
    saved_paths: list[str] = []
    if reference_image and reference_image.strip():
        primary = _save_reference_image(state, build_id, reference_image.strip())
    raw_refs = [
        str(item)
        for item in (reference_images or [])[:4]
        if str(item).strip()
    ]
    for index, raw_ref in enumerate(raw_refs, start=1):
        saved = _save_reference_image(
            state,
            build_id,
            raw_ref.strip(),
            index=index,
        )
        if saved:
            saved_paths.append(saved)
    if primary and primary not in saved_paths:
        saved_paths.insert(0, primary)
    if saved_paths and not primary:
        primary = saved_paths[0]
    return primary, saved_paths


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
_BUILD_FAILURE_STATUSES = {"failed", "completed_no_go"}


def _normalize_status(value: str) -> str:
    return (value or "").strip().lower()


def _build_is_terminal(value: str) -> bool:
    return _normalize_status(value) in _BUILD_TERMINAL_STATUSES


def _build_evidence_weight(value: Any) -> int:
    """Estimate how much diagnostic evidence a compact build field contains."""
    if value is None:
        return 0
    if isinstance(value, str):
        return 1 if value.strip() else 0
    if isinstance(value, dict):
        total = 0
        for item in value.values():
            weight = _build_evidence_weight(item)
            if weight:
                total += 1 + weight
        return total
    if isinstance(value, (list, tuple)):
        total = 0
        for item in value:
            weight = _build_evidence_weight(item)
            if weight:
                total += 1 + weight
        return total
    return 1


def _richer_build_evidence(live: Any, persisted: Any) -> Any:
    """Prefer persisted evidence on ties, but never replace richer live evidence."""
    if _build_evidence_weight(persisted) >= _build_evidence_weight(live):
        return persisted
    return live


def _merge_build_model_trace(live: Any, persisted: Any) -> dict[str, Any]:
    """Merge compact trace fields without dropping richer per-field evidence."""
    live_trace = dict(live) if isinstance(live, dict) else {}
    persisted_trace = dict(persisted) if isinstance(persisted, dict) else {}
    merged = dict(live_trace)
    for key, value in persisted_trace.items():
        merged[key] = _richer_build_evidence(live_trace.get(key), value)

    prompt_counts: list[int] = []
    for value in (live_trace.get("prompt_count"), persisted_trace.get("prompt_count")):
        if value is None:
            continue
        try:
            prompt_counts.append(max(0, int(value)))
        except (TypeError, ValueError):
            continue
    if prompt_counts:
        merged["prompt_count"] = max(prompt_counts)
    return merged


def _nonnegative_build_cost(value: Any) -> float | None:
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def _project_non_shippable_spend_usd(
    *,
    status: str,
    verdict: str,
    cost_usd: Any,
    persisted_value: Any = None,
) -> float | None:
    """Return recorded spend for a terminal build that produced no shippable app.

    This is cost attribution, not a claim about provider invoice certainty. Callers
    must keep exposing ``cost_truth`` alongside it for confirmed/estimated labels.
    """
    normalized_status = str(status or "").strip().lower()
    normalized_verdict = str(verdict or "").strip().lower()
    if normalized_status in _ACTIVE_PROJECT_STATUSES or normalized_status == "building":
        return None
    terminal_unshipped = (
        normalized_status in _NON_SHIPPABLE_PROJECT_STATUSES
        or (
            normalized_verdict == "no_go"
            and normalized_status in {"", "completed"}
        )
    )
    if not terminal_unshipped:
        return None
    persisted = _nonnegative_build_cost(persisted_value)
    if persisted is not None:
        return persisted
    return _nonnegative_build_cost(cost_usd)


def _cost_truth_amount(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    return _nonnegative_build_cost(value.get("llm_cost_usd"))


def _same_build_cost(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return True
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def _row_cost_truth(row: dict[str, Any]) -> dict[str, Any]:
    direct = row.get("cost_truth")
    if isinstance(direct, dict) and _build_evidence_weight(direct):
        return direct
    scorecard = row.get("quality_scorecard")
    if isinstance(scorecard, dict):
        nested = scorecard.get("cost_truth")
        if isinstance(nested, dict) and _build_evidence_weight(nested):
            return nested
    return {}


def _matching_row_cost_truth(
    row: dict[str, Any],
    cost: float,
) -> dict[str, Any]:
    truth = _row_cost_truth(row)
    truth_cost = _cost_truth_amount(truth)
    if truth_cost is not None and _same_build_cost(truth_cost, cost):
        return truth
    return {}


def _set_scorecard_cost_fields(
    row: dict[str, Any],
    *,
    cost: float | None = None,
    cost_truth: dict[str, Any] | None = None,
) -> None:
    value = row.get("quality_scorecard")
    if not isinstance(value, dict):
        return
    scorecard = dict(value)
    if cost_truth is None:
        scorecard.pop("cost_truth", None)
        scorecard.pop("cost_usd", None)
    else:
        scorecard["cost_truth"] = cost_truth
        scorecard["cost_usd"] = cost
    row["quality_scorecard"] = scorecard


def _merge_live_build_history(
    live: dict[str, Any],
    persisted: dict[str, Any],
) -> dict[str, Any]:
    """Hydrate a live-cache row with compact evidence from persisted history."""
    merged = dict(live)
    for key, value in persisted.items():
        if _build_evidence_weight(value):
            merged[key] = value

    merged["model_trace"] = _merge_build_model_trace(
        live.get("model_trace"), persisted.get("model_trace")
    )
    for key in (
        "quality_scorecard",
        "best_of_n",
        "cost_truth",
        "skills_used",
        "recall_used",
        "classification",
        "stack_selection",
    ):
        selected = _richer_build_evidence(live.get(key), persisted.get(key))
        if _build_evidence_weight(selected):
            merged[key] = selected

    live_cost = _nonnegative_build_cost(live.get("cost_usd"))
    persisted_cost = _nonnegative_build_cost(persisted.get("cost_usd"))
    persisted_cost_truth = _row_cost_truth(persisted)
    live_cost_truth = _matching_row_cost_truth(live, live_cost) if live_cost else {}
    persisted_truth_cost = _cost_truth_amount(persisted_cost_truth)
    live_terminal = _build_is_terminal(str(live.get("status", "")))
    if (
        live_terminal
        and persisted_cost is not None
        and _build_evidence_weight(persisted_cost_truth)
    ):
        merged["cost_usd"] = (
            persisted_truth_cost
            if persisted_truth_cost is not None
            else persisted_cost
        )
        merged["cost_truth"] = persisted_cost_truth
    elif not live_terminal and live_cost is not None and live_cost > 0:
        merged["cost_usd"] = live_cost
        if _build_evidence_weight(live_cost_truth):
            merged["cost_truth"] = live_cost_truth
            _set_scorecard_cost_fields(
                merged,
                cost=live_cost,
                cost_truth=live_cost_truth,
            )
        elif not (
            _same_build_cost(persisted_cost, live_cost)
            and _same_build_cost(persisted_truth_cost, live_cost)
        ):
            merged.pop("cost_truth", None)
            _set_scorecard_cost_fields(merged)
    elif persisted_cost is not None and _build_evidence_weight(persisted_cost_truth):
        merged["cost_usd"] = (
            persisted_truth_cost
            if persisted_truth_cost is not None
            else persisted_cost
        )
        merged["cost_truth"] = persisted_cost_truth
    elif live_cost is not None and live_cost > 0:
        merged["cost_usd"] = live_cost
    elif persisted_cost is not None:
        merged["cost_usd"] = persisted_cost
    elif live_cost is not None:
        merged["cost_usd"] = live_cost

    # Persisted evidence may lag the current process; local lifecycle identity wins.
    for key in ("build_id", "status", "created_at", "updated_at", "correlation_id"):
        if key in live:
            merged[key] = live[key]
    return merged


def _normalize_build_key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _row_target_values(row: dict[str, Any]) -> tuple[str, str, str]:
    raw_manifest = row.get("manifest")
    manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
    return (
        str(row.get("brief") or manifest.get("brief") or ""),
        str(row.get("stack") or manifest.get("stack") or ""),
        str(row.get("slug") or manifest.get("slug") or ""),
    )


def _row_matches_build_target(row: dict[str, Any], *, brief: str, stack: str, slug: str) -> bool:
    row_brief, row_stack, row_slug = _row_target_values(row)
    target_slug = _normalize_build_key(slug)
    if target_slug:
        return _normalize_build_key(row_slug) == target_slug
    return (
        _normalize_build_key(row_brief) == _normalize_build_key(brief)
        and _normalize_build_key(row_stack) == _normalize_build_key(stack)
    )


async def _matching_failure_count(state: AppState, *, brief: str, stack: str, slug: str) -> int:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rec in state.builds.values():
        row = rec.to_dict()
        bid = str(row.get("build_id") or "")
        rows.append(row)
        if bid:
            seen.add(bid)
    memory = getattr(state, "memory", None)
    if memory is not None and hasattr(memory, "recent_builds"):
        try:
            for row in await memory.recent_builds(limit=200):
                bid = str(row.get("build_id") or "")
                if bid and bid in seen:
                    continue
                rows.append(row)
                if bid:
                    seen.add(bid)
        except Exception:  # noqa: BLE001 - failure diagnostics never block submit
            pass
    return sum(
        1
        for row in rows
        if _normalize_status(str(row.get("status") or "")) in _BUILD_FAILURE_STATUSES
        and _row_matches_build_target(row, brief=brief, stack=stack, slug=slug)
    )


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
    return router_is_free_model_id(model)


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


def _profile_extra(profile: str, *, asset_gen_enabled: bool) -> dict[str, Any]:
    if profile == "fast":
        return {
            "best_of_n": 1,
            "best_of_n_across_models": False,
            "max_debug_attempts": 1,
            "parallel_code_slices": True,
        }
    if profile == "cheap_learned":
        return {
            "best_of_n": 1,
            "best_of_n_across_models": False,
            "parallel_code_slices": True,
        }
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
            "asset_gen": asset_gen_enabled,
            "visual_self_heal": True,
        }
    if profile == "full_app":
        return _full_app_extra(asset_gen_enabled=asset_gen_enabled)
    return {}


def _full_app_extra(*, asset_gen_enabled: bool) -> dict[str, Any]:
    return {
        "full_app_contract": True,
        "asset_gen": asset_gen_enabled,
        "best_of_n": 2,
        "best_of_n_across_models": True,
        "max_debug_attempts": 4,
        "agentic_timeout": 1200,
        "visual_self_heal": True,
    }


def _orchestration_extra(
    profile: str,
    *,
    full_app: bool,
    asset_gen_enabled: bool,
) -> dict[str, Any]:
    """Compose profile policy without weakening the requested app contract."""
    extra = (
        _full_app_extra(asset_gen_enabled=asset_gen_enabled)
        if full_app
        else {}
    )
    extra.update(_profile_extra(profile, asset_gen_enabled=asset_gen_enabled))
    if full_app:
        # Profiles may trade retries/candidates for latency, but a full app still
        # includes its visual pass. Paid asset generation remains an explicit
        # operator setting; full scope must never override asset_gen=false.
        extra["full_app_contract"] = True
        extra["asset_gen"] = asset_gen_enabled
        extra["visual_self_heal"] = True
        if profile in {"fast", "cheap_learned"}:
            extra["parallel_code_slices_min_files"] = 4
    return extra


def _enforce_build_routing(state: AppState) -> None:
    """Reject unusable routes before an API request creates build state.

    Dashboard ``auto`` builds run on a local CLI from the operator's
    ``auto_cli_priority`` chain; hosted fallback needs the explicit
    ``auto_allow_openrouter`` opt-in. A configured OpenRouter key is never
    implicit consent to use it, so surface a missing executor before queueing
    a build or allocating its ledger.
    """
    from skyn3t.adapters.llm import enforce_explicit_routing_lock

    client = getattr(state, "llm_client", None)
    probe = getattr(client, "_cli_available", None)
    enforce_explicit_routing_lock(
        state.settings,
        cli_available=probe if callable(probe) else None,
        require_codex_for_auto=hasattr(state.settings, "llm_backend"),
    )


def _submission_routing_snapshot(
    state: AppState,
    model_override: str,
) -> dict[str, Any]:
    """Return immutable requested/effective routing truth for one build."""
    client = getattr(state, "llm_client", None)
    snapshot = getattr(client, "build_routing_snapshot", None)
    if callable(snapshot):
        return dict(snapshot(model_override))

    requested = str(
        getattr(state.settings, "llm_backend", "auto") or "auto"
    ).strip().lower()
    effective = str(getattr(client, "backend", "") or requested).strip().lower()
    requested_override = str(model_override or "").strip()
    preferred_model = str(
        getattr(state.settings, "preferred_model", "") or ""
    ).strip()
    requested_model = preferred_model if effective == "openrouter" else ""
    if effective == "openrouter":
        effective_model = preferred_model or "router:auto"
    elif effective.endswith("_cli"):
        effective_model = f"{effective[:-4]}-cli:default"
    else:
        effective_model = "offline-stub"
    codegen = {
        "source": "global_backend",
        "requested_backend": requested,
        "effective_backend": effective,
        "requested_model": requested_override or preferred_model,
        "effective_model": requested_override or effective_model,
    }
    return {
        "requested_backend": requested,
        "effective_backend": effective,
        "requested_model": requested_model,
        "effective_model": effective_model,
        "submission": {
            "requested_backend": requested,
            "effective_backend": effective,
            "requested_model": requested_model,
            "model_override": requested_override,
            "codegen": dict(codegen),
        },
        "codegen": codegen,
    }


def _restore_submission_routing_trace(
    state: AppState,
    build_id: str,
    routing: dict[str, Any],
    _task: Any,
) -> None:
    """Restore request provenance without replacing terminal execution evidence."""
    rec = state.builds.get(build_id)
    if rec is None:
        return
    trace = dict(rec.model_trace) if isinstance(rec.model_trace, dict) else {}
    existing_codegen = (
        dict(trace["codegen"]) if isinstance(trace.get("codegen"), dict) else {}
    )
    has_terminal_codegen_evidence = bool(
        trace.get("effective_codegen_backend")
        or trace.get("effective_codegen_model")
        or existing_codegen.get("effective_backend")
        or existing_codegen.get("effective_model")
    )
    routing_codegen = (
        dict(routing["codegen"]) if isinstance(routing.get("codegen"), dict) else {}
    )
    submission = (
        dict(routing["submission"])
        if isinstance(routing.get("submission"), dict)
        else {}
    )
    submission.setdefault("requested_backend", routing.get("requested_backend", ""))
    submission.setdefault("effective_backend", routing.get("effective_backend", ""))
    submission.setdefault("requested_model", routing.get("requested_model", ""))
    submission_codegen = submission.get("codegen")
    submission["codegen"] = (
        dict(submission_codegen)
        if isinstance(submission_codegen, dict)
        else routing_codegen
    )
    trace["submission"] = submission

    for key in ("requested_backend", "requested_model"):
        if key in routing:
            trace[key] = routing[key]
    for key in ("effective_backend", "effective_model"):
        if not trace.get(key) and routing.get(key):
            trace[key] = routing[key]

    terminal_codegen = existing_codegen
    for key in ("source", "requested_backend", "requested_model"):
        if key in routing_codegen:
            terminal_codegen[key] = routing_codegen[key]
    for key in ("effective_backend", "effective_model"):
        if not terminal_codegen.get(key) and routing_codegen.get(key):
            terminal_codegen[key] = routing_codegen[key]
    trace["codegen"] = terminal_codegen

    if routing_codegen.get("requested_backend") is not None:
        trace["requested_codegen_backend"] = routing_codegen.get(
            "requested_backend", ""
        )
    if routing_codegen.get("requested_model") is not None:
        trace["requested_codegen_model"] = routing_codegen.get("requested_model", "")
    if not trace.get("effective_codegen_backend"):
        trace["effective_codegen_backend"] = terminal_codegen.get(
            "effective_backend", ""
        )
    if not trace.get("effective_codegen_model"):
        trace["effective_codegen_model"] = terminal_codegen.get("effective_model", "")
    if not has_terminal_codegen_evidence and routing.get("effective_backend"):
        trace["backend"] = routing["effective_backend"]
    elif not trace.get("backend") and trace.get("effective_codegen_backend"):
        trace["backend"] = trace["effective_codegen_backend"]
    elif not trace.get("backend") and routing.get("effective_backend"):
        trace["backend"] = routing["effective_backend"]
    rec.model_trace = trace


def _normalize_moa_advisors(value: Any, *, no_claude: bool = False) -> str | None:
    """Coerce a dashboard advisor selection into a slot string.

    Accepts a list (checkbox selection) or a comma string. ``None``/absent means
    "use the configured default"; an explicit empty list or string means "no
    advisors for THIS build" — the two are deliberately different, so unchecking
    every box is honoured rather than silently falling back to the default.
    Unknown providers are dropped here so a hand-crafted request cannot smuggle
    an arbitrary token into an advisor slot.
    """
    if value is None:
        return None
    from skyn3t.adapters.model_slot import parse_slots

    if isinstance(value, (list, tuple, set)):
        raw = ",".join(str(v) for v in value)
    else:
        raw = str(value)
    # Require a RECOGNISED provider. parse_slots treats an unknown token as a
    # bare model id on the active backend, which is right for an operator's
    # settings string but wrong for request input: it would let a caller aim an
    # advisor at an arbitrary model instead of picking from the offered list.
    return ",".join(
        slot.address
        for slot in parse_slots(raw)
        if slot.provider
        and not (
            no_claude
            and (slot.provider == "claude" or "claude" in slot.address.lower())
        )
    )


async def cli_providers_payload(state: AppState) -> dict[str, Any]:
    """Selectable advisor providers with live availability, for the build form.

    Registry-driven so a newly supported backend appears in the GUI with no
    route change (the same rule ``gates_payload`` follows). ``available``
    reflects PATH detection only — it cannot prove a CLI is signed in, so a
    listed provider may still fail at call time and be recorded as a failed
    advisor rather than breaking the build.
    """
    from skyn3t.adapters.llm import KNOWN_CLI_PROVIDERS, LLMClient, openrouter_key
    from skyn3t.adapters.model_slot import parse_slots

    settings = state.settings
    labels = {
        "codex": "Codex CLI",
        "claude": "Claude Code CLI",
        "kimi": "Kimi Code CLI",
        "copilot": "GitHub Copilot CLI",
    }
    selected = {s.address for s in parse_slots(getattr(settings, "moa_advisors", "") or "")}
    providers: list[dict[str, Any]] = []
    for name in KNOWN_CLI_PROVIDERS:
        if bool(getattr(settings, "no_claude", False)) and name == "claude":
            continue
        slot = f"{name}_cli"
        try:
            available = bool(LLMClient._cli_available(name))
        except Exception:  # noqa: BLE001 - a probe failure must not 500 the route
            available = False
        providers.append({
            "slot": slot,
            "provider": name,
            "label": labels.get(name, name),
            "available": available,
            "selected": slot in selected,
        })
    providers.append({
        "slot": "openrouter",
        "provider": "openrouter",
        "label": "OpenRouter (hosted)",
        "available": bool(openrouter_key(settings)),
        "selected": any(s.startswith("openrouter") for s in selected),
    })
    return {
        "providers": providers,
        "moa_enabled": bool(getattr(settings, "moa_enabled", False)),
        "default_advisors": getattr(settings, "moa_advisors", "") or "",
    }


async def submit_build(state: AppState, brief: str, stack: str = "", slug: str = "",
                       reference_image: str = "", reference_images: list[str] | None = None,
                       build_profile: str = "cheap_learned",
                       model_override: str = "", full_app: bool = False,
                       source_product_spec: dict[str, Any] | None = None,
                       moa_advisors: str | None = None) -> dict[str, Any]:
    """Queue a build. Uses the studio if wired, else records + emits an event.

    ``reference_image`` is an optional base64 ``data:`` URL; ``reference_images``
    accepts a small ordered list. Valid images are saved and threaded into the
    build so the design agent can match them. Absent -> unchanged behavior.
    """
    if not brief or not brief.strip():
        raise ValueError("brief is required")
    _enforce_build_routing(state)
    requested_slug = slug.strip()
    if requested_slug:
        projects_root = Path(state.settings.projects_dir).resolve()
        candidate = (projects_root / requested_slug).resolve()
        if candidate == projects_root or not candidate.is_relative_to(projects_root):
            raise ValueError("invalid slug")
        if str(candidate) in _REVERIFYING_PROJECTS:
            raise ValueError("project local re-verification is still running")
    profile = _normalize_build_profile(build_profile)
    requested_model = _normalize_model_override(model_override)
    routing = _submission_routing_snapshot(state, requested_model)
    build_id = state.new_build_id()
    failure_count = await _matching_failure_count(
        state,
        brief=brief,
        stack=stack,
        slug=slug,
    )
    model = requested_model
    full_app_requested = bool(full_app) or profile == "full_app"
    routing_trace: dict[str, Any] = {
        key: deepcopy(routing[key])
        for key in (
            "requested_backend",
            "effective_backend",
            "requested_model",
            "effective_model",
            "submission",
            "codegen",
        )
        if key in routing
    }
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
            "requested_model_override": requested_model,
            "failure_count": failure_count,
            "backend": routing.get("effective_backend", ""),
            **routing_trace,
            "full_app": full_app_requested,
        },
        correlation_id=build_id,
    )
    state.builds[build_id] = rec

    # Prefer a wired StudioRunner (async start(brief, slug=None, extra=None)),
    # falling back to a legacy submit(...) if present. The build runs as a
    # background task so the endpoint returns immediately with the build_id.
    # Optional reference image: decode + save (degrades to data-URL pass-through).
    ref_path, ref_paths = _save_reference_images(
        state,
        build_id,
        reference_image,
        reference_images,
    )

    studio = state.studio
    dispatched = False
    runner = None
    build_extra: dict[str, Any] = {
        "stack": stack,
        "build_id": build_id,
        "build_profile": profile,
        "routing_snapshot": routing_trace,
    }
    if source_product_spec is not None:
        build_extra["source_product_spec"] = deepcopy(source_product_spec)
    if moa_advisors is not None:
        # Per-build advisory-council selection from the dashboard picker. An
        # explicit empty string means "no advisors for this build", which is
        # distinct from the key being absent (use the configured default).
        build_extra["moa_advisors"] = str(moa_advisors)
    build_extra.update(
        _orchestration_extra(
            profile,
            full_app=full_app_requested,
            asset_gen_enabled=bool(getattr(state.settings, "asset_gen", False)),
        )
    )
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
            client = getattr(state, "llm_client", None)
            routing_scope = getattr(client, "build_routing_scope", None)
            if callable(routing_scope):
                with routing_scope(routing):
                    res = runner()
                    # A Task captures the current context at creation. Create it
                    # inside the scope so every child agent keeps this build's
                    # backend even after the dashboard setting changes.
                    if hasattr(res, "__await__"):
                        res = asyncio.ensure_future(res)
            else:
                res = runner()
            if hasattr(res, "__await__"):
                # Keep a strong reference so the build task isn't garbage-
                # collected mid-run, and retrieve any exception on completion.
                task = asyncio.ensure_future(res)
                task._skyn3t_build_id = build_id  # type: ignore[attr-defined]
                _BUILD_TASKS.add(task)
                _BUILD_TASKS_BY_ID[build_id] = task
                task.add_done_callback(_reap_build_task)
                task.add_done_callback(
                    partial(
                        _restore_submission_routing_trace,
                        state,
                        build_id,
                        routing,
                    )
                )
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
    task_settled = task is None or task.done()
    if task is not None and not task.done():
        import asyncio

        task.cancel()
        task_cancelled = True
        try:
            await asyncio.wait_for(task, timeout=15.0)
            task_settled = True
        except asyncio.CancelledError:
            task_settled = True
        except TimeoutError:
            log.warning("build.cancel_settle_timeout", build_id=bid)
        except Exception as exc:  # noqa: BLE001 - persistence must still win
            task_settled = True
            log.warning("build.cancel_settle_error", build_id=bid, error=str(exc)[:160])

    # The runner's cancellation handler persists recovery paths while the task
    # settles. Refresh the row before our final status write so the stale copy
    # read above cannot erase that newly recorded recovery metadata.
    if state.memory is not None and hasattr(state.memory, "get_build"):
        try:
            latest = await state.memory.get_build(bid)
            if isinstance(latest, dict):
                db_row = latest
        except Exception:  # noqa: BLE001 - retain the original best-effort row
            pass

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
    return {
        "build_id": bid,
        "status": "cancelled",
        "task_cancelled": task_cancelled,
        "task_settled": task_settled,
    }


def _build_replay_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Extract the small set of inputs needed to rerun a prior build."""
    raw_manifest = row.get("manifest")
    manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
    raw_extra = manifest.get("extra")
    extra = raw_extra if isinstance(raw_extra, dict) else {}
    raw_trace = row.get("model_trace")
    trace = raw_trace if isinstance(raw_trace, dict) else {}
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


def _rebuild_source_product_spec(
    state: AppState,
    replay: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any] | None:
    """Load the current durable source contract, with manifest history as fallback."""

    from skyn3t.studio.product_spec import ProductSpecV1

    source: ProductSpecV1 | None = None
    source_slug = str(replay.get("slug") or "").strip()
    if source_slug:
        projects_root = Path(state.settings.projects_dir).resolve()
        project = (projects_root / source_slug).resolve()
        if project.is_relative_to(projects_root) and project.is_dir():
            source = ProductSpecV1.load(project)

    if source is None:
        raw_manifest = row.get("manifest")
        manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
        raw_extra = manifest.get("extra")
        extra = raw_extra if isinstance(raw_extra, dict) else {}
        snapshot = extra.get("product_spec")
        if isinstance(snapshot, dict):
            source = ProductSpecV1.from_dict(snapshot)
    return source.to_dict() if source is not None else None


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
    source_product_spec = _rebuild_source_product_spec(state, replay, row)
    res = await submit_build(
        state,
        brief=replay["brief"],
        stack=replay["stack"],
        slug=replay["slug"] if reuse_slug else "",
        build_profile=replay["build_profile"],
        model_override=replay["model_override"],
        full_app=replay["full_app"],
        source_product_spec=source_product_spec,
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
    positions = {str(build.get("build_id", "")): index for index, build in enumerate(builds)}
    if state.memory is not None and hasattr(state.memory, "recent_builds"):
        try:  # pragma: no cover - depends on memory backend
            for row in await state.memory.recent_builds(limit=limit):
                bid = str(row.get("build_id", ""))
                if not bid:
                    continue
                position = positions.get(bid)
                if position is not None:
                    builds[position] = _merge_live_build_history(builds[position], row)
                    continue
                positions[bid] = len(builds)
                builds.append(row)
        except Exception:  # noqa: BLE001
            pass
    visible = [dict(build) for build in builds[:limit]]
    gate = getattr(getattr(state, "studio", None), "approval_gate", None)
    for build in visible:
        pending: list[dict[str, Any]] = []
        if gate is not None:
            try:
                pending = list(gate.pending(str(build.get("build_id", ""))))
            except Exception:  # noqa: BLE001 - introspection is advisory
                pending = []
        build["approval_pending"] = bool(pending)
        build["approval_stages"] = [
            str(item.get("stage")) for item in pending if item.get("stage")
        ]
    return {"builds": visible}


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


def _project_visible_stats(project: Path) -> tuple[int, int]:
    """Return source/delivery bytes and file count, excluding internal previews."""
    snapshot = source_tree_snapshot(project)
    return int(snapshot["byte_count"]), int(snapshot["file_count"])


def _canonical_evidence_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        payload = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _completed_brief_review(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the latest completed canonical reviewer-stage evidence.

    The manifest's final verdict is not enough: cancellation can happen after a
    structural rescore or before finalization. The canonical ``reviewer`` stage
    is the existing brief-aware signal used by the main build pipeline.
    """
    stages = manifest.get("stages")
    if not isinstance(stages, list):
        stages = []
    for value in reversed(stages):
        if not isinstance(value, dict):
            continue
        if str(value.get("agent_type") or "").strip().lower() != "reviewer":
            continue
        if str(value.get("status") or "").strip().lower() != "completed":
            continue
        summary = value.get("output_summary")
        summary = summary if isinstance(summary, dict) else {}
        verdict = str(summary.get("verdict") or "").strip().lower()
        score_value = summary.get("score", value.get("score"))
        try:
            score = float(score_value) if score_value is not None else None
        except (TypeError, ValueError):
            score = None
        if score is not None and not math.isfinite(score):
            score = None
        snapshot_valid_value = summary.get("source_tree_snapshot_valid")
        snapshot_valid = (
            snapshot_valid_value if isinstance(snapshot_valid_value, bool) else None
        )
        return {
            "found": True,
            "stage": str(value.get("name") or "review"),
            "verdict": verdict,
            "score": score,
            "evidence_sha256": _canonical_evidence_sha256(value),
            "source_tree_sha256": str(
                summary.get("source_tree_sha256") or ""
            ).strip(),
            "source_tree_digest_algorithm": str(
                summary.get("source_tree_digest_algorithm") or ""
            ).strip(),
            "source_tree_snapshot_valid": snapshot_valid,
        }
    return {
        "found": False,
        "stage": "",
        "verdict": "",
        "score": None,
        "evidence_sha256": "",
        "source_tree_sha256": "",
        "source_tree_digest_algorithm": "",
        "source_tree_snapshot_valid": None,
    }


def _terminal_non_delivered_manifest(manifest: dict[str, Any]) -> bool:
    status = _normalize_status(str(manifest.get("status") or ""))
    verdict = str(manifest.get("verdict") or "").strip().lower()
    return status in _NON_SHIPPABLE_PROJECT_STATUSES or (
        status == "completed" and verdict == "no_go"
    )


def _reverify_file_count(project: Path, visible_file_count: int | None = None) -> int:
    """Count project-owned files, excluding the manifest itself.

    ``_project_visible_stats`` already excludes dependency/build caches and
    recovery/preview internals. Reuse its count when a list row already paid for
    that scan; otherwise perform the same bounded scan here.
    """
    return (
        _project_visible_stats(project)[1]
        if visible_file_count is None
        else max(0, int(visible_file_count))
    )


def _project_reverify_eligibility(
    project: Path,
    manifest: dict[str, Any] | None,
    *,
    status: str,
    active: bool,
    visible_file_count: int,
) -> tuple[bool, str]:
    if manifest is None:
        return False, "No build manifest is available."
    if active:
        return False, "The build is still active."
    if str(project.resolve()) in _REVERIFYING_PROJECTS:
        return False, "Local re-verification is already running."
    if not _terminal_non_delivered_manifest(manifest):
        if _normalize_status(status) == "completed" and (
            str(manifest.get("verdict") or "").strip().lower() == "go"
        ):
            return False, "The project is already delivered."
        return False, "The build is not a terminal non-delivered project."
    if _reverify_file_count(project, visible_file_count) <= 0:
        return False, "No project files are available to verify."
    review = _completed_brief_review(manifest)
    if review["verdict"] != "go":
        return False, "A completed brief-aware review with verdict go is required."
    return True, ""


def _reverified_project_files(project: Path) -> list[str]:
    snapshot = source_tree_snapshot(project)
    return list(snapshot["files"])


def _active_reverify_status(value: Any) -> bool:
    status = _normalize_status(str(value or ""))
    return bool(status) and not _build_is_terminal(status)


async def _project_has_active_build(
    state: AppState,
    *,
    slug: str,
    build_id: str,
) -> bool:
    """Check live and durable projections before mutating a project tree."""
    for record in getattr(state, "builds", {}).values():
        record_slug = str(getattr(record, "slug", "") or "")
        record_id = str(getattr(record, "build_id", "") or "")
        if (record_slug == slug or (build_id and record_id == build_id)) and (
            _active_reverify_status(getattr(record, "status", ""))
        ):
            return True

    memory = getattr(state, "memory", None)
    if memory is None:
        return False
    rows: list[dict[str, Any]] = []
    if build_id and hasattr(memory, "get_build"):
        try:
            row = await memory.get_build(build_id)
            if isinstance(row, dict):
                rows.append(row)
        except Exception:  # noqa: BLE001 - the disk manifest remains authoritative
            pass
    try:
        if hasattr(memory, "latest_builds_by_slug"):
            values = await memory.latest_builds_by_slug([slug])
        elif hasattr(memory, "recent_builds"):
            values = await memory.recent_builds(limit=200)
        else:
            values = []
        rows.extend(value for value in values if isinstance(value, dict))
    except Exception:  # noqa: BLE001 - a store read failure must not crash the API
        pass
    return any(
        (
            str(row.get("slug") or "") == slug
            or (build_id and str(row.get("build_id") or "") == build_id)
        )
        and _active_reverify_status(row.get("status"))
        for row in rows
    )


def _resolved_artifact_matches(project: Path, value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        return Path(raw).resolve() == project.resolve()
    except OSError:
        return False


async def _validate_reverify_identity(
    state: AppState,
    project: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Validate the direct project, manifest, live cache, and durable row agree."""
    from skyn3t.studio.cleanup import _load_manifest

    canonical_slug = project.name
    manifest_slug = str(manifest.get("slug") or "").strip()
    build_id = str(manifest.get("build_id") or "").strip()
    if not manifest_slug or manifest_slug != canonical_slug:
        raise ProjectReverifyError("manifest slug does not match the project directory")
    if not build_id:
        raise ProjectReverifyError("manifest build_id is required for local re-verification")
    artifact_dir = manifest.get("artifact_dir")
    if not artifact_dir:
        raise ProjectReverifyError("manifest artifact_dir is required for local re-verification")
    if not _resolved_artifact_matches(project, artifact_dir):
        raise ProjectReverifyError("manifest artifact_dir does not match the project directory")

    projects_root = Path(state.settings.projects_dir).resolve()
    try:
        siblings = [path for path in projects_root.iterdir() if path.is_dir() and path != project]
    except OSError as exc:
        raise ProjectReverifyError(
            "could not validate project identity",
            status_code=500,
        ) from exc
    for sibling in siblings:
        other = _load_manifest(sibling)
        if isinstance(other, dict) and str(other.get("build_id") or "").strip() == build_id:
            raise ProjectReverifyError("build_id is already owned by another project")

    for record in getattr(state, "builds", {}).values():
        record_id = str(getattr(record, "build_id", "") or "").strip()
        record_slug = str(getattr(record, "slug", "") or "").strip()
        if record_id == build_id and record_slug and record_slug != canonical_slug:
            raise ProjectReverifyError("live build_id is owned by another project")

    durable_row: dict[str, Any] | None = None
    latest_row: dict[str, Any] | None = None
    memory = getattr(state, "memory", None)
    if memory is not None:
        try:
            if hasattr(memory, "get_build"):
                value = await memory.get_build(build_id)
                durable_row = value if isinstance(value, dict) else None
            if hasattr(memory, "latest_builds_by_slug"):
                values = await memory.latest_builds_by_slug([canonical_slug])
            elif hasattr(memory, "recent_builds"):
                values = await memory.recent_builds(limit=200)
            else:
                values = []
        except Exception as exc:  # noqa: BLE001 - identity must fail closed
            raise ProjectReverifyError(
                "durable build identity could not be validated",
                status_code=503,
            ) from exc
        latest_row = next(
            (
                value
                for value in values
                if isinstance(value, dict)
                and str(value.get("slug") or "").strip() == canonical_slug
            ),
            None,
        )

    if durable_row is not None:
        durable_slug = str(durable_row.get("slug") or "").strip()
        if durable_slug and durable_slug != canonical_slug:
            raise ProjectReverifyError("durable build_id is owned by another project")
        durable_artifact = durable_row.get("artifact_dir")
        if durable_artifact and not _resolved_artifact_matches(project, durable_artifact):
            raise ProjectReverifyError("durable artifact_dir does not match the project")
    if latest_row is not None and str(latest_row.get("build_id") or "").strip() != build_id:
        raise ProjectReverifyError("a newer durable build owns this project slug")

    return {
        "slug": canonical_slug,
        "build_id": build_id,
        "project_relpath": canonical_slug,
        "validated": True,
        "durable_row": durable_row,
        "latest_row": latest_row,
    }


def _run_local_project_reverify(
    project: Path,
    *,
    stack: str,
    brief: str,
    settings: Any,
    cancel_requested: threading.Event | None = None,
) -> dict[str, Any]:
    """Repair and prove an isolated candidate without touching the live tree."""
    from skyn3t.studio.planner import file_checklist
    from skyn3t.studio.proof_run import (
        apply_deterministic_repairs,
        proof_run,
        stabilize_node_dependencies,
    )

    staging_root, staged_project, live_source = _copy_reverify_candidate(project)
    try:
        if cancel_requested is not None and cancel_requested.is_set():
            raise asyncio.CancelledError
        repairs = apply_deterministic_repairs(staged_project, stack=stack)
        if cancel_requested is not None and cancel_requested.is_set():
            raise asyncio.CancelledError
        before_dependencies = source_tree_snapshot(staged_project)
        if not bool(before_dependencies.get("valid")):
            raise ProjectReverifyError(
                "local re-verification candidate is ambiguous or unreadable"
            )

        run_tests = bool(getattr(settings, "run_generated_tests", True))
        run_build = bool(getattr(settings, "run_generated_build", True))
        build_timeout = int(getattr(settings, "generated_build_timeout", 300))
        execution_backend = str(
            getattr(settings, "execution_backend", "auto") or "auto"
        )
        dependency_ran, dependency_ok, dependency_summary = (
            stabilize_node_dependencies(
                staged_project,
                execution_backend=execution_backend,
                stack=stack,
                timeout=build_timeout,
                run_tests=run_tests,
                run_build=run_build,
            )
        )
        if cancel_requested is not None and cancel_requested.is_set():
            raise asyncio.CancelledError
        candidate = source_tree_snapshot(staged_project)
        if not bool(candidate.get("valid")):
            raise ProjectReverifyError(
                "local re-verification candidate is ambiguous or unreadable"
            )
        proof = proof_run(
            staged_project,
            checklist=file_checklist(stack),
            execution_backend=execution_backend,
            stack=stack,
            run_tests=run_tests,
            test_timeout=int(getattr(settings, "generated_test_timeout", 90)),
            run_build=run_build,
            build_timeout=build_timeout,
            enable_mock_llm=bool(getattr(settings, "mock_llm_proof_enabled", True)),
            install_python_deps=bool(
                getattr(settings, "proof_install_python_deps", True)
            ),
            python_deps_timeout=int(
                getattr(settings, "proof_python_deps_timeout", 120)
            ),
            brief=brief,
        )
        if cancel_requested is not None and cancel_requested.is_set():
            raise asyncio.CancelledError
        gates = _run_reverify_candidate_gates(
            staged_project,
            stack=stack,
            settings=settings,
        )
        after_proof = source_tree_snapshot(staged_project)
        return {
            "staging_root": staging_root,
            "project": staged_project,
            "live_source": live_source,
            "repairs": repairs,
            "dependency_stabilization": {
                "ran": dependency_ran,
                "passed": dependency_ok if dependency_ran else None,
                "summary": dependency_summary,
            },
            "candidate": candidate,
            "proof": proof,
            "gates": gates,
            "after_proof": after_proof,
        }
    except BaseException:
        _cleanup_reverify_staging(staging_root)
        raise


def _run_reverify_candidate_gates(
    project: Path,
    *,
    stack: str,
    settings: Any,
) -> dict[str, Any]:
    """Run promotion-critical checks against the isolated candidate only."""
    from skyn3t.studio.security_check import check_security
    from skyn3t.studio.web_polish_check import check_web_polish

    security = check_security(project, stack)
    web_polish = check_web_polish(project, stack)
    return {
        "security": security,
        "web_polish": web_polish,
        "runtime_liveness": _run_reverify_runtime_liveness(
            project,
            stack=stack,
            settings=settings,
        ),
    }


def _run_reverify_runtime_liveness(
    project: Path,
    *,
    stack: str,
    settings: Any,
) -> dict[str, Any]:
    """Boot and probe a staged web candidate without repair or host secrets."""
    from skyn3t.core.stacks import UI_WEB_STACKS, gate_applies

    normalized_stack = (stack or "").strip().lower()
    if not gate_applies("liveness", normalized_stack):
        return {
            "ok": True,
            "skipped": True,
            "reason": "runtime liveness does not apply to this stack",
            "routes": [],
            "dead_routes": [],
        }

    async def _probe() -> dict[str, Any]:
        from skyn3t.studio.liveness import (
            check_liveness,
            crawl_routes,
            enumerate_routes,
            merge_routes,
        )
        from skyn3t.studio.preview_supervisor import PreviewSupervisor

        runner = PreviewSupervisor()
        app = None
        try:
            timeout = max(
                1,
                min(20, int(getattr(settings, "generated_build_timeout", 20) or 20)),
            )
            app = await runner.start(
                project,
                normalized_stack,
                ready_timeout=timeout,
            )
            if app.status != "running" or not app.url:
                reason = str(app.detail.get("reason") or "")
                if not reason:
                    reason = str(app.detail.get("error") or "preview did not start")
                return {
                    "ok": False,
                    "skipped": False,
                    "reason": reason[:500],
                    "serve_status": app.status,
                    "serve_kind": app.kind,
                    "routes": [],
                    "dead_routes": [],
                }
            routes = merge_routes(
                enumerate_routes(project, normalized_stack),
                await crawl_routes(app.url),
            )
            report = await check_liveness(app.url, routes)
            blocking = [
                result
                for result in report.results
                if not result.ok
                and not (
                    normalized_stack not in UI_WEB_STACKS
                    and result.path == "/"
                    and result.status == 404
                )
            ]
            dead_routes = [result.path for result in blocking]
            return {
                "ok": not blocking,
                "skipped": False,
                "reason": (
                    "all runtime routes responded"
                    if not blocking
                    else f"{len(blocking)} runtime route(s) did not respond"
                ),
                "serve_status": app.status,
                "serve_kind": app.kind,
                "routes": [
                    {
                        "path": result.path,
                        "method": result.method,
                        "status": result.status,
                        "ok": result.ok,
                    }
                    for result in report.results
                ],
                "dead_routes": dead_routes,
            }
        except Exception as exc:  # noqa: BLE001 - fail closed for promotion
            return {
                "ok": False,
                "skipped": False,
                "reason": f"runtime liveness failed: {str(exc)[:400]}",
                "routes": [],
                "dead_routes": [],
            }
        finally:
            if app is not None:
                try:
                    await runner.stop(app)
                except Exception:  # noqa: BLE001 - cleanup remains best-effort
                    pass

    return asyncio.run(_probe())


def _cleanup_reverify_staging(staging_root: Path | None) -> None:
    if staging_root is None:
        return
    try:
        shutil.rmtree(staging_root)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("project.reverify_staging_cleanup_failed", path=str(staging_root))


def _copy_reverify_candidate(project: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Copy the canonical source view and verify the copy before any command."""
    live_source = source_tree_snapshot(project)
    if not bool(live_source.get("valid")):
        raise ProjectReverifyError(
            "local re-verification source tree is ambiguous or unreadable"
        )
    staging_root = project.parent / (
        f".skyn3t-reverify-{project.name}-{uuid.uuid4().hex}"
    )
    staged_project = staging_root / "candidate"
    try:
        staging_root.mkdir()
        if (
            staging_root.is_symlink()
            or staging_root.resolve().parent != project.parent.resolve()
        ):
            raise ProjectReverifyError("local re-verification staging path is unsafe")
        staged_project.mkdir()
        project_root = project.resolve()
        for relative_value in live_source.get("files") or []:
            relative = Path(str(relative_value))
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ProjectReverifyError(
                    "local re-verification source snapshot contains an unsafe path"
                )
            source_path = project / relative
            if source_path.is_symlink():
                raise ProjectReverifyError(
                    "local re-verification source changed while it was copied"
                )
            source = source_path.resolve()
            if not source.is_relative_to(project_root):
                raise ProjectReverifyError(
                    "local re-verification source changed while it was copied"
                )
            target = staged_project / relative
            if not target.resolve().is_relative_to(staged_project.resolve()):
                raise ProjectReverifyError(
                    "local re-verification source snapshot contains an unsafe path"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        copied = source_tree_snapshot(staged_project)
        if (
            not bool(copied.get("valid"))
            or copied.get("algorithm") != live_source.get("algorithm")
            or copied.get("sha256") != live_source.get("sha256")
        ):
            raise ProjectReverifyError(
                "local re-verification source changed while it was copied"
            )
    except BaseException:
        _cleanup_reverify_staging(staging_root)
        raise
    return staging_root, staged_project, live_source


def _reverify_replace_failure_message(exc: OSError) -> str:
    """Explain the common Windows preview-server lock without hiding failures."""
    lock_codes = {5, 13, 32}
    if (
        isinstance(exc, PermissionError)
        or getattr(exc, "errno", None) in lock_codes
        or getattr(exc, "winerror", None) in lock_codes
    ):
        return (
            "verified candidate could not replace the live project because its "
            "preview or server process may still be running; stop that project "
            "preview/server and retry"
        )
    return "verified candidate could not replace the live project"


def _promote_reverify_candidate(
    project: Path,
    staged_project: Path,
    staging_root: Path,
) -> None:
    """Swap a proven sibling candidate into the canonical project path."""
    backup = staging_root / "original"
    recovery = project / ".skyn3t-recovery"
    if recovery.is_dir() and not (staged_project / recovery.name).exists():
        shutil.copytree(recovery, staged_project / recovery.name)
    try:
        os.replace(project, backup)
    except OSError as exc:
        raise ProjectReverifyError(
            _reverify_replace_failure_message(exc),
            status_code=500,
        ) from exc
    try:
        os.replace(staged_project, project)
    except OSError as exc:
        rollback_failed = False
        try:
            os.replace(backup, project)
        except OSError:
            rollback_failed = True
            log.exception("project.reverify_rollback_failed", slug=project.name)
        raise ProjectReverifyError(
            _reverify_replace_failure_message(exc),
            status_code=500,
            preserve_staging=rollback_failed,
            recovery_path=str(backup) if rollback_failed else "",
        ) from exc
    try:
        shutil.rmtree(backup)
    except OSError:
        log.warning("project.reverify_backup_cleanup_failed", path=str(backup))


def _review_binding(
    project: Path,
    manifest: dict[str, Any],
    review: dict[str, Any],
    identity: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    brief = str(manifest.get("brief") or "")
    base = {
        "verdict": str(review.get("verdict") or ""),
        "evidence_sha256": str(review.get("evidence_sha256") or ""),
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "binding": "unbound",
        "approved_current_tree": False,
        "source": "build_manifest",
        "valid": False,
    }
    if base["verdict"] != "go":
        base["binding"] = "reviewer_no_go"
        return base
    reviewed_tree = str(review.get("source_tree_sha256") or "").strip()
    snapshot_valid = review.get("source_tree_snapshot_valid")
    if snapshot_valid is False or (snapshot_valid is True and not reviewed_tree):
        base["binding"] = "review_tree_invalid"
        return base
    if reviewed_tree:
        reviewed_algorithm = str(
            review.get("source_tree_digest_algorithm") or ""
        ).strip()
        exact_match = (
            reviewed_algorithm == str(candidate.get("algorithm") or "")
            and reviewed_tree == str(candidate.get("sha256") or "")
        )
        base["binding"] = (
            "exact_tree"
            if exact_match
            else "exact_tree_mismatch"
        )
        base["approved_current_tree"] = base["binding"] == "exact_tree"
        base["valid"] = bool(base["approved_current_tree"])
        base["reviewed_tree_sha256"] = reviewed_tree
        return base

    durable_row = identity.get("durable_row")
    durable_manifest = (
        durable_row.get("manifest")
        if isinstance(durable_row, dict)
        and isinstance(durable_row.get("manifest"), dict)
        else None
    )
    if durable_manifest is None:
        return base
    durable_row_dict = durable_row if isinstance(durable_row, dict) else {}
    identity_fields = ("build_id", "slug", "brief", "stack")
    if any(
        str(durable_manifest.get(field) or "").strip()
        != str(manifest.get(field) or "").strip()
        for field in identity_fields
    ):
        base["binding"] = "legacy_identity_mismatch"
        return base
    durable_artifact = durable_manifest.get("artifact_dir") or durable_row_dict.get(
        "artifact_dir"
    )
    if not _resolved_artifact_matches(project, durable_artifact):
        base["binding"] = "legacy_identity_mismatch"
        return base
    durable_review = _completed_brief_review(durable_manifest)
    if (
        durable_review.get("verdict") != "go"
        or durable_review.get("evidence_sha256") != review.get("evidence_sha256")
    ):
        base["binding"] = "legacy_review_mismatch"
        return base
    base.update(
        {
            "binding": "legacy_durable_brief",
            "approved_current_tree": False,
            "source": "durable_build_row",
            "valid": True,
        }
    )
    return base


def _durable_review_evidence_matches(
    project: Path,
    manifest: dict[str, Any],
    review: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    """Verify the original reviewer evidence is durably tied to this build.

    A re-review is allowed only for a project that already has a persisted,
    identity-matched reviewer result.  This deliberately excludes a manifest
    someone could have hand-authored beside arbitrary project files.
    """
    durable_row = identity.get("durable_row")
    durable_manifest = (
        durable_row.get("manifest")
        if isinstance(durable_row, dict)
        and isinstance(durable_row.get("manifest"), dict)
        else None
    )
    if durable_manifest is None:
        return False
    identity_fields = ("build_id", "slug", "brief", "stack")
    if any(
        str(durable_manifest.get(field) or "").strip()
        != str(manifest.get(field) or "").strip()
        for field in identity_fields
    ):
        return False
    durable_row_dict = durable_row if isinstance(durable_row, dict) else {}
    durable_artifact = durable_manifest.get("artifact_dir") or durable_row_dict.get(
        "artifact_dir"
    )
    if not _resolved_artifact_matches(project, durable_artifact):
        return False
    durable_review = _completed_brief_review(durable_manifest)
    return (
        durable_review.get("verdict") == "go"
        and durable_review.get("evidence_sha256") == review.get("evidence_sha256")
    )


async def _fresh_reverify_review(
    project: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Run the canonical offline reviewer and bind it to the current tree.

    This intentionally constructs no model client.  It reruns the existing
    deterministic reviewer after proof has completed, preserving the original
    durable reviewer evidence while adding a new, exact-tree approval record.
    """
    from skyn3t.agents.reviewer import ReviewerAgent
    from skyn3t.core.agent import TaskRequest

    task = TaskRequest(
        type="review",
        payload={
            "project_dir": str(project),
            "brief": str(manifest.get("brief") or ""),
            "stack": str(manifest.get("stack") or ""),
        },
    )
    result = await ReviewerAgent(
        name="local-reverify-reviewer",
        llm_client=None,
    ).execute(task)
    output = result.output if result.success and isinstance(result.output, dict) else {}
    snapshot = source_tree_snapshot(project)
    gaps = [str(value) for value in output.get("gaps", [])]
    verdict = str(output.get("verdict") or "no_go").lower()
    if not snapshot.get("valid"):
        verdict = "no_go"
        gaps.append("source tree snapshot was invalid")
    try:
        score = float(output.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return {
        "name": "reverify-review",
        "agent_type": "reviewer",
        "capability": "review",
        "status": "completed",
        "score": score,
        "agent_name": "local-reverify-reviewer",
        "output_summary": {
            "score": score,
            "verdict": verdict,
            "gaps": gaps,
            "review_scope": "local_reverify_fresh_tree",
            "reviewer_mode": "deterministic_local",
            "source_tree_snapshot_valid": bool(snapshot.get("valid")),
            "source_tree_sha256": str(snapshot.get("sha256") or ""),
            "source_tree_digest_algorithm": str(snapshot.get("algorithm") or ""),
        },
    }


def _should_refresh_reverify_review(
    project: Path,
    manifest: dict[str, Any],
    review: dict[str, Any],
    identity: dict[str, Any],
    binding: dict[str, Any],
    promotion_checks: dict[str, Any],
) -> bool:
    """Allow a fresh review only when stale binding is the sole blocker."""
    failures = [str(value) for value in promotion_checks.get("failures", [])]
    return (
        binding.get("binding") == "exact_tree_mismatch"
        and failures == ["review evidence is not bound or durably corroborated"]
        and _durable_review_evidence_matches(project, manifest, review, identity)
    )


def _project_validation_requirements(project: Path, stack: str) -> dict[str, bool]:
    from skyn3t.studio.proof_run import package_declares_node_tests

    requirements = {
        "node_build": False,
        "node_tests": False,
        "python_tests": False,
        "swift_build": False,
        "swift_tests": False,
    }
    package_path = project / "package.json"
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            package = {}
        if isinstance(package, dict):
            scripts = package.get("scripts")
            scripts = scripts if isinstance(scripts, dict) else {}
            requirements["node_build"] = any(
                name in scripts for name in ("build", "typecheck", "check")
            )
            requirements["node_tests"] = package_declares_node_tests(package)
    snapshot = source_tree_snapshot(project)
    source_files = [str(value) for value in snapshot.get("files", [])]
    requirements["python_tests"] = any(
        Path(value).name.startswith("test_") and Path(value).suffix == ".py"
        for value in source_files
    )
    manifest_path = project / "Package.swift"
    if (stack or "").strip().lower() == "swift" or manifest_path.is_file():
        requirements["swift_build"] = manifest_path.is_file()
        try:
            swift_manifest = manifest_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            swift_manifest = ""
        requirements["swift_tests"] = "testTarget" in swift_manifest
    return requirements


def _reverify_promotion_checks(
    project: Path,
    stack: str,
    proof: Any,
    gates: dict[str, Any],
    review_binding: dict[str, Any],
    candidate: dict[str, Any],
    after_proof: dict[str, Any],
    live_source: dict[str, Any],
    live_after_worker: dict[str, Any],
) -> dict[str, Any]:
    detail = getattr(proof, "detail", None)
    detail = detail if isinstance(detail, dict) else {}
    requirements = _project_validation_requirements(project, stack)
    failures: list[str] = []
    if not bool(getattr(proof, "passed", False)):
        failures.append("fresh proof failed")
    gate_validation: dict[str, bool] = {}
    for gate_name, label in (
        ("security", "security check"),
        ("web_polish", "web polish check"),
        ("runtime_liveness", "runtime liveness check"),
    ):
        raw_gate = gates.get(gate_name)
        gate = raw_gate if isinstance(raw_gate, dict) else {}
        warnings = [str(value) for value in gate.get("warnings", [])]
        passed = gate.get("ok") is True and not (
            gate.get("skipped") is True and warnings
        )
        gate_validation[gate_name] = passed
        if passed:
            continue
        issues = [str(value) for value in gate.get("issues", [])]
        dead_routes = [str(value) for value in gate.get("dead_routes", [])]
        reason = str(gate.get("reason") or "").strip()
        detail_values = issues or dead_routes or warnings or ([reason] if reason else [])
        detail_text = ", ".join(detail_values[:3])
        failures.append(f"{label} failed" + (f": {detail_text}" if detail_text else ""))
    if not bool(candidate.get("valid")) or not bool(after_proof.get("valid")):
        failures.append("source tree snapshot was ambiguous or unreadable")
    unchanged = bool(candidate.get("sha256")) and (
        candidate.get("sha256") == after_proof.get("sha256")
    )
    if not unchanged:
        failures.append("source tree changed while local verification ran")
    live_unchanged = (
        bool(live_source.get("valid"))
        and bool(live_after_worker.get("valid"))
        and live_source.get("algorithm") == live_after_worker.get("algorithm")
        and live_source.get("sha256") == live_after_worker.get("sha256")
    )
    if not live_unchanged:
        failures.append("live source tree changed while the candidate was verified")
    if not review_binding.get("valid"):
        failures.append("review evidence is not bound or durably corroborated")
    expected = {
        "node_build": ("build", "passed"),
        "node_tests": ("node_tests", "passed"),
        "python_tests": ("tests", "passed"),
        "swift_build": ("build", "passed"),
        "swift_tests": ("swift_tests", "passed"),
    }
    validation: dict[str, str] = {}
    for requirement, required in requirements.items():
        detail_key, passing_value = expected[requirement]
        actual = str(detail.get(detail_key) or "not_applicable")
        validation[requirement] = actual if required else "not_applicable"
        if required and actual != passing_value:
            failures.append(f"declared {requirement.replace('_', ' ')} did not pass")
    if detail.get("missing_features"):
        failures.append("fresh proof found brief features missing from the project")
    if detail.get("scaffold_stub"):
        failures.append("fresh proof detected an unchanged scaffold")
    environment = detail.get("proof_environment")
    environment = environment if isinstance(environment, dict) else {}
    degraded_reasons = [
        str(value) for value in environment.get("degraded_reasons", [])
    ]
    required_test_steps = ("node_tests", "python_tests", "swift_tests")
    required_build_steps = ("node_build", "swift_build")
    completed_declared_tests = any(
        requirements[name] for name in required_test_steps
    ) and all(
        not requirements[name] or validation.get(name) == expected[name][1]
        for name in required_test_steps
    )
    completed_declared_builds = any(
        requirements[name] for name in required_build_steps
    ) and all(
        not requirements[name] or validation.get(name) == expected[name][1]
        for name in required_build_steps
    )
    blocking_degradation = [
        reason
        for reason in degraded_reasons
        if not reason.startswith("docker sandbox unavailable")
        and not (
            reason.startswith("tests skipped")
            and (
                completed_declared_tests
                or not any(requirements[name] for name in required_test_steps)
            )
        )
        and not (
            reason.startswith("build skipped")
            and (
                completed_declared_builds
                or not any(requirements[name] for name in required_build_steps)
            )
        )
    ]
    if blocking_degradation:
        failures.extend(f"proof degraded: {reason}" for reason in blocking_degradation)
    return {
        "passed": not failures,
        "failures": failures,
        "requirements": requirements,
        "validation": validation,
        "candidate_gates": gate_validation,
        "gates": gates,
        "source_unchanged_during_proof": unchanged,
        "live_source_unchanged": live_unchanged,
        "blocking_degradation": blocking_degradation,
    }


def _reverify_execution_evidence(proof: Any) -> dict[str, Any]:
    detail = getattr(proof, "detail", None)
    detail = detail if isinstance(detail, dict) else {}
    environment = detail.get("proof_environment")
    environment = environment if isinstance(environment, dict) else {}
    backend = str(environment.get("command_backend") or "unknown")
    isolation = (
        "not_enforced"
        if backend in {"local", "subprocess", "unknown", "not_run"}
        else "mixed_network_phases"
        if backend == "docker"
        else "unknown"
    )
    return {
        "skyn3t_model_invocations": 0,
        "skyn3t_model_cost_usd": 0.0,
        "claim_scope": "reverify_controller_only",
        "provider_adapter_constructed": False,
        "project_command_network_isolation": isolation,
        "external_requests_observed": None,
        "external_cost_usd": None,
    }


def _release_reverify_worker(claim: str, task: Any) -> None:
    result = None
    try:
        result = task.result()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - callback cleanup
        pass
    if isinstance(result, dict):
        staging_value = result.get("staging_root")
        _cleanup_reverify_staging(
            staging_value if isinstance(staging_value, Path) else None
        )
    if _REVERIFY_WORKERS.get(claim) is task:
        _REVERIFY_WORKERS.pop(claim, None)
        _REVERIFYING_PROJECTS.discard(claim)


def _reverify_score(
    proof: Any,
    review: dict[str, Any],
    settings: Any,
    extra: dict[str, Any],
) -> float:
    from skyn3t.studio.runner import StudioRunner

    proof_score = float(getattr(proof, "score", 0.0) or 0.0)
    review_score = review.get("score")
    score = (
        proof_score
        if review_score is None
        else 0.6 * float(review_score) + 0.4 * proof_score
    )
    detail = getattr(proof, "detail", None)
    environment = detail.get("proof_environment") if isinstance(detail, dict) else None
    if isinstance(environment, dict) and environment.get("degraded") is True:
        cap = float(getattr(settings, "degraded_proof_score_cap", 74.0))
        score = min(score, cap)
        extra["proof_environment_gate"] = {
            "degraded": True,
            "score_cap": cap,
            "reasons": list(environment.get("degraded_reasons") or []),
        }
    score = StudioRunner._shape_final_score(
        SimpleNamespace(extra=extra),
        proof,
        score,
        "go",
    )
    return round(max(0.0, min(100.0, score)), 2)


async def _persist_reverified_build(
    state: AppState,
    project: Path,
    manifest: dict[str, Any],
) -> bool | None:
    """Refresh the process cache and durable build row from one manifest."""
    build_id = str(manifest.get("build_id") or "")
    extra = manifest.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    summary = build_summary(manifest)
    recorded_costs = [
        value
        for raw in (manifest.get("cost_usd"), extra.get("build_cost_usd"))
        if (value := _nonnegative_build_cost(raw)) is not None
    ]
    cost = max(recorded_costs, default=0.0)
    record = getattr(state, "builds", {}).get(build_id)
    if record is None:
        record = BuildRecord(
            build_id=build_id,
            brief=str(manifest.get("brief") or ""),
            slug=str(manifest.get("slug") or project.name),
            stack=str(manifest.get("stack") or ""),
        )
        state.builds[build_id] = record
    updates = {
        "brief": str(manifest.get("brief") or ""),
        "slug": str(manifest.get("slug") or project.name),
        "stack": str(manifest.get("stack") or ""),
        "status": str(manifest.get("status") or ""),
        "score": manifest.get("score"),
        "verdict": str(manifest.get("verdict") or ""),
        "cost_usd": cost,
        "model_trace": dict(summary.get("model_trace") or {}),
        "quality_scorecard": dict(summary.get("quality_scorecard") or {}),
        "updated_at": time.time(),
    }
    for name, update_value in updates.items():
        try:
            setattr(record, name, update_value)
        except (AttributeError, TypeError):
            continue

    memory = getattr(state, "memory", None)
    if memory is None or not hasattr(memory, "save_build"):
        return None
    try:
        await memory.save_build(
            build_id=build_id,
            slug=updates["slug"],
            brief=updates["brief"],
            stack=updates["stack"],
            status=updates["status"],
            score=updates["score"],
            verdict=updates["verdict"],
            cost_usd=cost,
            artifact_dir=str(project),
            manifest=manifest,
        )
    except Exception as exc:  # noqa: BLE001 - disk/live state still remains usable
        log.warning(
            "project.reverify_persist_failed",
            slug=updates["slug"],
            error=str(exc)[:200],
        )
        return False
    return True


async def reverify_project(state: AppState, slug: str) -> dict[str, Any]:
    """Locally repair and re-prove a terminal non-delivered project.

    The controller constructs no model/provider adapter. Project-owned commands
    may still have host network access, which is reported as unknown external
    activity rather than mislabeled as zero total provider usage.
    """
    from skyn3t.studio.cleanup import _load_manifest

    project = _resolve_project_dir(state, slug)
    manifest = _load_manifest(project)
    if not isinstance(manifest, dict):
        raise ProjectReverifyError("project has no valid build manifest")

    manifest_slug = str(manifest.get("slug") or "").strip()
    build_id = str(manifest.get("build_id") or "").strip()
    status = _normalize_status(str(manifest.get("status") or ""))
    if not _terminal_non_delivered_manifest(manifest):
        raise ProjectReverifyError("project is not a terminal non-delivered build")
    if _reverify_file_count(project) <= 0:
        raise ProjectReverifyError("project has no files to verify")

    claim = str(project.resolve())
    if claim in _REVERIFYING_PROJECTS:
        raise ProjectReverifyError("local re-verification is already running")
    _REVERIFYING_PROJECTS.add(claim)
    release_claim = True
    staging_root: Path | None = None
    preserve_staging = False
    try:
        identity = await _validate_reverify_identity(state, project, manifest)
        manifest_slug = str(identity["slug"])
        build_id = str(identity["build_id"])
        if _active_reverify_status(status) or await _project_has_active_build(
            state,
            slug=manifest_slug,
            build_id=build_id,
        ):
            raise ProjectReverifyError("project belongs to an active build")
        review = _completed_brief_review(manifest)
        if review.get("verdict") != "go":
            raise ProjectReverifyError(
                "a completed brief-aware review with verdict go is required"
            )
        cancel_requested = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                _run_local_project_reverify,
                project,
                stack=str(manifest.get("stack") or ""),
                brief=str(manifest.get("brief") or ""),
                settings=state.settings,
                cancel_requested=cancel_requested,
            )
        )
        _REVERIFY_WORKERS[claim] = worker
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_requested.set()
            release_claim = False
            worker.add_done_callback(partial(_release_reverify_worker, claim))
            raise
        except ProjectReverifyError:
            raise
        except Exception as exc:  # noqa: BLE001 - return a controlled API failure
            log.exception("project.reverify_failed", slug=manifest_slug)
            raise ProjectReverifyError(
                "local re-verification failed to run",
                status_code=500,
            ) from exc

        staging_root = result["staging_root"]
        staged_project = result["project"]
        live_source = result["live_source"]
        repairs = result["repairs"]
        dependency_stabilization = result["dependency_stabilization"]
        candidate = result["candidate"]
        proof = result["proof"]
        gates = result["gates"]
        after_proof = result["after_proof"]
        live_after_worker = source_tree_snapshot(project)
        proof_dict = proof.to_dict()
        binding = _review_binding(
            project,
            manifest,
            review,
            identity,
            candidate,
        )
        promotion_checks = _reverify_promotion_checks(
            staged_project,
            str(manifest.get("stack") or ""),
            proof,
            gates,
            binding,
            candidate,
            after_proof,
            live_source,
            live_after_worker,
        )
        review_refreshed = False
        if _should_refresh_reverify_review(
            project,
            manifest,
            review,
            identity,
            binding,
            promotion_checks,
        ):
            stages = manifest.get("stages")
            if not isinstance(stages, list):
                stages = []
                manifest["stages"] = stages
            stages.append(await _fresh_reverify_review(staged_project, manifest))
            review = _completed_brief_review(manifest)
            binding = _review_binding(
                project,
                manifest,
                review,
                identity,
                candidate,
            )
            promotion_checks = _reverify_promotion_checks(
                staged_project,
                str(manifest.get("stack") or ""),
                proof,
                gates,
                binding,
                candidate,
                after_proof,
                live_source,
                live_after_worker,
            )
            review_refreshed = True
        promoted = bool(promotion_checks["passed"])
        previous = {
            "status": str(manifest.get("status") or ""),
            "verdict": str(manifest.get("verdict") or ""),
            "score": manifest.get("score"),
        }
        manifest["slug"] = manifest_slug
        manifest["files"] = list(
            (after_proof if promoted else live_after_worker).get("files") or []
        )
        extra_value = manifest.get("extra")
        extra = dict(extra_value) if isinstance(extra_value, dict) else {}
        extra["proof"] = proof_dict
        if promoted:
            manifest["status"] = "completed"
            manifest["verdict"] = "go"
            manifest["score"] = _reverify_score(
                proof,
                review,
                state.settings,
                extra,
            )
            extra.pop("wasted_usd", None)
            extra.pop("non_shippable_spend_usd", None)
            manifest.pop("wasted_usd", None)
            manifest.pop("non_shippable_spend_usd", None)
            extra["delivery_source_tree"] = {
                "algorithm": after_proof.get("algorithm"),
                "sha256": after_proof.get("sha256"),
                "file_count": after_proof.get("file_count"),
                "byte_count": after_proof.get("byte_count"),
                "verdict": "go",
            }
            if binding.get("binding") == "exact_tree":
                reason = (
                    "Fresh local proof and candidate gates passed against the exact "
                    "reviewed source tree."
                )
            else:
                reason = (
                    "Fresh local proof and candidate gates passed, and durable build "
                    "history corroborated the legacy brief-aware review."
                )
        else:
            reason = "; ".join(promotion_checks["failures"]) or (
                "Local re-verification did not satisfy every promotion check."
            )
        verified_at = datetime.now(UTC).isoformat()
        execution = _reverify_execution_evidence(proof)
        candidate_evidence = {
            "digest_algorithm": after_proof.get("algorithm"),
            "tree_sha256": after_proof.get("sha256"),
            "file_count": after_proof.get("file_count"),
            "byte_count": after_proof.get("byte_count"),
            "excluded_entries": after_proof.get("excluded_entries"),
            "unchanged_during_proof": promotion_checks[
                "source_unchanged_during_proof"
            ],
        }
        extra["reverify"] = {
            "schema_version": 2,
            "policy": "local-reverify-v2",
            "verified_at": verified_at,
            "promoted": promoted,
            "reason": reason,
            "previous": previous,
            "identity": {
                key: identity[key]
                for key in ("slug", "build_id", "project_relpath", "validated")
            },
            "candidate": candidate_evidence,
            "review": binding,
            "review_refreshed": review_refreshed,
            "execution": execution,
            "promotion_checks": promotion_checks,
            "repairs": repairs,
            "repairs_committed": promoted,
            "dependency_stabilization": dependency_stabilization,
            "gates": gates,
            "proof": proof_dict,
        }
        manifest["extra"] = extra
        manifest["updated_at"] = verified_at
        manifest_target = staged_project if promoted else project
        atomic_write_text(
            manifest_target / MANIFEST_FILENAME,
            json.dumps(manifest, indent=2, sort_keys=False),
        )
        if promoted:
            try:
                _promote_reverify_candidate(project, staged_project, staging_root)
            except ProjectReverifyError as exc:
                preserve_staging = bool(exc.preserve_staging)
                raise
        memory_persisted = await _persist_reverified_build(state, project, manifest)
        return {
            "slug": manifest_slug,
            "build_id": build_id,
            "promoted": promoted,
            "status": str(manifest.get("status") or ""),
            "verdict": str(manifest.get("verdict") or ""),
            "score": manifest.get("score"),
            "reason": reason,
            "skyn3t_model_invocations": 0,
            "execution": execution,
            "repairs": repairs,
            "proof": proof_dict,
            "review": binding,
            "review_refreshed": review_refreshed,
            "candidate": candidate_evidence,
            "gates": gates,
            "promotion_checks": promotion_checks,
            "memory_persisted": memory_persisted,
        }
    finally:
        if preserve_staging:
            log.error(
                "project.reverify_recovery_preserved",
                path=str(staging_root),
            )
        else:
            _cleanup_reverify_staging(staging_root)
        if release_claim:
            _REVERIFY_WORKERS.pop(claim, None)
            _REVERIFYING_PROJECTS.discard(claim)


def _project_build_record(
    state: AppState,
    slug: str,
    persisted: dict[str, Any] | None = None,
) -> Any | None:
    matches = [
        record
        for record in getattr(state, "builds", {}).values()
        if str(getattr(record, "slug", "") or "") == slug
    ]
    if matches:
        return max(
            matches,
            key=lambda record: float(getattr(record, "updated_at", 0.0) or 0.0),
        )
    return SimpleNamespace(**persisted) if persisted else None


async def _persisted_project_builds(
    state: AppState,
    slugs: list[str],
) -> dict[str, dict[str, Any]]:
    """Index the newest persisted build per slug for post-restart project hydration."""
    memory = getattr(state, "memory", None)
    if memory is None or not slugs:
        return {}
    try:
        if hasattr(memory, "latest_builds_by_slug"):
            rows = await memory.latest_builds_by_slug(slugs)
        elif hasattr(memory, "recent_builds"):
            limit = max(50, min(500, len(slugs) * 4))
            rows = await memory.recent_builds(limit=limit)
        else:
            return {}
    except Exception:  # noqa: BLE001 - project listing degrades to disk/live state
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for value in rows:
        if not isinstance(value, dict):
            continue
        slug = str(value.get("slug") or "").strip()
        if slug and slug not in indexed:
            indexed[slug] = value
    return indexed


def _project_has_static_preview(state: AppState, slug: str) -> bool:
    try:
        return (_preview_root(state, slug) / "index.html").is_file()
    except (OSError, ValueError):
        return False


def _compact_project_ai_fields(
    manifest: dict[str, Any],
    record: Any | None = None,
) -> dict[str, Any]:
    """Project the compact model trace without copying large prompt bodies."""
    summary = build_summary(manifest)
    manifest_trace_value = summary.get("model_trace")
    manifest_trace = (
        dict(manifest_trace_value) if isinstance(manifest_trace_value, dict) else {}
    )
    record_trace_value = getattr(record, "model_trace", {})
    record_trace = (
        dict(record_trace_value) if isinstance(record_trace_value, dict) else {}
    )
    trace = _merge_build_model_trace(record_trace, manifest_trace)
    extra_value = manifest.get("extra")
    extra = extra_value if isinstance(extra_value, dict) else {}

    profile = str(
        trace.get("profile")
        or summary.get("build_profile")
        or getattr(record, "build_profile", "")
        or extra.get("build_profile")
        or ""
    )
    backend = str(trace.get("backend") or extra.get("llm_backend") or "")
    codegen_model = str(
        trace.get("codegen_model")
        or trace.get("effective_codegen_model")
        or extra.get("effective_codegen_model")
        or extra.get("codegen_model")
        or ""
    )
    model_override = str(
        trace.get("model_override") or extra.get("model_override") or ""
    )
    stages_value = trace.get("stages")
    stage_costs_value = trace.get("stage_costs")
    trace["profile"] = profile
    trace["backend"] = backend
    trace["codegen_model"] = codegen_model
    trace["model_override"] = model_override
    trace["stages"] = list(stages_value) if isinstance(stages_value, list) else []
    trace["stage_costs"] = (
        list(stage_costs_value) if isinstance(stage_costs_value, list) else []
    )
    return {
        "build_profile": profile,
        "backend": backend,
        "llm_backend": backend,
        "codegen_model": codegen_model,
        "model_override": model_override,
        "model_trace": trace,
        "stages": trace["stages"],
        "stage_costs": trace["stage_costs"],
    }


def _compact_local_reverify(extra: dict[str, Any]) -> dict[str, Any]:
    """Expose the durable outcome of a local reverify without its large proof log."""
    raw = extra.get("reverify")
    if not isinstance(raw, dict):
        return {}
    proof = raw.get("proof")
    proof = proof if isinstance(proof, dict) else {}
    execution = raw.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    return {
        "verified_at": str(raw.get("verified_at") or ""),
        "promoted": raw.get("promoted") is True,
        "review_refreshed": raw.get("review_refreshed") is True,
        "proof_passed": proof.get("passed") is True,
        "score": raw.get("score"),
        "reason": str(raw.get("reason") or ""),
        "skyn3t_model_invocations": execution.get("skyn3t_model_invocations"),
    }


def _incomplete_project_row(
    state: AppState,
    project: Path,
    manifest: dict[str, Any] | None,
    persisted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = manifest or {}
    slug = str(raw.get("slug") or project.name)
    record = _project_build_record(state, slug, persisted)
    build_status = str(getattr(record, "status", "") or raw.get("status") or "")
    normalized = build_status.strip().lower()
    active = normalized in _ACTIVE_PROJECT_STATUSES
    reverify_active = _active_reverify_status(build_status)
    status = "building" if active else normalized or "incomplete"
    delivery_state = "building" if status == "building" else "incomplete"
    size_bytes, file_count = _project_visible_stats(project)
    stack = str(raw.get("stack") or getattr(record, "stack", "") or "")
    created_at = raw.get("created_at") or getattr(record, "created_at", "")
    updated_at = raw.get("updated_at") or getattr(record, "updated_at", "")
    raw_extra_value = raw.get("extra")
    raw_extra: dict[str, Any] = (
        dict(raw_extra_value) if isinstance(raw_extra_value, dict) else {}
    )
    local_reverify = _compact_local_reverify(raw_extra)
    summary = build_summary(raw) if raw else {}
    record_scorecard = getattr(record, "quality_scorecard", {})
    record_cost_truth = (
        record_scorecard.get("cost_truth")
        if isinstance(record_scorecard, dict)
        and isinstance(record_scorecard.get("cost_truth"), dict)
        else {}
    )
    cost_truth = record_cost_truth
    if not cost_truth and "build_cost_usd" in raw_extra:
        cost_truth = summary.get("cost_truth", {})
    record_cost = getattr(record, "cost_usd", None)
    manifest_cost = raw_extra.get("build_cost_usd", raw.get("cost_usd"))
    live_cost = _nonnegative_build_cost(record_cost)
    durable_cost = _nonnegative_build_cost(manifest_cost)
    if active and live_cost is not None and live_cost > 0:
        display_cost: float | None = live_cost
    elif durable_cost is not None:
        display_cost = durable_cost
    else:
        display_cost = live_cost
    prompts = raw_extra.get("prompts")
    skills_used = raw_extra.get("skills_used")
    recall_used = raw_extra.get("recall_used")
    stage_skills_used = raw_extra.get("stage_skills_used")
    ai_fields = _compact_project_ai_fields(raw, record)
    non_shippable_spend = _project_non_shippable_spend_usd(
        status=status,
        verdict=str(raw.get("verdict") or getattr(record, "verdict", "") or ""),
        cost_usd=display_cost,
        persisted_value=raw_extra.get("non_shippable_spend_usd"),
    )
    reason = "build is still in progress" if delivery_state == "building" else (
        "no completed build manifest"
        if status == "incomplete"
        else f"build {status} before delivery"
    )
    can_reverify, reverify_reason = _project_reverify_eligibility(
        project,
        manifest,
        status=status,
        active=reverify_active,
        visible_file_count=file_count,
    )
    return {
        "slug": slug,
        "stack": stack,
        "status": status,
        "build_status": build_status or "manifest_missing",
        "build_id": str(getattr(record, "build_id", "") or raw.get("build_id") or ""),
        "build_active": active,
        "delivery_state": delivery_state,
        "is_complete": False,
        "verdict": "",
        "score": None,
        "created_at": created_at,
        "updated_at": updated_at,
        "size_bytes": size_bytes,
        "file_count": file_count,
        "has_preview": False,
        "preview_url": "",
        "has_serve": False,
        "serve_kind": "",
        "serve_reason": reason,
        "has_manifest": manifest is not None,
        "can_reverify": can_reverify,
        "reverify_reason": reverify_reason,
        "cost_usd": display_cost,
        "cost_truth": cost_truth,
        "wasted_usd": raw_extra.get("wasted_usd"),
        "non_shippable_spend_usd": non_shippable_spend,
        "prompt_count": len(prompts) if isinstance(prompts, list) else 0,
        "skills_used": list(skills_used) if isinstance(skills_used, list) else [],
        "recall_used": list(recall_used) if isinstance(recall_used, list) else [],
        "stage_skills_used": dict(stage_skills_used)
        if isinstance(stage_skills_used, dict) else {},
        **ai_fields,
        "quality_scorecard": dict(summary.get("quality_scorecard") or {}),
        "local_reverify": local_reverify,
        "scaffold_stub_gate": {},
        "deploy_plan": {},
        "deployments": [],
        "live_url": "",
        "deploy_check": {},
    }


async def list_projects(state: AppState) -> dict[str, Any]:
    from skyn3t.studio.app_runner import build_run_spec
    from skyn3t.studio.cleanup import _load_manifest
    from skyn3t.studio.proof_run import detect_offline_starter_stub
    from skyn3t.studio.runner import StudioRunner
    pdir = Path(state.settings.projects_dir)
    out: list[dict[str, Any]] = []
    if pdir.is_dir():
        project_manifests = [
            (project, _load_manifest(project))
            for project in sorted(
                p for p in pdir.iterdir() if p.is_dir() and not p.name.startswith(".")
            )
        ]
        project_slugs = [
            str((manifest or {}).get("slug") or project.name)
            for project, manifest in project_manifests
        ]
        persisted_builds = await _persisted_project_builds(state, project_slugs)
        for d, man in project_manifests:
            manifest_slug = str((man or {}).get("slug") or d.name)
            persisted = persisted_builds.get(manifest_slug)
            if not _manifest_is_delivered(man):
                out.append(_incomplete_project_row(state, d, man, persisted))
                continue
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
            display_extra = dict(extra)
            local_reverify = _compact_local_reverify(display_extra)
            if scaffold_stub_gate:
                display_extra["scaffold_stub_gate"] = scaffold_stub_gate
            proof_dict = display_extra.get("proof") if isinstance(display_extra, dict) else None
            proof_dict = proof_dict if isinstance(proof_dict, dict) else {}
            try:
                score = StudioRunner._shape_final_score(
                    SimpleNamespace(extra=display_extra),
                    SimpleNamespace(
                        passed=proof_dict.get("passed") if "passed" in proof_dict else None,
                        score=float(proof_dict.get("score", score) or 0.0),
                        files_total=proof_dict.get("files_total"),
                        files_substantive=proof_dict.get("files_substantive"),
                        detail=proof_dict.get("detail") or {},
                    ),
                    float(score),
                    verdict or "no_go",
                )
            except Exception as exc:  # noqa: BLE001 - score display must never break listing
                log.warning(
                    "project_score_shape_error",
                    slug=m.get("slug", d.name),
                    error=str(exc)[:200],
                )
            serve_kind = ""
            serve_reason = "no web entrypoint"
            try:
                spec = build_run_spec(d, str(m.get("stack", "") or ""), port=0)
            except Exception as exc:  # noqa: BLE001 - one bad project must not break the list
                log.warning(
                    "project_serve_capability_error",
                    slug=m.get("slug", d.name),
                    error=str(exc)[:200],
                )
                spec = None
                serve_reason = "serve capability check failed"
            if spec is not None:
                serve_kind = spec.kind
                serve_reason = ""
            size_bytes, file_count = _project_visible_stats(d)
            slug = str(m.get("slug", d.name))
            has_preview = _project_has_static_preview(state, slug)
            record = _project_build_record(state, slug, persisted)
            record_active = _active_reverify_status(
                getattr(record, "status", "") if record is not None else ""
            )
            summary = build_summary(m)
            ai_fields = _compact_project_ai_fields(m, record)
            project_cost = extra.get("build_cost_usd")
            non_shippable_spend = _project_non_shippable_spend_usd(
                status=status,
                verdict=verdict,
                cost_usd=project_cost,
                persisted_value=extra.get("non_shippable_spend_usd"),
            )
            can_reverify, reverify_reason = _project_reverify_eligibility(
                d,
                m,
                status=status,
                active=record_active,
                visible_file_count=file_count,
            )
            out.append({
                "slug": slug,
                "stack": m.get("stack", ""),
                "status": status,
                "build_status": str(getattr(record, "status", "") or status),
                "build_id": str(getattr(record, "build_id", "") or m.get("build_id", "")),
                "build_active": False,
                "delivery_state": "delivered",
                "is_complete": True,
                "verdict": verdict,
                "score": score,
                "created_at": m.get("created_at", ""),
                "updated_at": m.get("updated_at", ""),
                "size_bytes": size_bytes,
                "file_count": file_count,
                "has_preview": has_preview,
                # A read-only capability lets browser navigation/iframes load a
                # token-authenticated preview without putting the bearer itself
                # in a URL. It exposes only the strict project-file allowlist.
                "preview_url": _project_preview_url(state, slug) if has_preview else "",
                "has_serve": bool(serve_kind),
                "serve_kind": serve_kind,
                "serve_reason": serve_reason,
                "has_manifest": man is not None,
                "can_reverify": can_reverify,
                "reverify_reason": reverify_reason,
                # Spec 2 cost attribution (None when a build predates it).
                "cost_usd": project_cost,
                "cost_truth": summary.get("cost_truth", {}),
                "wasted_usd": extra.get("wasted_usd"),
                "non_shippable_spend_usd": non_shippable_spend,
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
                **ai_fields,
                "quality_scorecard": dict(extra.get("quality_scorecard") or {})
                if isinstance(extra.get("quality_scorecard"), dict) else {},
                "local_reverify": local_reverify,
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


async def get_project_product(state: AppState, slug: str) -> dict[str, Any]:
    """Return a delivered project's durable, versioned product contract."""
    from skyn3t.studio.product_spec import ProductSpecStore

    project, _manifest = _require_delivered_project(state, slug)
    spec = await asyncio.to_thread(ProductSpecStore(project).load)
    return {
        "slug": slug,
        "available": spec is not None,
        "product": spec.to_dict() if spec is not None else None,
    }


async def capture_project_human_feedback(
    state: AppState,
    slug: str,
    *,
    feedback: Any,
    category: Any = None,
    context: Any = None,
    rating: Any = None,
) -> dict[str, Any]:
    """Distil a delivered project's human design feedback into shared lessons.

    The raw reviewer text is validated by ``human_feedback`` but intentionally
    is not echoed back or stored as a prompt. Only fixed, durable design rules
    are persisted to MemoryStore for future web-design builds to retrieve.
    """
    from skyn3t.intelligence.human_feedback import (
        HUMAN_DESIGN_LESSON_STACK,
        HUMAN_DESIGN_LESSON_STAGE,
        capture_human_design_feedback,
    )

    _project, manifest = _require_delivered_project(state, slug)
    stack = str(manifest.stack or "").strip()
    if not stack:
        raise ValueError("project has no detected build stack")
    result = await capture_human_design_feedback(
        getattr(state, "memory", None),
        feedback=feedback,
        category=category,
        context=context,
        rating=rating,
        source_build=manifest.build_id,
        event_bus=getattr(state, "event_bus", None),
    )
    return {
        "slug": slug,
        "stack": stack,
        "lesson_stack": HUMAN_DESIGN_LESSON_STACK,
        "stage": HUMAN_DESIGN_LESSON_STAGE,
        "feedback": {
            "category": result.feedback.category,
            "context": result.feedback.context,
            "rating": result.feedback.rating,
        },
        "captured": result.captured,
        "deduped": result.deduped,
        "lessons": [lesson.to_dict() for lesson in result.lessons],
    }


async def patch_project_product(
    state: AppState,
    slug: str,
    *,
    base_version: int,
    patch: dict[str, Any],
    reason: str = "",
) -> dict[str, Any]:
    """Optimistically revise a product contract without mutating build history."""
    from skyn3t.studio.product_spec import ProductSpecStore

    if (
        isinstance(base_version, bool)
        or not isinstance(base_version, int)
        or base_version < 1
    ):
        raise ValueError("base_version must be a positive integer")
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    project, _manifest = _require_delivered_project(state, slug)
    store = ProductSpecStore(project)
    if await asyncio.to_thread(store.load) is None:
        raise ProductSpecUnavailableError(slug)
    updated = await asyncio.to_thread(
        partial(
            store.update,
            base_version=base_version,
            patch=patch,
            actor="studio-gui",
            reason=str(reason or "").strip(),
            provenance={
                "interface": "studio-gui",
                "requirements_modified": "requirements" in patch,
            },
        )
    )
    return {
        "slug": slug,
        "available": True,
        "product": updated.to_dict(),
    }


async def research_project_product(
    state: AppState,
    slug: str,
    *,
    base_version: int,
    force_refresh: bool = True,
    github_client: Any | None = None,
) -> dict[str, Any]:
    """Run explicit clean-room GitHub research and append only optional ideas."""
    from skyn3t.studio.github_research import GitHubResearchClient
    from skyn3t.studio.product_spec import (
        ProductSpecConflictError,
        ProductSpecStore,
    )
    from skyn3t.studio.similarity_scout import SimilarityScout

    if (
        isinstance(base_version, bool)
        or not isinstance(base_version, int)
        or base_version < 1
    ):
        raise ValueError("base_version must be a positive integer")
    project, manifest = _require_delivered_project(state, slug)
    store = ProductSpecStore(project)
    current = await asyncio.to_thread(store.load)
    if current is None:
        raise ProductSpecUnavailableError(slug)
    if current.version != base_version:
        raise ProductSpecConflictError(base_version, current.version)

    client = github_client or GitHubResearchClient(
        token=str(getattr(state.settings, "github_token", "") or ""),
        max_results=max(
            8,
            int(getattr(state.settings, "github_similarity_max_repos", 8)),
        ),
    )
    scout = SimilarityScout(
        client,
        project_dir=project,
        max_results=int(
            getattr(state.settings, "github_similarity_max_repos", 8)
        ),
    )
    report = await scout.research(
        brief=current.goal or manifest.brief,
        stack=manifest.stack,
        requirements=current.requirements,
        force_refresh=bool(force_refresh),
    )
    updated = await asyncio.to_thread(
        partial(
            store.record_research,
            base_version=base_version,
            sources=report.research_sources,
            backlog=report.backlog,
            provenance={
                "interface": "studio-gui",
                "queries": list(report.queries),
                "requirements_modified": False,
                "force_refresh": bool(force_refresh),
            },
        )
    )
    return {
        "slug": slug,
        "available": True,
        "research": report.to_dict(),
        "product": updated.to_dict(),
        "requirements_modified": False,
    }


async def delete_project(state: AppState, slug: str) -> dict[str, Any]:
    projects_root = Path(state.settings.projects_dir).resolve()
    target = (projects_root / slug).resolve()
    if target == projects_root or not target.is_relative_to(projects_root):
        raise ValueError(f"invalid slug: {slug!r}")
    if not target.is_dir():
        raise FileNotFoundError(slug)
    if str(target) in _REVERIFYING_PROJECTS:
        raise ValueError("project local re-verification is still running")
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


def _serve_start_tasks(state: AppState) -> dict[str, asyncio.Task[Any]]:
    """Background Docker-preview launches, keyed by project slug."""
    tasks = getattr(state, "serve_start_tasks", None)
    if tasks is None:
        tasks = {}
        try:
            state.serve_start_tasks = tasks
        except Exception:  # noqa: BLE001 - test doubles may be read-only
            pass
    return tasks


_SERVE_HISTORY_SCHEMA_VERSION = 1
_SERVE_HISTORY_LIMIT = 40


def _serve_history_path(state: AppState) -> Path | None:
    """Return the restart-safe preview-launch history file, when configured."""
    data_dir = getattr(getattr(state, "settings", None), "data_dir", None)
    if not data_dir:
        return None
    try:
        return Path(data_dir).expanduser().resolve() / "serve_launch_history.json"
    except (OSError, TypeError, ValueError):
        return None


def _serve_history_cache(state: AppState) -> dict[str, list[dict[str, Any]]]:
    """Load a bounded, defensive history cache once per dashboard process."""
    cached = getattr(state, "serve_launch_history", None)
    if isinstance(cached, dict):
        return cached
    cache: dict[str, list[dict[str, Any]]] = {}
    path = _serve_history_path(state)
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            launches = raw.get("launches", {}) if isinstance(raw, dict) else {}
            if isinstance(launches, dict):
                for key, entries in launches.items():
                    if isinstance(key, str) and isinstance(entries, list):
                        cache[key] = [
                            dict(entry) for entry in entries[:_SERVE_HISTORY_LIMIT]
                            if isinstance(entry, dict)
                        ]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    try:
        state.serve_launch_history = cache
    except Exception:  # noqa: BLE001 - read-only test state still serves
        pass
    return cache


def _persist_serve_history(state: AppState) -> None:
    path = _serve_history_path(state)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps(
                {
                    "schema_version": _SERVE_HISTORY_SCHEMA_VERSION,
                    "launches": _serve_history_cache(state),
                },
                indent=2,
                sort_keys=True,
            ),
        )
    except OSError as exc:
        log.warning("web.serve_history_persist_failed", error=str(exc))


def _serve_elapsed_ms(entry: dict[str, Any]) -> int:
    try:
        started_at_ms = int(entry.get("started_at_ms") or 0)
    except (TypeError, ValueError):
        started_at_ms = 0
    return max(0, int(time.time() * 1000) - started_at_ms) if started_at_ms else 0


def _current_serve_launch(state: AppState, slug: str) -> dict[str, Any] | None:
    entries = _serve_history_cache(state).get(slug, [])
    return dict(entries[0]) if entries else None


def _record_serve_launch(
    state: AppState,
    slug: str,
    *,
    phase: str,
    message: str,
    status: str = "starting",
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    entry: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "status": status,
        "phase": phase,
        "message": message,
        "started_at": now,
        "started_at_ms": int(time.time() * 1000),
        "updated_at": now,
        "elapsed_ms": 0,
        "timeline": [
            {"phase": phase, "message": message, "at": now, "elapsed_ms": 0}
        ],
    }
    cache = _serve_history_cache(state)
    cache[slug] = [entry, *cache.get(slug, [])][:_SERVE_HISTORY_LIMIT]
    _persist_serve_history(state)
    return dict(entry)


def _update_serve_launch(
    state: AppState,
    slug: str,
    launch_id: str,
    *,
    phase: str,
    message: str,
    status: str = "starting",
    **extra: Any,
) -> dict[str, Any] | None:
    entries = _serve_history_cache(state).get(slug, [])
    entry = next((item for item in entries if item.get("id") == launch_id), None)
    if entry is None:
        return None
    now = datetime.now(UTC).isoformat()
    elapsed_ms = _serve_elapsed_ms(entry)
    timeline = entry.setdefault("timeline", [])
    if not isinstance(timeline, list):
        timeline = []
        entry["timeline"] = timeline
    if not timeline or (
        timeline[-1].get("phase") != phase
        or timeline[-1].get("message") != message
    ):
        timeline.append(
            {"phase": phase, "message": message, "at": now, "elapsed_ms": elapsed_ms}
        )
    entry.update(
        {
            "status": status,
            "phase": phase,
            "message": message,
            "updated_at": now,
            "elapsed_ms": elapsed_ms,
            **extra,
        }
    )
    _persist_serve_history(state)
    return dict(entry)


async def serve_history(state: AppState, slug: str) -> dict[str, Any]:
    _require_delivered_project(state, slug)
    return {"slug": slug, "launches": _serve_history_cache(state).get(slug, [])}

def _serve_starting_payload(
    slug: str,
    stack: str,
    launch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    launch = launch or {}
    return {
        "slug": slug,
        "url": "",
        "port": 0,
        "pid": None,
        "kind": stack or "web",
        "status": "starting",
        "detail": {
            "engine": "docker",
            "phase": str(launch.get("phase") or "queued"),
            "message": str(launch.get("message") or "Queued isolated preview"),
            "elapsed_ms": int(launch.get("elapsed_ms") or 0),
            "launch_id": str(launch.get("id") or ""),
            "fallback_used": False,
        },
    }

async def start_serve_project(state: AppState, slug: str) -> dict[str, Any]:
    """Queue a Docker-only preview and return immediately for UI polling.

    First-time dependency preparation can take several minutes. The launch
    record is updated phase-by-phase and persisted under ``data_dir`` so a
    restart does not erase the useful explanation of what Docker was doing.
    """
    _pdir, manifest = _require_delivered_project(state, slug)
    registry = _serve_registry(state)
    existing = registry.get(slug)
    if existing is not None and getattr(existing, "status", "") == "running":
        return {**existing.to_dict(), "slug": slug}
    tasks = _serve_start_tasks(state)
    existing_task = tasks.get(slug)
    if existing_task is not None and not existing_task.done():
        return _serve_starting_payload(
            slug, manifest.stack, _current_serve_launch(state, slug)
        )

    launch = _record_serve_launch(
        state,
        slug,
        phase="queued",
        message="Queued isolated Docker preview",
    )
    launch_id = str(launch["id"])

    async def _progress(phase: str, message: str) -> None:
        snapshot = _update_serve_launch(
            state,
            slug,
            launch_id,
            phase=phase,
            message=message,
        )
        await state.event_bus.emit(
            EventType.SERVE_STARTING,
            source="web.api",
            payload={
                "slug": slug,
                "phase": phase,
                "message": message,
                "elapsed_ms": int((snapshot or {}).get("elapsed_ms") or 0),
                "launch_id": launch_id,
            },
        )

    async def _launch() -> None:
        try:
            result = await serve_project(
                state, slug, progress_callback=_progress
            )
            if result.get("status") == "running":
                _update_serve_launch(
                    state,
                    slug,
                    launch_id,
                    phase="ready",
                    message="Preview is live",
                    status="running",
                    url=str(result.get("url") or ""),
                    port=int(result.get("port") or 0),
                )
                return
            reason = scrub_text(
                str((result.get("detail") or {}).get("reason") or result.get("status") or "preview failed")
            )[:1000]
            _update_serve_launch(
                state,
                slug,
                launch_id,
                phase="failed",
                message="Preview launch failed",
                status="failed",
                error=reason,
            )
            await state.event_bus.emit(
                EventType.SERVE_FAILED,
                source="web.api",
                payload={"slug": slug, "reason": reason, "launch_id": launch_id},
            )
        except asyncio.CancelledError:
            _update_serve_launch(
                state,
                slug,
                launch_id,
                phase="cancelled",
                message="Preview launch cancelled",
                status="cancelled",
            )
            await state.event_bus.emit(
                EventType.SERVE_STOPPED,
                source="web.api",
                payload={"slug": slug, "cancelled": True, "launch_id": launch_id},
            )
            raise
        except Exception as exc:  # noqa: BLE001 - background errors become UI evidence
            reason = scrub_text(f"preview launch error: {exc}")[:1000]
            _update_serve_launch(
                state,
                slug,
                launch_id,
                phase="failed",
                message="Preview launch failed",
                status="failed",
                error=reason,
            )
            await state.event_bus.emit(
                EventType.SERVE_FAILED,
                source="web.api",
                payload={"slug": slug, "reason": reason, "launch_id": launch_id},
            )
        finally:
            task = asyncio.current_task()
            if tasks.get(slug) is task:
                tasks.pop(slug, None)
            # A cancellation can leave the synchronous serve claim in place;
            # never let that placeholder make a later Serve look permanently busy.
            current = registry.get(slug)
            if current is not None and not hasattr(current, "status"):
                registry.pop(slug, None)

    task = asyncio.create_task(_launch(), name=f"skyn3t-serve-{slug}")
    tasks[slug] = task
    payload = _serve_starting_payload(slug, manifest.stack, launch)
    await state.event_bus.emit(
        EventType.SERVE_STARTING,
        source="web.api",
        payload={
            "slug": slug,
            "phase": payload["detail"]["phase"],
            "message": payload["detail"]["message"],
            "elapsed_ms": payload["detail"]["elapsed_ms"],
            "launch_id": launch_id,
        },
    )
    return payload

def _app_runner(state: AppState) -> Any:
    runner = getattr(state, "app_runner", None)
    if runner is None:
        from skyn3t.studio.preview_supervisor import PreviewSupervisor

        runner = PreviewSupervisor()
        try:
            state.app_runner = runner
        except Exception:  # noqa: BLE001
            pass
    return runner


async def _stop_running_app(runner: Any, app: Any) -> None:
    stopped = runner.stop(app)
    if hasattr(stopped, "__await__"):
        await stopped


def _pid_alive(pid: int) -> bool:
    return is_process_alive(pid)


async def serve_project(
    state: AppState,
    slug: str,
    *,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Start a delivered project as a live localhost server, registering the
    handle so a later stop can find it. Restarting a slug supersedes the prior
    run.

    Concurrency-safe without a lock (which would bind to one event loop): a
    synchronous claim token guards the pop -> ``await start`` -> register window.
    If a racing serve takes the slot, or a stop pops our claim, while we await
    start(), we detect the lost claim afterward and tear down our own process
    instead of leaking it (review findings #1, #2)."""
    pdir, man = _require_delivered_project(state, slug)
    from skyn3t.studio.app_runner import RunningApp, cleanup_serve
    registry = _serve_registry(state)
    runner = _app_runner(state)
    # Synchronously (before any await) claim the slot, superseding a *running*
    # predecessor. A bare object() is our claim identity.
    prev = registry.get(slug)
    if isinstance(prev, RunningApp):
        await _stop_running_app(runner, prev)
        cleanup_serve(prev)
    claim = object()
    registry[slug] = claim

    stack = man.stack
    app = await runner.start(
        pdir, stack, progress_callback=progress_callback
    )

    if registry.get(slug) is not claim:
        # A concurrent serve superseded us, or a stop cancelled us, mid-start:
        # we no longer own the slot, so tear down our own app rather than leak it.
        if app.status == "running":
            await _stop_running_app(runner, app)
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
    tasks = _serve_start_tasks(state)
    task = tasks.pop(slug, None)
    cancelled_start = task is not None and not task.done()
    if cancelled_start:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    registry = _serve_registry(state)
    app = registry.pop(slug, None)
    if app is None:
        return {"slug": slug, "stopped": cancelled_start}
    if not isinstance(app, RunningApp):
        # Popped an in-flight claim: the in-progress serve will self-cancel.
        return {"slug": slug, "stopped": True}
    runner = _app_runner(state)
    await _stop_running_app(runner, app)
    cleanup_serve(app)
    latest = _current_serve_launch(state, slug)
    if latest is not None and latest.get("status") == "running":
        _update_serve_launch(
            state,
            slug,
            str(latest.get("id") or ""),
            phase="stopped",
            message="Preview stopped",
            status="stopped",
        )
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
    for slug, task in list(_serve_start_tasks(state).items()):
        if task.done():
            continue
        if not isinstance(registry.get(slug), RunningApp):
            try:
                _pdir, manifest = _require_delivered_project(state, slug)
            except (FileNotFoundError, ProjectNotDeliveredError, ValueError):
                continue
            running.append(
                _serve_starting_payload(
                    slug, manifest.stack, _current_serve_launch(state, slug)
                )
            )
    return {"running": running}


async def visual_editor_inspect(
    state: AppState,
    slug: str,
    signature: dict[str, Any],
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Resolve one browser-selected element to ranked editable source hits."""
    project, _manifest = _require_delivered_project(state, slug)
    from skyn3t.studio.visual_editor import VisualEditor

    editor = VisualEditor(project)
    occurrences = await asyncio.to_thread(editor.inspect, signature, limit=limit)
    return {
        "slug": slug,
        "signature": dict(signature),
        "occurrences": [occurrence.to_dict() for occurrence in occurrences],
    }


async def visual_editor_style(state: AppState, slug: str) -> dict[str, Any]:
    """Return the managed visual CSS document and optimistic SHA."""
    project, _manifest = _require_delivered_project(state, slug)
    from skyn3t.studio.visual_editor import VisualEditor

    style = await asyncio.to_thread(VisualEditor(project).stylesheet_state)
    return {"slug": slug, "style": style.to_dict()}


# ---- batched visual annotations (v0 Design Mode port) ----------------------
# Click-to-comment pins collected in the preview arrive as ONE improve goal.
# Resolution borrows the visual editor's click-to-source inspection, but
# annotations never edit files: they only shape a goal for ImproveEngine.
_ANNOTATION_MAX_COUNT = 20
_ANNOTATION_COMMENT_MAX_CHARS = 2_000
_ANNOTATION_SELECTOR_MAX_CHARS = 200
_ANNOTATION_SOURCE_FILE_MAX_CHARS = 500
_ANNOTATION_SOURCE_LINE_MAX = 10_000_000
_ANNOTATION_SCREENSHOT_MAX_BYTES = 512 * 1024
_ANNOTATION_SELECTOR_RE = re.compile(r"^[A-Za-z0-9#._:\-\[\]()=\"' >+~,]+$")
_ANNOTATION_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ANNOTATION_DATA_ID_RE = re.compile(r'^\[data-skyn3t-id="([A-Za-z0-9_.:-]{1,128})"\]$')


def _validate_annotation(item: Any, index: int) -> dict[str, Any]:
    """Normalize one raw annotation into a bounded internal shape (422 input)."""
    label = f"annotation #{index + 1}"
    if not isinstance(item, dict):
        raise ValueError(f"{label} must be an object")
    comment = " ".join(str(item.get("comment", "") or "").split())
    if not 1 <= len(comment) <= _ANNOTATION_COMMENT_MAX_CHARS:
        raise ValueError(
            f"{label} comment must contain 1 to "
            f"{_ANNOTATION_COMMENT_MAX_CHARS} characters"
        )
    selector = str(item.get("selector", "") or "").strip()
    if len(selector) > _ANNOTATION_SELECTOR_MAX_CHARS or (
        selector and not _ANNOTATION_SELECTOR_RE.fullmatch(selector)
    ):
        raise ValueError(f"{label} selector contains unsupported characters")
    signature = item.get("signature")
    if signature is not None and not isinstance(signature, dict):
        raise ValueError(f"{label} signature must be an object")
    if not selector and not signature:
        raise ValueError(f"{label} requires a selector or element signature")
    source_file = str(item.get("source_file", "") or "").strip().replace("\\", "/")
    if source_file:
        parts = source_file.split("/")
        if (
            len(source_file) > _ANNOTATION_SOURCE_FILE_MAX_CHARS
            or source_file.startswith("/")
            or _ANNOTATION_DRIVE_RE.match(source_file)
            or "\x00" in source_file
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(f"{label} source_file must be a safe relative path")
    raw_line = item.get("source_line")
    source_line: int | None = None
    if raw_line is not None and raw_line != "":
        if isinstance(raw_line, bool) or not isinstance(raw_line, int):
            raise ValueError(f"{label} source_line must be an integer")
        if not 1 <= raw_line <= _ANNOTATION_SOURCE_LINE_MAX:
            raise ValueError(f"{label} source_line is out of range")
        source_line = raw_line
    screenshot = item.get("screenshot_b64")
    has_screenshot = False
    if screenshot is not None and screenshot != "":
        if not isinstance(screenshot, str) or len(screenshot) > 4 * (
            _ANNOTATION_SCREENSHOT_MAX_BYTES // 3 + 2
        ):
            raise ValueError(f"{label} screenshot exceeds the size cap")
        try:
            decoded = base64.b64decode(screenshot, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} screenshot must be base64") from exc
        if len(decoded) > _ANNOTATION_SCREENSHOT_MAX_BYTES:
            raise ValueError(f"{label} screenshot exceeds the size cap")
        has_screenshot = True
    return {
        "index": index + 1,
        "selector": selector,
        "comment": comment,
        "signature": signature,
        "source_file": source_file,
        "source_line": source_line,
        "screenshot": has_screenshot,
    }


def _annotation_selector_signature(selector: str) -> dict[str, Any] | None:
    """Derive an inspect signature from a simple id/class/data selector."""
    if not selector:
        return None
    data_match = _ANNOTATION_DATA_ID_RE.fullmatch(selector)
    if data_match:
        return {"element_id": data_match.group(1)}
    if selector.startswith("#"):
        return {"element_id": selector[1:]}
    if selector.startswith("."):
        classes = [part for part in selector.split(".") if part]
        return {"classes": classes} if classes else None
    return None


def _annotation_element_label(item: dict[str, Any]) -> str:
    """Human-readable element description for the shaped goal, e.g. h1.hero."""
    signature = item.get("signature") or {}
    tag = str(signature.get("tag", "") or "").strip()
    element_id = str(
        signature.get("element_id", signature.get("id", "")) or ""
    ).strip()
    raw_classes = signature.get("classes", ())
    if isinstance(raw_classes, str):
        raw_classes = raw_classes.split()
    classes = [str(name).strip() for name in raw_classes if str(name).strip()][:3]
    label = tag or "element"
    if element_id:
        label += f"#{element_id}"
    label += "".join(f".{name}" for name in classes)
    if label == "element" and item.get("selector"):
        return str(item["selector"])
    return label


def shape_annotations_goal(report: list[dict[str, Any]]) -> str:
    """Shape the batch into ONE numbered improve goal with per-element evidence."""
    noun = "annotation" if len(report) == 1 else "annotations"
    lines = [
        f"Address these {len(report)} visual {noun} "
        "(numbered pins dropped on elements in the live preview):"
    ]
    for entry in report:
        source = entry.get("source")
        hint = entry.get("hint")
        if source:
            label = entry.get("element") or entry.get("selector") or "element"
            location = f"{source['file']}:{source['line']} · {label}"
        elif hint:
            hint_line = f":{hint['line']}" if hint.get("line") else ""
            location = (
                f"{entry.get('selector') or 'element'} — source unresolved; "
                f"user-marked hint {hint['file']}{hint_line}"
            )
        else:
            location = f"{entry.get('selector') or 'element'} — source unresolved"
        lines.append(f"#{entry['index']} [{location}] {entry['comment']}")
    return "\n".join(lines)


async def annotations_improve(
    state: AppState,
    slug: str,
    body: Any,
) -> dict[str, Any]:
    """Resolve a batch of preview pins and dispatch ONE improve goal for them.

    Unresolvable pins are still accepted with ``source: null``; submission goes
    through the exact :func:`improve_project` path the improve UI uses."""
    raw_items: Any
    if isinstance(body, list):
        raw_items = body
    elif isinstance(body, dict):
        raw_items = body.get("annotations")
    else:
        raw_items = None
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("annotations must be a non-empty array")
    if len(raw_items) > _ANNOTATION_MAX_COUNT:
        raise ValueError(f"at most {_ANNOTATION_MAX_COUNT} annotations per batch")
    items = [
        _validate_annotation(item, index) for index, item in enumerate(raw_items)
    ]
    project, _manifest = _require_delivered_project(state, slug)
    from skyn3t.studio.visual_editor import ElementSignature, VisualEditor

    editor = VisualEditor(project)
    report: list[dict[str, Any]] = []
    for item in items:
        source: dict[str, Any] | None = None
        raw_signature = item["signature"] or _annotation_selector_signature(
            item["selector"]
        )
        if raw_signature:
            signature = ElementSignature.from_mapping(raw_signature)
            occurrences = await asyncio.to_thread(editor.inspect, signature, limit=1)
            if occurrences:
                top = occurrences[0]
                source = {"file": top.relative_path, "line": top.line}
        hint = None
        if item["source_file"]:
            hint = {"file": item["source_file"], "line": item["source_line"]}
        report.append(
            {
                "index": item["index"],
                "selector": item["selector"],
                "element": _annotation_element_label(item),
                "comment": item["comment"],
                "resolved": source is not None,
                "source": source,
                "hint": hint,
                "screenshot": item["screenshot"],
            }
        )
    goal = shape_annotations_goal(report)
    result = await improve_project(state, slug, goal)
    return {**result, "annotation_count": len(report), "annotations": report}


def _visual_quality_tasks(state: AppState) -> dict[str, Any]:
    tasks = getattr(state, "_visual_quality_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        state._visual_quality_tasks = tasks  # type: ignore[attr-defined]
    return tasks


async def get_visual_quality(state: AppState, slug: str) -> dict[str, Any]:
    """Return persisted Visual Quality Lab receipts for one delivered project."""
    from skyn3t.studio.visual_quality_lab import VisualQualityLab

    project, _manifest = _require_delivered_project(state, slug)
    tasks = _visual_quality_tasks(state)
    active = tasks.get(slug)
    return {
        "slug": slug,
        "running": bool(active is not None and not active.done()),
        "runs": await asyncio.to_thread(VisualQualityLab.list_runs, project),
    }


async def start_visual_quality(state: AppState, slug: str) -> dict[str, Any]:
    """Queue one local visual review and auto-repair run for a delivered project."""
    from skyn3t.studio.visual_quality_lab import VisualQualityLab

    project, manifest = _require_delivered_project(state, slug)
    tasks = _visual_quality_tasks(state)
    active = tasks.get(slug)
    if active is not None and not active.done():
        return {"accepted": False, "slug": slug, "reason": "visual quality run already in progress"}
    run_id = uuid.uuid4().hex
    lab = VisualQualityLab(
        project,
        slug=slug,
        brief=manifest.brief,
        stack=manifest.stack,
        settings=state.settings,
        event_bus=state.event_bus,
        orchestrator=state.orchestrator,
        memory=state.memory,
        skills=state.skills,
        rag=getattr(getattr(state, "studio", None), "rag", None),
    )

    async def worker() -> None:
        try:
            await lab.run(run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - the durable receipt records this too
            log.warning("visual_quality_lab.worker_failed", slug=slug, error=str(exc))
        finally:
            if tasks.get(slug) is task:
                tasks.pop(slug, None)

    task = asyncio.create_task(worker(), name=f"visual-quality:{slug}:{run_id[:8]}")
    tasks[slug] = task
    return {"accepted": True, "slug": slug, "run_id": run_id, "status": "queued"}


def visual_quality_artifact(state: AppState, slug: str, run_id: str, path: str) -> Path:
    """Resolve one stored lab artifact without exposing arbitrary project files."""
    from skyn3t.studio.visual_quality_lab import VisualQualityLab

    project, _manifest = _require_delivered_project(state, slug)
    candidate = VisualQualityLab.artifact_path(project, run_id, path)
    if candidate is None:
        raise FileNotFoundError(path)
    return candidate

def _visual_editor_lock(state: AppState, project: Path) -> asyncio.Lock:
    """Return the process-local transaction lock for one delivered project."""

    locks = getattr(state, "_visual_editor_locks", None)
    if not isinstance(locks, dict):
        locks = {}
        try:
            state._visual_editor_locks = locks  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - fail closed without lock storage
            raise RuntimeError("visual-editor project locking is unavailable") from exc
    key = str(project.resolve())
    lock = locks.get(key)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


async def _run_visual_editor_worker(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Keep the project lock held until a cancelled thread worker has stopped."""

    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(worker)
        except Exception:  # noqa: BLE001 - pending/no-go state remains authoritative
            pass
        raise


async def visual_editor_apply(
    state: AppState,
    slug: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Apply one typed visual edit and immediately re-run applicable proof."""
    project, _manifest = _require_delivered_project(state, slug)
    async with _visual_editor_lock(state, project):
        project, manifest = _require_delivered_project(state, slug)
        return await _visual_editor_apply_locked(
            state,
            slug,
            body,
            project=project,
            manifest=manifest,
        )


async def _visual_editor_apply_locked(
    state: AppState,
    slug: str,
    body: dict[str, Any],
    *,
    project: Path,
    manifest: BuildManifest,
) -> dict[str, Any]:
    from skyn3t.studio.proof_run import proof_run
    from skyn3t.studio.visual_editor import (
        EditKind,
        EditRequest,
        ElementSignature,
        VisualEditor,
    )
    from skyn3t.studio.visual_editor_integration import (
        VisualEditorIntegration,
        sync_visual_editor_assets,
    )

    signature_value = body.get("signature")
    signature = (
        ElementSignature.from_mapping(signature_value)
        if isinstance(signature_value, dict)
        else None
    )
    line_value = body.get("line")
    try:
        line = int(line_value) if line_value is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("line must be an integer") from exc
    request = EditRequest(
        kind=str(body.get("kind") or ""),
        base_sha=str(body.get("base_sha") or ""),
        value=body.get("value") if body.get("value") is None else str(body.get("value")),
        relative_path=str(body.get("relative_path") or ""),
        signature=signature,
        occurrence_id=str(body.get("occurrence_id") or ""),
        line=line,
        selector=str(body.get("selector") or ""),
        css_property=str(body.get("css_property") or body.get("property") or ""),
        breakpoint=str(body.get("breakpoint") or "base"),
    )
    editor = VisualEditor(project)
    kind = request.kind if isinstance(request.kind, EditKind) else EditKind(request.kind)

    original_status = manifest.status
    original_verdict = manifest.verdict
    original_score = manifest.score
    cap = float(getattr(state.settings, "degraded_proof_score_cap", 74.0))
    pre_edit_tree = await _run_visual_editor_worker(source_tree_snapshot, project)
    visual_state_value = manifest.extra.get("visual_editor")
    visual_state = (
        dict(visual_state_value) if isinstance(visual_state_value, dict) else {}
    )
    visual_state["verification_pending"] = True
    visual_state["verified"] = False
    visual_state["proof_binding"] = {
        "status": "pending",
        "algorithm": str(pre_edit_tree.get("algorithm") or ""),
        "pre_edit_source_tree_sha256": str(pre_edit_tree.get("sha256") or ""),
        "pre_edit_snapshot_valid": bool(pre_edit_tree.get("valid")),
        "matched": False,
    }
    manifest.extra["visual_editor"] = visual_state
    manifest.status = "completed_no_go"
    manifest.verdict = "no_go"
    manifest.score = min(float(manifest.score or cap), cap)
    manifest.save(project)

    edit = await _run_visual_editor_worker(editor.apply_edit, request)
    integration = VisualEditorIntegration(True, reason="not required for source edit")
    if kind in {EditKind.DESIGN_TOKEN, EditKind.LAYOUT}:
        integration = await _run_visual_editor_worker(
            sync_visual_editor_assets,
            project,
        )

    proof_tree = await _run_visual_editor_worker(source_tree_snapshot, project)
    proof = await _run_visual_editor_worker(
        proof_run,
        project,
        checklist=[],
        execution_backend=str(
            getattr(state.settings, "execution_backend", "docker")
        ),
        stack=manifest.stack,
        run_tests=bool(getattr(state.settings, "run_generated_tests", True)),
        test_timeout=int(getattr(state.settings, "generated_test_timeout", 90)),
        run_build=bool(getattr(state.settings, "run_generated_build", True)),
        build_timeout=int(getattr(state.settings, "generated_build_timeout", 300)),
    )
    after_proof_tree = await _run_visual_editor_worker(source_tree_snapshot, project)
    tree_binding = {
        "status": "bound",
        "scope": "proof_run",
        "algorithm": str(proof_tree.get("algorithm") or ""),
        "source_tree_sha256": str(proof_tree.get("sha256") or ""),
        "snapshot_valid": bool(proof_tree.get("valid")),
        "after_proof_source_tree_sha256": str(
            after_proof_tree.get("sha256") or ""
        ),
        "after_proof_snapshot_valid": bool(after_proof_tree.get("valid")),
        "matched": bool(
            proof_tree.get("valid")
            and after_proof_tree.get("valid")
            and proof_tree.get("algorithm") == after_proof_tree.get("algorithm")
            and proof_tree.get("sha256") == after_proof_tree.get("sha256")
        ),
    }
    ladder_payload: dict[str, Any] | None = None
    ladder_passed = True
    if (
        bool(getattr(state.settings, "proof_ladder_required", True))
        and manifest.stack
        in {
            "react",
            "react_vite",
            "vite",
            "nextjs",
            "next",
            "astro",
            "remix",
            "vue",
            "vuejs",
            "sveltekit",
            "svelte",
            "react_ts",
            "typescript",
            "static",
            "static_html",
            "tauri",
            "desktop",
            "phaser",
            "react_native",
        }
    ):
        from skyn3t.studio.preview_supervisor import ProofLadderCoordinator

        ladder = await ProofLadderCoordinator().run(project, manifest.stack)
        ladder_payload = ladder.to_dict()
        ladder_passed = ladder.passed

    verified = bool(proof.passed and ladder_passed and tree_binding["matched"])
    proof_payload = proof.to_dict()
    proof_payload["source_tree"] = tree_binding
    edit_entry = {
        **edit.to_dict(),
        "integration": integration.to_dict(),
        "proof_passed": proof.passed,
        "proof_ladder_passed": ladder_passed,
        "source_tree": tree_binding,
    }
    history_value = visual_state.get("history")
    history = list(history_value) if isinstance(history_value, list) else []
    visual_state["last_edit"] = edit_entry
    visual_state["history"] = [*history, edit_entry][-50:]
    visual_state["verification_pending"] = False
    visual_state["verified"] = verified
    visual_state["proof_binding"] = tree_binding
    manifest.extra["visual_editor"] = visual_state
    manifest.extra["proof_after_visual_edit"] = proof_payload
    if ladder_payload is not None:
        manifest.extra["proof_ladder_after_visual_edit"] = ladder_payload
    manifest.files = list_files(project)
    if verified:
        manifest.status = original_status
        manifest.verdict = original_verdict
        manifest.score = original_score
    else:
        manifest.status = "completed_no_go"
        manifest.verdict = "no_go"
        manifest.score = min(float(manifest.score or cap), cap)
    manifest.save(project)
    return {
        "slug": slug,
        "edit": edit.to_dict(),
        "integration": integration.to_dict(),
        "verification": {
            "passed": verified,
            "proof": proof_payload,
            "proof_ladder": ladder_payload,
            "source_tree": tree_binding,
        },
        "manifest": {
            "status": manifest.status,
            "verdict": manifest.verdict,
            "score": manifest.score,
        },
    }


async def deploy_plan_project(
    state: AppState,
    slug: str,
    *,
    target: str = "",
) -> dict[str, Any]:
    """Return the deploy plan for a delivered project without firing a deploy."""
    pdir, man = _require_delivered_project(state, slug)
    stack = man.stack
    from skyn3t.studio.deploy import plan_deploy

    requested_target = str(target or "").strip().lower()
    base_plan = plan_deploy(pdir, stack)
    valid_targets = list(base_plan.targets)
    plan = (
        plan_deploy(pdir, stack, target=requested_target)
        if requested_target in valid_targets
        else base_plan
    )
    provider_options = await _deploy_provider_options(
        state,
        pdir,
        man,
        base_plan,
        stack,
    )
    preflight = next(
        (item for item in provider_options if item["target"] == requested_target),
        None,
    )
    if requested_target and preflight is None:
        preflight = {
            "target": requested_target,
            "provider": _deploy_provider_key(requested_target),
            "target_supported": False,
            "ready": False,
            "blockers": [
                f"target '{requested_target}' is not supported for this {base_plan.kind} build"
            ],
        }
    extra = man.extra if isinstance(man.extra, dict) else {}
    return {
        "slug": slug,
        "stack": stack,
        "plan": plan.to_dict(),
        "live_url": str(extra.get("live_url") or ""),
        "deployments": list(extra.get("deployments") or [])
        if isinstance(extra.get("deployments"), list) else [],
        "deploy_check": dict(extra.get("deploy_check") or {})
        if isinstance(extra.get("deploy_check"), dict) else {},
        "provider_options": provider_options,
        "preflight": preflight,
    }


def _deploy_provider_key(target: str) -> str:
    normalized = str(target or "").strip().lower()
    return {
        "cloudflare-pages": "cloudflare",
        "cloudflare_pages": "cloudflare",
        "pages": "cloudflare",
        "flyctl": "fly",
    }.get(normalized, normalized)


def _deploy_quality_gate(manifest: BuildManifest) -> dict[str, Any]:
    from skyn3t.studio.deploy import deployment_quality_gate

    return deployment_quality_gate(manifest)


async def _deploy_provider_options(
    state: AppState,
    project_dir: Path,
    manifest: BuildManifest,
    base_plan: Any,
    stack: str,
) -> list[dict[str, Any]]:
    from skyn3t.studio.deploy import plan_deploy

    settings = await deploy_settings_payload(state)
    quality = _deploy_quality_gate(manifest)
    options: list[dict[str, Any]] = []
    for target in base_plan.targets:
        provider = _deploy_provider_key(target)
        detail = settings["provider_details"].get(provider, {})
        selected_plan = plan_deploy(project_dir, stack, target=target)
        blockers = list(quality["blockers"])
        if not bool(base_plan.serves_url):
            blockers.append("this plan produces an artifact, not a live URL")
        if not bool(settings["allow_remote_deploy"]):
            blockers.append("remote deploy is disabled in Settings")
        if not bool(detail.get("configured")):
            blockers.append(f"{provider} credential is not configured")
        if not bool(detail.get("cli_available")):
            blockers.append(f"{detail.get('cli') or provider} CLI is not installed")
        if selected_plan.kind == "static":
            output = Path(str(selected_plan.output_dir or "."))
            try:
                artifact = (project_dir / output).resolve(strict=True)
                artifact.relative_to(project_dir.resolve(strict=True))
                static_artifact_ready = (
                    not output.is_absolute()
                    and ".." not in output.parts
                    and artifact.is_dir()
                    and (artifact / "index.html").is_file()
                )
            except (OSError, ValueError):
                static_artifact_ready = False
            if not static_artifact_ready:
                blockers.append(
                    f"verified static artifact '{selected_plan.output_dir}' is missing"
                )
        else:
            static_artifact_ready = None
        options.append({
            "target": target,
            "provider": provider,
            "target_supported": True,
            "configured": bool(detail.get("configured")),
            "cli": str(detail.get("cli") or ""),
            "cli_available": bool(detail.get("cli_available")),
            "remote_allowed": bool(settings["allow_remote_deploy"]),
            "quality_gate": quality,
            "ready": not blockers,
            "blockers": blockers,
            "command": selected_plan.command,
            "build_command": selected_plan.build_command,
            "build_command_execution": "verified earlier; not rerun during deploy",
            "static_artifact_ready": static_artifact_ready,
            "kind": selected_plan.kind,
            "artifacts": sorted(selected_plan.artifacts),
            "health_check_enabled": bool(
                getattr(state.settings, "deploy_check_enabled", False)
            ),
        })
    return options


async def deploy_project(
    state: AppState,
    slug: str,
    *,
    target: str = "",
    write: bool = False,
) -> dict[str, Any]:
    """Fire the existing token-gated deploy path from the web surface."""
    pdir, man = _require_delivered_project(state, slug)
    stack = man.stack
    from skyn3t.agents.deploy_agent import DeployAgent
    from skyn3t.studio.deploy import (
        apply_deploy_health_gate,
        plan_deploy,
        record_deployment,
        write_deploy_artifacts,
    )

    requested_target = str(target or "").strip().lower()
    if not requested_target:
        raise DeployPreflightError(
            "deploy target is required; request the deploy plan and choose a provider",
            status_code=422,
        )
    if requested_target == "static":
        raise DeployPreflightError(
            "local static deploys are not durable; use the managed Serve preview instead"
        )
    base_plan = plan_deploy(pdir, stack)
    if requested_target not in base_plan.targets:
        raise DeployPreflightError(
            f"target '{requested_target}' is not supported for this {base_plan.kind} build",
            status_code=422,
        )
    plan = plan_deploy(pdir, stack, target=requested_target)
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
    provider = plan.targets[0] if plan.targets else ""
    if not provider:
        return {
            "slug": slug,
            "ok": False,
            "plan": plan.to_dict(),
            "result": {"ok": False, "url": None, "error": "no deploy target available"},
        }
    options = await _deploy_provider_options(state, pdir, man, base_plan, stack)
    preflight = next(
        (item for item in options if item["target"] == requested_target),
        None,
    )
    if preflight is None or not preflight["ready"]:
        blockers = list((preflight or {}).get("blockers") or ["deploy preflight failed"])
        raise DeployPreflightError("; ".join(str(item) for item in blockers))

    artifacts_written = (
        write_deploy_artifacts(plan, pdir) if (write or plan.artifacts) else []
    )
    deploy_config = {
        "allow_remote_deploy": bool(
            getattr(state.settings, "allow_remote_deploy", False)
        ),
        **{
            field: value
            for field in _DEPLOY_PROVIDER_FIELDS.values()
            if (value := str(getattr(state.settings, field, "") or ""))
        },
    }
    agent = DeployAgent(event_bus=state.event_bus, config=deploy_config)
    result = await asyncio.to_thread(
        agent.deploy,
        pdir,
        target=provider,
        plan=plan,
    )
    deploy_check: dict[str, Any] | None = None
    if result.get("ok") and result.get("url"):
        if getattr(state.settings, "deploy_check_enabled", False):
            try:
                from skyn3t.studio.deploy_check import check_deploy

                verdict = await check_deploy(str(result.get("url") or ""), stack)
                deploy_check = verdict.to_dict()
            except Exception as exc:  # noqa: BLE001 - persist an unverified attempt
                deploy_check = {
                    "ok": False,
                    "skipped": True,
                    "issues": [],
                    "checked": {},
                    "reason": f"deploy check unavailable: {str(exc)[:160]}",
                    "gaps": [],
                }
            result = apply_deploy_health_gate(result, deploy_check)
    record: dict[str, Any] | None = record_deployment(
        pdir,
        result=result,
        plan=plan,
        target=provider,
    )
    if result.get("ok") and result.get("url"):
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
    elif result.get("activation_blocked"):
        await state.event_bus.emit(
            EventType.SYSTEM,
            source="web.api",
            payload={
                "event": "deploy.activation_blocked",
                "slug": slug,
                "url": result.get("url"),
                "target": provider,
                "reason": result.get("activation_blocker"),
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
        "artifacts_written": artifacts_written,
    }


async def rollback_project_deployment(
    state: AppState,
    slug: str,
    *,
    reason: str = "",
    deployment_index: int | None = None,
) -> dict[str, Any]:
    """Move only the delivered project's local live-URL pointer backward."""
    pdir, _ = _require_delivered_project(state, slug)
    from skyn3t.studio.deploy import rollback_deployment

    result = rollback_deployment(
        pdir,
        reason=reason,
        deployment_index=deployment_index,
    )
    return {"slug": slug, **result}


async def improve_project(state: AppState, slug: str, goal: str) -> dict[str, Any]:
    """Dispatch an 'improve this project toward a goal' run as a background task,
    streaming IMPROVE_* events to the cockpit. Returns immediately with a
    correlation id; the project is validated up front so a bad slug 404s before
    any work starts."""
    if not goal or not goal.strip():
        raise ValueError("goal is required")
    project = _resolve_project_dir(state, slug)
    if str(project) in _REVERIFYING_PROJECTS:
        raise ValueError("project local re-verification is still running")
    _require_delivered_project(state, slug)
    if getattr(state, "orchestrator", None) is None:
        return {"accepted": False, "slug": slug, "reason": "orchestrator unavailable"}
    # Snapshot routing synchronously with the GUI submission. The background
    # task may wait behind another Improve while Settings continues to change.
    _enforce_build_routing(state)
    routing = _submission_routing_snapshot(state, "")
    from skyn3t.studio.improve import ImproveEngine
    engine = ImproveEngine(
        state.event_bus, state.orchestrator,
        settings=state.settings,
        memory=getattr(state, "memory", None),
        skills=getattr(state, "skills", None),
        rag=getattr(state, "rag", None),
        llm_client=getattr(state, "llm_client", None),
        routing_snapshot=routing,
    )
    cid = uuid.uuid4().hex
    import asyncio
    task = asyncio.ensure_future(engine.improve(slug, goal.strip(), correlation_id=cid))
    _IMPROVE_TASKS.add(task)
    task.add_done_callback(_reap_improve_task)
    return {"accepted": True, "slug": slug, "goal": goal.strip(), "correlation_id": cid}


async def fanout_project(
    state: AppState,
    brief: str,
    stacks: list[str],
    *,
    build_profile: str = "cheap_learned",
    model_override: str = "",
    full_app: bool = False,
    reference_image: str = "",
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
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
    _enforce_build_routing(state)
    from skyn3t.studio.fanout import FanCandidate, fan_out
    from skyn3t.studio.runner import _slugify
    cands = [FanCandidate(id=s, label=s, spec={"stack": s}) for s in ids]
    cid = uuid.uuid4().hex
    base = _slugify(brief)
    profile = _normalize_build_profile(build_profile)
    model = _normalize_model_override(model_override)
    full_app_requested = bool(full_app) or profile == "full_app"
    ref_path, ref_paths = _save_reference_images(
        state,
        cid,
        reference_image,
        reference_images,
    )
    common_extra: dict[str, Any] = {
        "build_profile": profile,
        **_orchestration_extra(
            profile,
            full_app=full_app_requested,
            asset_gen_enabled=bool(getattr(state.settings, "asset_gen", False)),
        ),
    }
    if model:
        common_extra["model_override"] = model
    if ref_paths:
        common_extra["reference_images"] = ref_paths
    if ref_path:
        common_extra["reference_image"] = ref_path

    async def build_fn(c):
        stack = (c.spec or {}).get("stack", "")
        candidate_extra = {**common_extra, "stack": stack}
        return await state.studio.start(
            brief.strip(), slug=f"{base}-{c.id}",
            extra=candidate_extra)

    import asyncio
    task = asyncio.ensure_future(
        fan_out(cands, build_fn, event_bus=state.event_bus, correlation_id=cid))
    _FANOUT_TASKS.add(task)
    task.add_done_callback(_reap_fanout_task)
    return {
        "accepted": True,
        "brief": brief.strip(),
        "stacks": ids,
        "correlation_id": cid,
        "build_profile": profile,
        "model_override": model,
        "full_app": full_app_requested,
        "reference_images": len(ref_paths),
    }


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
    decision = "approved" if approved else "rejected"
    correlation_id: str | None = None
    if rec is not None:
        correlation_id = rec.correlation_id
        lifecycle_status = rec.status
    else:
        correlation_id = build_id
        lifecycle_status = str((db_row or {}).get("status") or "")
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
    return {
        "build_id": build_id,
        "status": lifecycle_status,
        "decision": decision,
        "gate_resolved": gate_resolved,
        "applied": gate_resolved > 0,
    }


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


async def cortex_candidates_payload(
    state: AppState,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    """Return durable, bounded Cortex candidate reports and current policy."""
    from skyn3t.cortex.candidate_service import list_candidate_reports

    reports = await asyncio.to_thread(
        list_candidate_reports,
        state.settings,
        limit=max(1, min(int(limit), 100)),
    )
    return {
        "enabled": bool(
            getattr(state.settings, "cortex_candidates_enabled", True)
        ),
        "auto_merge": bool(
            getattr(state.settings, "cortex_candidate_auto_merge", False)
        ),
        "merge_strategy": str(
            getattr(
                state.settings,
                "cortex_candidate_merge_strategy",
                "ff-only",
            )
        ),
        "remote_push": False,
        "reports": reports,
    }


async def run_cortex_candidate_payload(
    state: AppState,
    *,
    goal: str,
) -> dict[str, Any]:
    """Run one isolated, selected-model candidate without blocking the event loop."""
    from skyn3t.cortex.candidate_service import run_cortex_candidate

    return await asyncio.to_thread(run_cortex_candidate, state.settings, goal)


def _lab_autopilot(state: AppState) -> Any:
    """Return the process-local controller backed by durable lab receipts."""
    controller = getattr(state, "_lab_autopilot_controller", None)
    if controller is None:
        from skyn3t.cortex.lab_autopilot import LabAutopilot

        controller = LabAutopilot(
            state.settings.data_dir,
            enabled=bool(getattr(state.settings, "lab_autopilot", False)),
        )
        state._lab_autopilot_controller = controller
    return controller


async def cortex_autopilot_payload(state: AppState) -> dict[str, Any]:
    """Plain-language local autonomy status for the Cortex dashboard."""
    return _lab_autopilot(state).payload()


async def set_cortex_autopilot(
    state: AppState, *, enabled: bool, persist: bool = True
) -> dict[str, Any]:
    """Enable or stop local Cortex autopilot without remote authority."""
    enabled = _coerce_bool(enabled)
    controller = _lab_autopilot(state)
    controller.set_enabled(enabled)
    try:
        state.settings.lab_autopilot = enabled
    except Exception:  # noqa: BLE001 - immutable test settings remain readable
        pass
    os.environ["SKYN3T_LAB_AUTOPILOT"] = "true" if enabled else "false"
    if persist:
        _persist_env_var("SKYN3T_LAB_AUTOPILOT", "true" if enabled else "false")
    if enabled:
        await set_lab_autonomy(state, True, persist=persist)
        await set_cortex_candidate_policy(
            state, enabled=True, auto_merge=True, persist=persist
        )
    return await cortex_autopilot_payload(state)


async def report_cortex_autopilot_incident(
    state: AppState, *, scope: str, category: str, summary: str, evidence: str = ""
) -> dict[str, Any]:
    """Record a deduplicated local repair signal for the next autopilot tick."""
    controller = _lab_autopilot(state)
    incident = controller.report_incident(
        scope=scope, category=category, summary=summary, evidence=evidence
    )
    return {"incident": asdict(incident), **controller.payload()}


async def tick_cortex_autopilot(state: AppState) -> dict[str, Any]:
    """Advance the durable queue by one local work item."""
    controller = _lab_autopilot(state)
    run = controller.next_run()
    return {"run": asdict(run) if run is not None else None, **controller.payload()}

def _cortex_graph_run_row(run: Any, comparison: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce a durable graph run to dashboard-safe experiment metadata."""
    raw_rerun = run.inputs.get("_graph_rerun")
    rerun = raw_rerun if isinstance(raw_rerun, dict) else {}
    raw_build = {
        name: run.inputs.get(name)
        for name in ("build_id", "slug", "stack")
        if run.inputs.get(name) not in (None, "")
    }
    statuses = {node.id: run.node_statuses[node.id].value for node in run.graph.nodes}
    rerunnable = [
        node_id for node_id, status in statuses.items() if status in {"succeeded", "cached"}
    ]
    comparison_payload = None
    if comparison is not None:
        comparison_payload = {
            name: comparison.get(name)
            for name in (
                "comparison_id",
                "source_run_id",
                "rerun_run_id",
                "from_node_id",
                "rerun_nodes",
                "baseline_digest",
                "candidate_digest",
                "outcome",
                "promotion_status",
                "created_at",
                "baseline_evidence",
                "candidate_evidence",
            )
        }
    return {
        "run_id": run.run_id,
        "graph_id": run.graph.graph_id,
        "graph_version": run.graph.version,
        "status": run.status.value,
        "created_at": run.created_at,
        "build": raw_build,
        "nodes": statuses,
        "rerunnable_nodes": rerunnable,
        "rerun": {
            "source_run_id": rerun.get("source_run_id"),
            "from_node_id": rerun.get("from_node_id"),
            "rerun_nodes": rerun.get("rerun_nodes", []),
        }
        if rerun
        else None,
        "comparison": comparison_payload,
    }


async def cortex_graph_runs_payload(
    state: AppState,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    """List bounded preflight graph evidence for review in the Cortex dashboard."""
    from skyn3t.studio.graph_runtime import GraphStore

    store = GraphStore(Path(state.settings.data_dir) / "build_graphs.sqlite3")
    try:
        runs = await store.list_runs(limit=max(1, min(int(limit), 100)))
        rows = []
        for run in runs:
            comparison = await store.load_rerun_comparison(run.run_id)
            rows.append(_cortex_graph_run_row(run, comparison))
        return {"available": True, "review_only": True, "runs": rows}
    except Exception:  # noqa: BLE001 - history must not break Cortex control plane
        log.warning("cortex.graph_history_unavailable")
        return {"available": False, "review_only": True, "runs": []}
    finally:
        await store.close()


async def rerun_cortex_graph_payload(
    state: AppState,
    *,
    source_run_id: str,
    from_node_id: str,
) -> dict[str, Any]:
    """Execute only a human-selected completed preflight branch for review."""
    from skyn3t.studio.build_intelligence import rerun_build_intelligence

    return await rerun_build_intelligence(
        settings=state.settings,
        source_run_id=source_run_id,
        from_node_id=from_node_id,
    )


def _cortex_graph_review_row(
    item: dict[str, Any],
    source_run: Any | None,
) -> dict[str, Any]:
    """Reduce one immutable comparison/receipt set for the decision inbox."""

    comparison = dict(item.get("comparison") or {})
    decision = item.get("decision")
    dispatch = item.get("build_dispatch")
    source_inputs = getattr(source_run, "inputs", {}) if source_run is not None else {}
    source_inputs = source_inputs if isinstance(source_inputs, dict) else {}
    source_build = {
        name: source_inputs.get(name)
        for name in ("build_id", "slug", "stack")
        if source_inputs.get(name) not in (None, "")
    }
    return {
        "comparison": {
            name: comparison.get(name)
            for name in (
                "comparison_id",
                "source_run_id",
                "rerun_run_id",
                "from_node_id",
                "rerun_nodes",
                "baseline_digest",
                "candidate_digest",
                "outcome",
                "promotion_status",
                "created_at",
            )
        },
        "source_build": source_build,
        "decision": dict(decision) if isinstance(decision, dict) else None,
        "build_dispatch": dict(dispatch) if isinstance(dispatch, dict) else None,
        "state": "awaiting_human_decision" if decision is None else "decided",
        "review_only": True,
        "auto_promotion": False,
    }


async def cortex_graph_reviews_payload(
    state: AppState,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    """List durable rerun comparisons that do or do not have a human receipt."""

    from skyn3t.studio.graph_runtime import GraphStore

    store = GraphStore(Path(state.settings.data_dir) / "build_graphs.sqlite3")
    try:
        items = await store.list_review_items(limit=max(1, min(int(limit), 100)))
        rows: list[dict[str, Any]] = []
        for item in items:
            comparison = dict(item.get("comparison") or {})
            source_run = await store.load_run(str(comparison.get("source_run_id") or ""))
            rows.append(_cortex_graph_review_row(item, source_run))
        return {
            "available": True,
            "review_only": True,
            "auto_promotion": False,
            "pending_count": sum(row["decision"] is None for row in rows),
            "reviews": rows,
        }
    except Exception:  # noqa: BLE001 - history must not break Cortex control plane
        log.warning("cortex.graph_review_history_unavailable")
        return {
            "available": False,
            "review_only": True,
            "auto_promotion": False,
            "pending_count": 0,
            "reviews": [],
        }
    finally:
        await store.close()


async def decide_cortex_graph_review_payload(
    state: AppState,
    *,
    comparison_id: str,
    decision: str,
    note: str = "",
) -> dict[str, Any]:
    """Append the only allowed human outcome for a graph comparison.

    This does not promote a candidate or change code/configuration/policy. It
    only records what a human decided about the exact source/candidate digests.
    """

    from skyn3t.studio.graph_runtime import GraphReviewDecision, GraphStore

    selected_id = str(comparison_id).strip()
    selected_decision = str(decision).strip().lower()
    selected_note = str(note).strip()
    if selected_decision not in {"keep", "reject"}:
        raise ValueError("decision must be 'keep' or 'reject'")
    if len(selected_note) > 2_000:
        raise ValueError("review decision note must be at most 2000 characters")
    store = GraphStore(Path(state.settings.data_dir) / "build_graphs.sqlite3")
    try:
        comparison = await store.load_rerun_comparison_by_id(selected_id)
        if comparison is None:
            raise KeyError(selected_id)
        if comparison.get("promotion_status") != "review_required":
            raise ValueError("only completed review-required comparisons can be decided")
        receipt = GraphReviewDecision(
            decision_id=uuid.uuid4().hex,
            comparison_id=selected_id,
            source_run_id=str(comparison.get("source_run_id") or ""),
            rerun_run_id=str(comparison.get("rerun_run_id") or ""),
            decision=selected_decision,
            note=selected_note,
            decided_by="dashboard",
            baseline_digest=str(comparison.get("baseline_digest") or ""),
            candidate_digest=str(comparison.get("candidate_digest") or ""),
            outcome=str(comparison.get("outcome") or ""),
        )
        await store.save_review_decision(receipt)
        return {
            "review_only": True,
            "auto_promotion": False,
            "decision": receipt.to_dict(),
        }
    finally:
        await store.close()


async def queue_cortex_graph_review_build_payload(
    state: AppState,
    *,
    comparison_id: str,
    brief: str,
) -> dict[str, Any]:
    """Start one normal Studio build from an explicitly kept experiment.

    The review receipt stays immutable and this path delegates to the ordinary
    build submission function, retaining its routing and admission safeguards.
    It is an operator action, not a promotion of graph evidence.
    """

    from skyn3t.studio.graph_runtime import (
        GraphReviewBuildDispatch,
        GraphReviewBuildRequest,
        GraphStore,
    )

    selected_id = str(comparison_id).strip()
    selected_brief = str(brief).strip()
    if not selected_brief:
        raise ValueError("follow-up build brief is required")
    if len(selected_brief) > 12_000:
        raise ValueError("follow-up build brief must be at most 12000 characters")
    store = GraphStore(Path(state.settings.data_dir) / "build_graphs.sqlite3")
    try:
        comparison = await store.load_rerun_comparison_by_id(selected_id)
        if comparison is None:
            raise KeyError(selected_id)
        decision = await store.load_review_decision(selected_id)
        if decision is None or decision.get("decision") != "keep":
            raise ValueError("keep the experiment evidence before queueing a follow-up build")
        existing = await store.load_review_build_dispatch(selected_id)
        if existing is not None:
            raise ValueError("a follow-up build is already queued for this decision")
        source_run = await store.load_run(str(comparison.get("source_run_id") or ""))
        source_inputs = getattr(source_run, "inputs", {}) if source_run is not None else {}
        stack = str(source_inputs.get("stack") or "") if isinstance(source_inputs, dict) else ""
        request = GraphReviewBuildRequest(
            request_id=uuid.uuid4().hex,
            decision_id=str(decision.get("decision_id") or ""),
            comparison_id=selected_id,
            brief_sha256=hashlib.sha256(selected_brief.encode("utf-8")).hexdigest(),
            stack=stack,
            requested_by="dashboard",
        )
        await store.save_review_build_request(request)
        queued = await submit_build(state, brief=selected_brief, stack=stack)
        build_id = str(queued.get("build_id") or "")
        if not build_id:
            raise RuntimeError("normal Studio build did not return a build id")
        dispatch = GraphReviewBuildDispatch(
            dispatch_id=uuid.uuid4().hex,
            request_id=request.request_id,
            decision_id=request.decision_id,
            comparison_id=selected_id,
            build_id=build_id,
        )
        await store.save_review_build_dispatch(dispatch)
        return {
            "review_only": True,
            "auto_promotion": False,
            "request": request.to_dict(),
            "build_dispatch": dispatch.to_dict(),
            "build": {"build_id": build_id, "status": queued.get("status", "queued")},
        }
    finally:
        await store.close()


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


_SKILL_QUARANTINE_TAGS = frozenset({"hygiene:quarantine", "quarantine", "disabled"})
_EXTERNAL_CANDIDATE_SKILL_TAG = "external-candidate"
_EXTERNAL_GITHUB_SKILL_SOURCE = "github-distilled"
_EXTERNAL_PROMOTION_REFUSAL = (
    "Not promoted. Only a quarantined GitHub-derived external candidate with a "
    "canonical repository URL, immutable 40/64-character revision, SHA-256 "
    "provenance hash, and source path can be promoted. Migrated candidates also "
    "need retained source bytes that match that hash; repair the provenance or "
    "receipt and try again."
)


def _skill_value(skill: Any, name: str, default: Any = None) -> Any:
    if isinstance(skill, dict):
        return skill.get(name, default)
    return getattr(skill, name, default)


def _skill_tags(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(tag) for tag in value if str(tag).strip()]


def _external_provenance_complete(skill: Any) -> bool:
    """Whether a GitHub-derived skill has complete immutable provenance fields.

    This is intentionally distinct from promotion readiness: older candidates
    are provenance-complete even when no retained-byte receipt exists, while
    migrated candidates additionally need that local receipt to verify.
    """
    source = str(_skill_value(skill, "source", "") or "").strip().lower()
    if source != _EXTERNAL_GITHUB_SKILL_SOURCE:
        return False
    try:
        from skyn3t.intelligence.skill_library import _has_complete_github_provenance

        return bool(_has_complete_github_provenance(_skill_value(skill, "provenance")))
    except Exception:  # noqa: BLE001 - unavailable provenance is never complete
        return False


def _external_promotion_ready(
    library: Any,
    skill: Any,
    *,
    tags: list[str] | None = None,
) -> bool:
    """Whether the live SkillLibrary will allow this one candidate to promote.

    The read-only library predicate is the sole readiness authority because a
    migrated legacy candidate must also verify its retained source bytes. The
    route keeps a narrow fallback of ``False`` rather than guessing from
    metadata when a custom library does not expose that predicate.
    """
    tagset = {
        tag.strip().lower()
        for tag in (tags or _skill_tags(_skill_value(skill, "tags", [])))
    }
    source = str(_skill_value(skill, "source", "") or "").strip().lower()
    slug = str(_skill_value(skill, "slug", "") or "").strip()
    if not (
        slug
        and source == _EXTERNAL_GITHUB_SKILL_SOURCE
        and _EXTERNAL_CANDIDATE_SKILL_TAG in tagset
        and bool(tagset & _SKILL_QUARANTINE_TAGS)
    ):
        return False
    checker = getattr(library, "can_promote_external", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(slug))
    except Exception:  # noqa: BLE001 - failed checks must keep candidates inert
        return False


def _skill_payload(skill: Any, *, library: Any = None) -> dict[str, Any]:
    """Serialize one skill plus its safe injection/promotion state."""
    if isinstance(skill, dict):
        out = dict(skill)
        title = str(out.get("title") or out.get("name") or "")
        body = str(out.get("body") or "")
        out.setdefault("slug", str(out.get("id") or ""))
        out.setdefault("title", title)
        out.setdefault("name", title)
        out.setdefault("stack", "")
        if "description" not in out:
            out["description"] = body[:160] + ("…" if len(body) > 160 else "")
        out.setdefault("score", 0)
        out.setdefault("source", "")
    else:
        title = str(getattr(skill, "title", "") or "")
        body = str(getattr(skill, "body", "") or "")
        out = {
            "slug": getattr(skill, "slug", ""),
            "title": title,
            "name": title,  # SPA card reads s.name
            "stack": getattr(skill, "stack", ""),
            "description": body[:160] + ("…" if len(body) > 160 else ""),
            "tags": list(getattr(skill, "tags", []) or []),
            "score": getattr(skill, "score", 0),
            "source": getattr(skill, "source", ""),
        }

    tags = _skill_tags(out.get("tags", []))
    tagset = {tag.strip().lower() for tag in tags}
    quarantined = bool(tagset & _SKILL_QUARANTINE_TAGS)
    out["tags"] = tags
    out["active"] = not quarantined
    out["quarantined"] = quarantined
    out["provenance_complete"] = _external_provenance_complete(skill)
    out["promotion_ready"] = _external_promotion_ready(library, skill, tags=tags)
    return out


def _skills_summary(skills: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "registered": len(skills),
        "active": sum(1 for skill in skills if skill.get("active") is True),
        "quarantined": sum(1 for skill in skills if skill.get("quarantined") is True),
        "promotion_ready": sum(1 for skill in skills if skill.get("promotion_ready") is True),
    }


async def promote_external_skill(state: AppState, slug: str) -> dict[str, Any]:
    """Promote exactly one evidence-valid external candidate, never a batch.

    ``SkillLibrary.promote_external`` owns the security contract. A refusal is
    returned as structured data so the authenticated UI/API caller can explain
    how to make a candidate eligible without treating a rejected action as a
    server outage.
    """
    candidate_slug = str(slug or "").strip()
    if not candidate_slug:
        raise ValueError("skill slug is required")
    promoter = getattr(getattr(state, "skills", None), "promote_external", None)
    if not callable(promoter):
        raise RuntimeError("the skills hub does not support reviewed external promotion")
    promoted = promoter(candidate_slug)
    if hasattr(promoted, "__await__"):
        promoted = await promoted
    if promoted is None:
        return {
            "status": "refused",
            "promoted": False,
            "slug": candidate_slug,
            "message": _EXTERNAL_PROMOTION_REFUSAL,
        }
    return {
        "status": "promoted",
        "promoted": True,
        "slug": candidate_slug,
        "skill": _skill_payload(promoted, library=getattr(state, "skills", None)),
        "message": f"Promoted {getattr(promoted, 'title', candidate_slug)} for future builds.",
    }


async def promote_all_ready_skills(state: AppState) -> dict[str, Any]:
    """Promote every currently evidence-ready external skill in one local action.

    Candidates that do not pass the existing immutable-evidence predicate are
    deliberately left untouched. This is a convenience bulk action, not a
    bypass of SkillLibrary's promotion contract.
    """
    library = getattr(state, "skills", None)
    getter = getattr(library, "all", None) or getattr(library, "list_skills", None)
    if not callable(getter):
        raise RuntimeError("the skills hub does not support reviewed external promotion")
    records = getter()
    if hasattr(records, "__await__"):
        records = await records
    ready = [
        skill for skill in list(records or [])
        if _external_promotion_ready(library, skill)
    ]
    promoted: list[dict[str, Any]] = []
    refused: list[str] = []
    for skill in ready:
        slug = str(_skill_value(skill, "slug", "") or "").strip()
        if not slug:
            continue
        result = await promote_external_skill(state, slug)
        if result.get("promoted"):
            promoted.append(result["skill"])
        else:
            refused.append(slug)
    return {
        "status": "completed",
        "attempted": len(ready),
        "promoted": promoted,
        "refused": refused,
        "message": f"Accepted {len(promoted)} ready skill{'s' if len(promoted) != 1 else ''}.",
    }

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
            out = [_skill_payload(skill, library=skills) for skill in res]
            return {"skills": out, "patterns": patterns, "summary": _skills_summary(out)}
        except Exception:  # noqa: BLE001
            pass
    # Degraded: surface configured skill-hub paths from settings.
    paths = [p for p in state.settings.skills_hub_paths.split(",") if p.strip()]
    return {
        "skills": [],
        "patterns": patterns,
        "summary": _skills_summary([]),
        "hub_paths": paths,
    }


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
    state: AppState, path: str, limit: int = 100, activate: bool = False
) -> dict[str, Any]:
    """Import local catalog roles as candidates or explicitly activate them."""
    if state.skills is None or not hasattr(state.skills, "add"):
        raise ValueError("a writable skill library is required to import catalogs")
    if not isinstance(activate, bool):
        raise ValueError("catalog activate must be a boolean")
    preview = await agent_catalog_preview(state, path, limit=limit)
    from skyn3t.intelligence.agent_catalog import import_catalog_as_skills

    imported = import_catalog_as_skills(
        preview["path"],
        state.skills,
        limit=max(1, min(int(limit or 100), 500)),
        activate=activate,
    )
    return {
        "path": preview["path"],
        "imported": imported,
        "summary": preview["summary"],
        "activation": {
            "requested": activate,
            "status": "activated" if activate else "quarantined",
            "activated": imported if activate else 0,
            "quarantined": 0 if activate else imported,
        },
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

_DEPLOY_PROVIDER_FIELDS = {
    "fly": "fly_api_token",
    "vercel": "vercel_token",
    "cloudflare": "cloudflare_api_token",
    "netlify": "netlify_auth_token",
    "railway": "railway_token",
    "render": "render_api_key",
}

_DEPLOY_PROVIDER_NATIVE_ENV = {
    "fly": "FLY_API_TOKEN",
    "vercel": "VERCEL_TOKEN",
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "netlify": "NETLIFY_AUTH_TOKEN",
    "railway": "RAILWAY_TOKEN",
    "render": "RENDER_API_KEY",
}

# Mirrors DeployAgent._PROVIDER_CLIS — these three maps must list the same
# providers Settings.deploy_tokens and DeployAgent support, or a provider
# becomes unreachable from the GUI (render was missing from all three while
# being fully supported downstream).
_DEPLOY_PROVIDER_CLIS = {
    "fly": "flyctl",
    "vercel": "vercel",
    "cloudflare": "wrangler",
    "netlify": "netlify",
    "railway": "railway",
    "render": "render",
}


def _persist_env_vars(values: dict[str, str]) -> None:
    """Atomically upsert safe ``NAME=value`` pairs in the repo .env.

    Settings values eventually become dotenv assignments. Reject line breaks and
    invalid names before touching the file so an API value cannot smuggle a second
    assignment into a future process. The helper remains best-effort: a failed local
    persistence write must never crash the live control plane.
    """
    normalized: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = str(raw_name or "").strip()
        value = str(raw_value or "")
        if not _ENV_NAME_RE.fullmatch(name) or any(ch in value for ch in ("\r", "\n", "\x00")):
            return
        normalized[name] = value
    if not normalized:
        return
    try:
        from skyn3t.config.settings import REPO_ROOT

        env = REPO_ROOT / ".env"
        with _ENV_WRITE_LOCK:
            lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
            out: list[str] = []
            found: set[str] = set()
            for ln in lines:
                stripped = ln.strip()
                # Only a real (non-comment) ``KEY=value`` assignment can match — a
                # commented line (``# KEY=...``) must be preserved verbatim, never
                # uncommented/overwritten.
                if stripped.startswith("#") or "=" not in stripped:
                    out.append(ln)
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key in normalized:
                    out.append(f"{key}={normalized[key]}")
                    found.add(key)
                else:
                    out.append(ln)
            for name, value in normalized.items():
                if name not in found:
                    out.append(f"{name}={value}")
            atomic_write_text(env, "\n".join(out) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _persist_env_var(name: str, value: str) -> None:
    """Upsert one safe dotenv assignment (best-effort; never raises)."""
    _persist_env_vars({name: value})


async def llm_secrets_payload(state: AppState) -> dict[str, Any]:
    import os

    from skyn3t.adapters.llm import openrouter_key

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
    providers = {}
    for provider, field in _PROVIDER_FIELDS.items():
        if provider == "openrouter":
            # Honor the persistent dashboard disconnect even if an external
            # shell supplies OPENROUTER_API_KEY.
            providers[provider] = bool(openrouter_key(s))
        else:
            providers[provider] = bool(
                getattr(s, field, "")
                or os.environ.get(f"SKYN3T_{field.upper()}")
                or os.environ.get(field.upper())
            )
    return {
        "providers": providers,
        "backend": backend,
        "routing": routing,
        "backend_pref": getattr(s, "llm_backend", "auto"),
        "cli_provider": getattr(s, "cli_llm_provider", "codex"),
        "codegen_cli_provider": getattr(s, "codegen_cli_provider", "") or "",
        "codegen_cli_model": getattr(s, "codegen_cli_model", "") or "",
        "openrouter_codegen_model": getattr(s, "openrouter_codegen_model", "") or "",
        "vision_model": getattr(s, "vision_model", "") or "",
        "codegen_model_slot": getattr(s, "codegen_model_slot", "") or "",
        "repair_model_slot": getattr(s, "repair_model_slot", "") or "",
        "free_only": bool(getattr(s, "free_only", True)),
        "no_claude": bool(getattr(s, "no_claude", False)),
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
    state: AppState, token: str | None, model: str | None = None, persist: bool = True
) -> dict[str, Any]:
    """Set the Replicate token (+ optional model) used for image generation.

    Updates the live Settings object AND os.environ (so a running cortex picks it
    up on its next build) and persists to .env. ``token=None`` preserves an
    existing token so the dashboard can update the non-secret model preference
    without asking the operator to re-enter a credential. An explicit empty
    token still disconnects Replicate. ``model`` is only updated when a non-empty
    value is supplied. Mirrors :func:`set_github_token`. Returns presence + the
    active model (never the token itself).
    """
    import os

    def _single_line(value: str, label: str) -> str:
        raw = str(value)
        if any(ch in raw for ch in ("\r", "\n", "\x00")):
            raise ValueError(f"{label} must be a single line")
        return raw.strip()

    normalized_token = None if token is None else _single_line(token, "Replicate token")
    normalized_model = ""
    if model is not None:
        normalized_model = _single_line(model, "Replicate model")
        if normalized_model and (
            len(normalized_model) > 256
            or not _REPLICATE_MODEL_RE.fullmatch(normalized_model)
        ):
            raise ValueError("Replicate model must use a single-line owner/model id")

    persist_values: dict[str, str] = {}
    if token is not None:
        assert normalized_token is not None
        try:
            state.settings.replicate_api_token = normalized_token
        except Exception:  # noqa: BLE001
            pass
        if normalized_token:
            os.environ["SKYN3T_REPLICATE_API_TOKEN"] = normalized_token
        else:
            os.environ.pop("SKYN3T_REPLICATE_API_TOKEN", None)
            # A disconnect is a single operation: it cannot leave the opt-in
            # asset flag armed for a later token reconnect.
            try:
                state.settings.asset_gen = False
            except Exception:  # noqa: BLE001
                pass
            os.environ["SKYN3T_ASSET_GEN"] = "false"
            persist_values["SKYN3T_ASSET_GEN"] = "false"
        persist_values["SKYN3T_REPLICATE_API_TOKEN"] = normalized_token

    if normalized_model:
        try:
            state.settings.replicate_model = normalized_model
        except Exception:  # noqa: BLE001
            pass
        os.environ["SKYN3T_REPLICATE_MODEL"] = normalized_model
        persist_values["SKYN3T_REPLICATE_MODEL"] = normalized_model
    if persist and persist_values:
        _persist_env_vars(persist_values)
    result = {
        "configured": bool(getattr(state.settings, "replicate_api_token", "")),
        "model": getattr(state.settings, "replicate_model", "") or "",
    }
    if normalized_token == "":
        # Backward-compatible response shape for normal connect/model updates;
        # disconnect callers receive the coupled opt-in state they just changed.
        result["asset_gen"] = bool(getattr(state.settings, "asset_gen", False))
    return result


async def golden_bench_payload(state: AppState) -> dict[str, Any]:
    """Live progress of golden-bench ledgers, straight from the durable files.

    Bench attempts run in ISOLATED per-attempt state on purpose (they must
    never pollute real build memory), which also made every run invisible to
    the dashboard — operators watched a blank cockpit while 62 builds ran.
    The ledgers under artifacts/golden are the bench's own progressive
    checkpoints, so reading them is the honest window.
    """

    def _collect() -> dict[str, Any]:
        from skyn3t.config.settings import REPO_ROOT

        ledgers: list[dict[str, Any]] = []
        root = REPO_ROOT / "artifacts" / "golden"
        try:
            candidates = sorted(root.glob("*.json"))
        except OSError:
            candidates = []
        for path in candidates:
            if path.name.startswith("comparison"):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict) or "attempts" not in raw:
                continue
            attempts = [a for a in raw.get("attempts") or [] if isinstance(a, dict)]
            cases = raw.get("case_ids") or []
            repeats = int(raw.get("repeats") or 0)
            raw_metadata = raw.get("metadata")
            metadata: dict[str, Any] = (
                dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            )
            raw_profile = metadata.get("safety_profile")
            profile: dict[str, Any] = (
                dict(raw_profile) if isinstance(raw_profile, dict) else {}
            )
            try:
                updated_at = path.stat().st_mtime
            except OSError:
                updated_at = 0.0
            ledgers.append({
                "name": path.stem,
                "status": str(raw.get("status") or ""),
                "attempts": len(attempts),
                "passed": sum(1 for a in attempts if a.get("passed")),
                "expected": (len(cases) * repeats) if cases and repeats else None,
                "llm_backend": str(metadata.get("llm_backend") or ""),
                # A lifted pin is what distinguishes a live (billed,
                # non-deterministic) ledger from the free floor.
                "live": bool(
                    profile.get("moa_enabled") or profile.get("codegen_cli_provider")
                ),
                "updated_at": updated_at,
                "completed_at": raw.get("completed_at"),
            })
        ledgers.sort(key=lambda entry: entry["updated_at"], reverse=True)
        return {"ledgers": ledgers}

    return await asyncio.to_thread(_collect)


async def deploy_settings_payload(state: AppState) -> dict[str, Any]:
    """Return deploy credential presence without returning credential values."""
    providers = {
        provider: bool(
            getattr(state.settings, field, "")
            or os.environ.get(f"SKYN3T_{field.upper()}")
            or os.environ.get(_DEPLOY_PROVIDER_NATIVE_ENV[provider])
        )
        for provider, field in _DEPLOY_PROVIDER_FIELDS.items()
    }
    allow_remote = bool(getattr(state.settings, "allow_remote_deploy", False))
    cli_available = {
        provider: bool(shutil.which(cli))
        for provider, cli in _DEPLOY_PROVIDER_CLIS.items()
    }
    provider_details = {
        provider: {
            "configured": configured,
            "cli": _DEPLOY_PROVIDER_CLIS[provider],
            "cli_available": cli_available[provider],
            "ready": bool(allow_remote and configured and cli_available[provider]),
        }
        for provider, configured in providers.items()
    }
    return {
        "providers": providers,
        "allow_remote_deploy": allow_remote,
        "cli_available": cli_available,
        "provider_details": provider_details,
        "selectable_providers": list(_DEPLOY_PROVIDER_FIELDS),
    }


async def set_deploy_credential(
    state: AppState,
    provider: str,
    token: str,
    persist: bool = True,
) -> dict[str, Any]:
    """Set or clear one allowlisted provider credential.

    Only the managed ``SKYN3T_*`` variable is changed; the token is never
    included in the response.
    """
    provider = (provider or "").strip().lower()
    config = _DEPLOY_PROVIDER_FIELDS.get(provider)
    if config is None:
        raise ValueError(f"unknown deploy provider {provider!r}")
    token = (token or "").strip()
    if "\r" in token or "\n" in token:
        raise ValueError("deploy credentials must be a single line")

    field = config
    try:
        setattr(state.settings, field, token)
    except Exception:  # noqa: BLE001 - keep the live route available for validated models
        pass
    env_name = f"SKYN3T_{field.upper()}"
    if persist:
        if token:
            os.environ[env_name] = token
        else:
            os.environ.pop(env_name, None)
        _persist_env_var(env_name, token)

    payload = await deploy_settings_payload(state)
    return {
        "provider": provider,
        "configured": payload["providers"][provider],
    }


async def set_allow_remote_deploy(
    state: AppState,
    enabled: bool,
    persist: bool = True,
) -> dict[str, Any]:
    """Set the explicit master gate for provider-side deploy commands."""
    enabled = bool(enabled)
    try:
        state.settings.allow_remote_deploy = enabled
    except Exception:  # noqa: BLE001 - keep the live route available for validated models
        pass
    value = "true" if enabled else "false"
    if persist:
        os.environ["SKYN3T_ALLOW_REMOTE_DEPLOY"] = value
        _persist_env_var("SKYN3T_ALLOW_REMOTE_DEPLOY", value)
    return {"allow_remote_deploy": enabled}


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


async def set_lab_autonomy(
    state: AppState, enabled: bool, persist: bool = True
) -> dict[str, Any]:
    """Toggle local build autonomy without weakening verification gates."""
    enabled = _coerce_bool(enabled)
    try:
        state.settings.lab_autonomy = enabled
    except Exception:  # noqa: BLE001 - frozen settings should not break the API
        pass
    os.environ["SKYN3T_LAB_AUTONOMY"] = "true" if enabled else "false"
    if persist:
        _persist_env_var("SKYN3T_LAB_AUTONOMY", "true" if enabled else "false")
    return {
        "lab_autonomy": enabled,
        "approval_gates_effective": bool(
            getattr(state.settings, "approval_gates", True)
        )
        and not enabled,
        "verification_gates_effective": True,
    }


async def set_cortex_candidate_policy(
    state: AppState,
    *,
    enabled: bool,
    auto_merge: bool,
    merge_strategy: str = "ff-only",
    persist: bool = True,
) -> dict[str, Any]:
    """Persist explicit consent for scoped Cortex candidates and local merging."""
    enabled = _coerce_bool(enabled)
    auto_merge = _coerce_bool(auto_merge)
    strategy = str(merge_strategy or "").strip().lower()
    if strategy not in {"ff-only", "squash"}:
        raise ValueError("merge_strategy must be ff-only or squash")
    values = {
        "cortex_candidates_enabled": bool(enabled),
        "cortex_candidate_auto_merge": bool(auto_merge),
        "cortex_candidate_merge_strategy": strategy,
    }
    for name, value in values.items():
        try:
            setattr(state.settings, name, value)
        except Exception:  # noqa: BLE001 - narrow immutable test doubles degrade
            pass
    env_values = {
        "SKYN3T_CORTEX_CANDIDATES_ENABLED": (
            "true" if values["cortex_candidates_enabled"] else "false"
        ),
        "SKYN3T_CORTEX_CANDIDATE_AUTO_MERGE": (
            "true" if values["cortex_candidate_auto_merge"] else "false"
        ),
        "SKYN3T_CORTEX_CANDIDATE_MERGE_STRATEGY": strategy,
    }
    os.environ.update(env_values)
    if persist:
        _persist_env_vars(env_values)
    return {
        **values,
        "verification_required": True,
        "remote_push": False,
        "protected_scope": True,
    }


async def set_similarity_research(
    state: AppState,
    enabled: bool,
    persist: bool = True,
) -> dict[str, Any]:
    """Toggle clean-room similar-project research for future builds."""
    enabled = _coerce_bool(enabled)
    try:
        state.settings.github_similarity_research = enabled
    except Exception:  # noqa: BLE001 - narrow test doubles may be immutable
        pass
    os.environ["SKYN3T_GITHUB_SIMILARITY_RESEARCH"] = (
        "true" if enabled else "false"
    )
    if persist:
        _persist_env_var(
            "SKYN3T_GITHUB_SIMILARITY_RESEARCH",
            "true" if enabled else "false",
        )
    return {
        "github_similarity_research": enabled,
        "max_repositories": int(
            getattr(state.settings, "github_similarity_max_repos", 8)
        ),
        "usage_policy": "metadata_readme_docs_manifests_only",
        "requirements_modified": False,
    }


async def lab_toolchain_payload(
    state: AppState,
    stack: str = "",
) -> dict[str, Any]:
    """Return live Docker/Playwright/Maestro readiness without mutating."""
    from skyn3t.studio.lab_tools import inspect_lab_toolchain

    report = await asyncio.to_thread(inspect_lab_toolchain, stack=stack)
    return report.to_dict()


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
    if prompt_rate is None or completion_rate is None:
        return None

    prompt_cost = max(0, int(prompt_tokens)) * prompt_rate
    completion_cost = max(0, int(completion_tokens)) * completion_rate
    request_cost = float(pricing.get("request") or 0.0)
    return round(prompt_cost + completion_cost + request_cost, 10)


def _model_pricing_summary(model_record: dict[str, Any] | None) -> str:
    if not isinstance(model_record, dict):
        return "price unknown"
    model_id = str(model_record.get("id") or "")
    if _is_free_model_id(model_id):
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


def _build_model_catalog_item(
    model_record: dict[str, Any],
    value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_id = str(model_record.get("id") or "").strip()
    pricing = _normalize_openrouter_pricing(model_record)
    prompt_rate = pricing.get("prompt", pricing.get("input"))
    completion_rate = pricing.get("completion", pricing.get("output"))
    request_rate = pricing.get("request")
    value = value or model_value_annotation([model_record], model_id)
    return {
        "id": model_id,
        "name": str(model_record.get("name") or model_id),
        "provider": _catalog_model_provider(model_id),
        "family": _catalog_model_family(model_id),
        "created": int(model_record.get("created", 0) or 0),
        "context_length": int(model_record.get("context_length", 0) or 0),
        "is_free": _is_free_model_id(model_id),
        "pricing_summary": _model_pricing_summary(model_record),
        "pricing_raw": pricing,
        "prompt_rate": prompt_rate,
        "completion_rate": completion_rate,
        "request_rate": request_rate,
        **value,
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
        cached_catalog = _MODELS_CACHE["catalog"]
        prime_live_catalog(cached_catalog, fetched_at=float(_MODELS_CACHE["ts"]))
        return cached_catalog, _MODELS_CACHE.get("note")

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
                    prime_live_catalog(catalog, fetched_at=now)
                    return catalog, "ok"
                except Exception as exc:  # noqa: BLE001 - try each auth mode, then fail safely
                    last_exc = exc
                    continue

            note = f"could not load OpenRouter catalog: {last_exc}"
            catalog = _MODELS_CACHE.get("catalog") or []
            prime_live_catalog(catalog, fetched_at=float(_MODELS_CACHE.get("ts") or now))
            _MODELS_CACHE.update(ts=now, note=note)
            return catalog, note
    except Exception as exc:  # noqa: BLE001 - degrade to cache/empty on API/network
        note = f"could not load OpenRouter catalog: {exc}"
        catalog = _MODELS_CACHE.get("catalog") or []
        prime_live_catalog(catalog, fetched_at=float(_MODELS_CACHE.get("ts") or now))
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
    # Keep the dropdown/bootstrap endpoint compact. Full pricing, benchmarks,
    # and value alternatives live on the filtered/paginated /models/catalog
    # endpoint; returning them here as well duplicated roughly 482 KiB.
    models = sorted(
        str(model.get("id"))
        for model in catalog
        if isinstance(model, dict) and isinstance(model.get("id"), str)
    )
    payload = {
        "models": models,
        "count": len(models),
        "details_endpoint": "/models/catalog",
    }
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

    value_by_model = catalog_value_annotations(catalog)
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
        if only_free and not _is_free_model_id(model_id_l):
            continue
        if normalized_query and normalized_query not in model_id_l:
            continue
        if normalized_provider and normalized_provider != provider_name:
            continue
        if normalized_family and normalized_family not in family_name.lower():
            continue

        filtered.append(_build_model_catalog_item(m, value_by_model.get(model_id)))

    def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        if normalized_sort in {"created", "newest"}:
            return (item.get("created", 0), item["provider"], item["id"])
        if normalized_sort == "provider":
            return (item["provider"], item["family"], item["id"])
        if normalized_sort == "price":
            unknown = float("inf")
            example_cost = item.get("example_cost_usd")
            if isinstance(example_cost, (int, float)):
                return (float(example_cost), item["id"])
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
        "example_workload": workload_payload(),
    }

async def model_routing_preview_payload(
    state: AppState,
    build_profile: str = "cheap_learned",
    model_override: str = "",
) -> dict[str, Any]:
    """Estimate the currently selected model per router tier and attach pricing.

    Build-time codegen, vision, slice, failover, and task-specific learned routes
    have additional precedence, so this endpoint must not claim to be an exact
    execution trace. Completed build manifests remain authoritative.
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
        value = model_value_annotation(catalog, str(model), profile=tier.value)
        tier_models.append({
            "tier": tier.value,
            "model": str(model),
            "source": source,
            "resolution_kind": "estimate",
            "pricing": _model_pricing_summary(catalog_map.get(str(model))),
            "pricing_raw": pricing,
            **value,
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
        "resolution_kind": "estimate",
        "authoritative": False,
        "estimate_reason": (
            "Tier-level estimate from current settings and catalog; build-time codegen, "
            "vision, slice, failover, and task-specific learned routing may differ."
        ),
        "backend": backend,
        "build_profile": profile,
        "model_override": override,
        "preferred_model": preferred,
        "free_only": free_only,
        "catalog_note": catalog_note,
        "catalog_age_seconds": catalog_age_seconds,
        "catalog_model_count": len(catalog),
        "example_workload": workload_payload(),
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

    from skyn3t.adapters.llm import openrouter_key

    field = _PROVIDER_FIELDS.get((provider or "").lower())
    if field is None:
        raise ValueError(f"unknown provider {provider!r}")
    raw_key = str(key or "")
    if any(ch in raw_key for ch in ("\r", "\n", "\x00")):
        raise ValueError("LLM API keys must be a single line")
    key = raw_key.strip()
    setattr(state.settings, field, key)
    env_name = f"SKYN3T_{field.upper()}"
    if field == "openrouter_api_key":
        # Do not mutate a parent-managed OPENROUTER_API_KEY. Instead persist a
        # local enable flag so Disconnect consistently prevents this app from
        # resolving either credential alias, including after a restart.
        enabled = bool(key)
        try:
            state.settings.openrouter_enabled = enabled
        except Exception:  # noqa: BLE001 - support narrow test/state doubles
            pass
        if persist:
            enabled_value = "true" if enabled else "false"
            os.environ["SKYN3T_OPENROUTER_ENABLED"] = enabled_value
            _persist_env_var("SKYN3T_OPENROUTER_ENABLED", enabled_value)
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
        "configured": (
            bool(openrouter_key(state.settings))
            if field == "openrouter_api_key"
            else bool(key)
        ),
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

    from skyn3t.adapters.llm import SUPPORTED_LLM_BACKENDS

    backend = (backend or "auto").strip().lower()
    if backend not in SUPPORTED_LLM_BACKENDS:
        allowed = ", ".join(SUPPORTED_LLM_BACKENDS)
        raise ValueError(f"Unsupported LLM backend {backend!r}; use one of: {allowed}")
    state.settings.llm_backend = backend  # type: ignore[assignment]
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
_CODEGEN_CLI_PROVIDERS = {"", "codex", "claude", "kimi", "copilot"}


async def set_llm_routing(
    state: AppState,
    *,
    codegen_cli_provider: str | None = None,
    codegen_cli_model: str | None = None,
    openrouter_codegen_model: str | None = None,
    model_pins: dict[str, Any] | None = None,
    vision_model: str | None = None,
    codegen_model_slot: str | None = None,
    repair_model_slot: str | None = None,
    free_only: bool | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Set model-routing controls used by future builds.

    Missing fields are left unchanged; explicit empty strings clear a
    pin/override. Values are always written to the live Settings object;
    ``persist=True`` additionally writes process env and .env, while
    ``persist=False`` stays scoped to the live object so the change cannot
    survive a Settings() reconstruction.
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
    if vision_model is not None:
        updates["vision_model"] = _normalize_model_id(vision_model)
    if codegen_model_slot is not None:
        updates["codegen_model_slot"] = (codegen_model_slot or "").strip()
    if repair_model_slot is not None:
        updates["repair_model_slot"] = (repair_model_slot or "").strip()
    if model_pins is not None:
        for tier, field in _MODEL_PIN_FIELDS.items():
            if tier in model_pins:
                updates[field] = _normalize_model_id(str(model_pins.get(tier) or ""))

    for field, value in updates.items():
        try:
            setattr(state.settings, field, value)
        except Exception:  # noqa: BLE001
            pass
        # persist=False is DELIBERATELY env-free (pinned by
        # test_set_llm_routing_persist_false_does_not_mutate_env): a
        # non-persisted routing change is scoped to this AppState's live
        # settings and must not survive a Settings() reconstruction via
        # process env. Audit M22 flagged the old docstring, which promised
        # env writes — the docs were wrong, not this gate.
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

    llm_client = state.llm_client
    if llm_client is not None:
        try:
            llm_client.settings = state.settings
        except Exception:  # noqa: BLE001
            pass
        client_router = getattr(llm_client, "router", None)
        if client_router is not None:
            state.router = client_router

    routing = (
        llm_client.backend_status()
        if llm_client is not None and hasattr(llm_client, "backend_status")
        else {}
    )
    tiers: dict[str, str] = {}
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
            "vision_model", "codegen_model_slot", "repair_model_slot",
            "auto_route", "model_evolution", "app_type_override", "engine_override",
            "visual_self_heal", "visual_self_heal_max_rounds",
            "improve_agentic", "improve_agentic_timeout",
            "parallel_code_slices", "parallel_code_slices_min_files",
            "lab_autonomy", "github_similarity_research",
            "github_similarity_max_repos", "proof_ladder_required",
            "build_graph_max_concurrency", "cortex_candidates_enabled",
            "cortex_candidate_auto_merge",
            "cortex_candidate_merge_strategy", "cortex_candidate_timeout",
            # Gate posture: whether a gate's finding BLOCKS the verdict, as
            # opposed to whether the gate RUNS (that stays each gate's own
            # *_enabled flag, driven by gates_payload/set_gate_enabled).
            "build_posture", "blocking_gates",
            # Multi-provider routing.
            "auto_cli_priority", "auto_allow_openrouter",
            "cli_max_concurrency", "provider_max_concurrency",
            # Mixture-of-Agents advisory council.
            "moa_enabled", "moa_advisors", "moa_max_concurrency",
            "moa_advisor_timeout", "moa_advisor_max_tokens",
            "moa_advisor_block_bytes", "moa_trace_enabled")
    payload = {k: getattr(s, k, None) for k in keys}
    deploy = await deploy_settings_payload(state)
    payload["allow_remote_deploy"] = deploy["allow_remote_deploy"]
    payload["deploy_providers"] = deploy["providers"]
    return payload


# ---------------------------------------------------------------------------
# FastAPI wiring (only constructed when FastAPI is importable).
# ---------------------------------------------------------------------------
def _durable_trajectory_event(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a MessageRow projection to the existing Event.to_dict shape."""
    payload_value = row.get("payload")
    payload = dict(payload_value) if isinstance(payload_value, dict) else {}
    payload.pop("__skyn3t_event__", None)
    event_id = str(row.get("event_id") or f"persisted-{row.get('id', '')}")
    timestamp_value = row.get("timestamp")
    try:
        if timestamp_value is None:
            raise ValueError("missing durable event timestamp")
        timestamp = float(timestamp_value)
    except (TypeError, ValueError):
        created_at = str(row.get("created_at") or "")
        try:
            from datetime import datetime

            timestamp = datetime.fromisoformat(created_at).timestamp()
        except (TypeError, ValueError):
            timestamp = 0.0
    return {
        "type": str(row.get("type") or row.get("event_type") or ""),
        "source": str(row.get("source") or ""),
        "payload": payload,
        "id": event_id,
        "timestamp": timestamp,
        "correlation_id": row.get("correlation_id"),
    }


async def trajectory_events(
    state: AppState,
    *,
    limit: int = 200,
    event_type: EventType | None = None,
    correlation_id: str | None = None,
    since: float | None = None,
    until: float | None = None,
) -> list[dict[str, Any]]:
    """Merge durable and live trajectory evidence, preferring the live copy."""
    requested = max(1, min(int(limit), 2000))
    live = state.trajectory(
        limit=requested,
        event_type=event_type,
        correlation_id=correlation_id,
        since=since,
        until=until,
    )
    durable: list[dict[str, Any]] = []
    memory = getattr(state, "memory", None)
    recent = getattr(memory, "recent_events", None)
    if callable(recent):
        try:
            durable_limit = (
                2000
                if since is not None or until is not None
                else min(2000, requested + len(live))
            )
            rows = await recent(
                limit=durable_limit,
                correlation_id=correlation_id,
                event_types=[event_type.value] if event_type is not None else None,
            )
            for raw in rows or []:
                if not isinstance(raw, dict):
                    continue
                event = _durable_trajectory_event(raw)
                timestamp = float(event["timestamp"])
                if since is not None and timestamp < since:
                    continue
                if until is not None and timestamp > until:
                    continue
                durable.append(event)
        except Exception as exc:  # noqa: BLE001 - durable replay degrades to live history
            log.warning("trajectory.durable_read_failed", error=str(exc)[:200])

    # Durable rows are inserted first; a matching live event then replaces the
    # DB projection with the original in-memory payload while retaining one row.
    merged: dict[str, dict[str, Any]] = {}
    for event in [*durable, *live]:
        event_id = str(event.get("id") or "")
        if not event_id:
            event_id = (
                f"anonymous:{event.get('timestamp')}:{event.get('type')}:"
                f"{event.get('source')}:{event.get('correlation_id')}"
            )
        merged[event_id] = event
    events = sorted(
        merged.values(),
        key=lambda event: (
            float(event.get("timestamp") or 0.0),
            str(event.get("id") or ""),
        ),
    )
    return events[-requested:]


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

    async def require_control_auth(request: Request) -> None:
        await require_auth(request)
        client_host = request.client.host if request.client else None
        if not is_cross_origin_browser_request(request.headers, client_host=client_host):
            return
        # A real bearer explicitly authorizes cross-origin/native control use.
        # Loopback trust alone must never authorize a sandboxed preview
        # (Origin:null) or a drive-by browser request.
        bearer_ok = check_auth(
            state.settings,
            authorization=request.headers.get("authorization"),
            client_host=None,
        )
        if not bearer_ok:
            raise HTTPException(status_code=403, detail="cross-origin control request denied")

    auth = Depends(require_control_auth)
    project_auth = Depends(require_auth)
    empty_body: Any = Body(default_factory=dict)
    any_body: Any = Body(default=None)

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

    @router.get("/bench/golden", dependencies=[auth])
    async def _bench_golden() -> dict[str, Any]:
        return await golden_bench_payload(state)

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

    @router.get("/cli-providers", dependencies=[auth])
    async def _cli_providers() -> dict[str, Any]:
        return await cli_providers_payload(state)

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
                full_app=_coerce_bool(body.get("full_app", False)),
                moa_advisors=_normalize_moa_advisors(
                    body.get("moa_advisors"),
                    no_claude=bool(getattr(state.settings, "no_claude", False)),
                ),
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
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid slug") from None

    @router.get("/projects/cleanup", dependencies=[auth])
    async def _cleanup_report() -> dict[str, Any]:
        from skyn3t.studio.cleanup import scan as cleanup_scan
        wt = state.settings.projects_dir.parent / ".skyn3t_worktrees"
        active = sorted(
            {
                getattr(r, "slug", "")
                for r in state.builds.values()
                if getattr(r, "status", "") == "running" and getattr(r, "slug", "")
            }
            | {Path(claim).name for claim in _REVERIFYING_PROJECTS}
        )
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
        active = sorted(
            {
                getattr(r, "slug", "")
                for r in state.builds.values()
                if getattr(r, "status", "") == "running" and getattr(r, "slug", "")
            }
            | {Path(claim).name for claim in _REVERIFYING_PROJECTS}
        )
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
    # catch-all treats these endpoint names as project file paths.
    @router.get("/projects/{slug}/prompts", dependencies=[auth])
    async def _project_prompts(slug: str) -> dict[str, Any]:
        try:
            return await get_project_prompts(state, slug)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid project") from None

    @router.get("/projects/{slug}/product", dependencies=[auth])
    async def _project_product(slug: str) -> dict[str, Any]:
        from skyn3t.studio.product_spec import ProductSpecPersistenceError

        try:
            return await get_project_product(state, slug)
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid project") from None
        except ProductSpecPersistenceError:
            raise HTTPException(
                status_code=500,
                detail="product contract could not be read",
            ) from None

    @router.post("/projects/{slug}/feedback", dependencies=[auth])
    async def _project_feedback(
        slug: str,
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        from skyn3t.intelligence.human_feedback import (
            HumanFeedbackPersistenceError,
            HumanFeedbackValidationError,
        )

        try:
            return await capture_project_human_feedback(
                state,
                slug,
                feedback=body.get("feedback"),
                category=body.get("category"),
                context=body.get("context"),
                rating=body.get("rating"),
            )
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except HumanFeedbackValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except HumanFeedbackPersistenceError:
            raise HTTPException(status_code=503, detail="feedback learning store is unavailable") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @router.patch("/projects/{slug}/product", dependencies=[auth])
    async def _patch_project_product(
        slug: str,
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        from skyn3t.studio.product_spec import (
            ProductSpecConflictError,
            ProductSpecPersistenceError,
            ProductSpecValidationError,
        )

        try:
            raw_base_version = body.get("base_version")
            raw_patch = body.get("patch")
            if isinstance(raw_base_version, bool) or not isinstance(raw_base_version, int):
                raise ValueError("base_version must be an integer")
            if not isinstance(raw_patch, dict) or not all(
                isinstance(key, str) for key in raw_patch
            ):
                raise ValueError("patch must be an object with string keys")
            patch: dict[str, Any] = dict(raw_patch)
            return await patch_project_product(
                state,
                slug,
                base_version=raw_base_version,
                patch=patch,
                reason=str(body.get("reason") or ""),
            )
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except ProductSpecUnavailableError:
            raise HTTPException(status_code=404, detail="product contract not found") from None
        except ProductSpecConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "stale_product_version",
                    "requested_version": exc.expected_version,
                    "current_version": exc.actual_version,
                },
            ) from None
        except (ValueError, ProductSpecValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except ProductSpecPersistenceError:
            raise HTTPException(
                status_code=500,
                detail="product contract could not be saved",
            ) from None

    @router.post("/projects/{slug}/product/research", dependencies=[auth])
    async def _research_project_product(
        slug: str,
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        from skyn3t.studio.product_spec import (
            ProductSpecConflictError,
            ProductSpecPersistenceError,
            ProductSpecValidationError,
        )

        try:
            raw_base_version = body.get("base_version")
            if isinstance(raw_base_version, bool) or not isinstance(raw_base_version, int):
                raise ValueError("base_version must be an integer")
            return await research_project_product(
                state,
                slug,
                base_version=raw_base_version,
                force_refresh=bool(body.get("force_refresh", True)),
            )
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except ProductSpecUnavailableError:
            raise HTTPException(status_code=404, detail="product contract not found") from None
        except ProductSpecConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "stale_product_version",
                    "requested_version": exc.expected_version,
                    "current_version": exc.actual_version,
                },
            ) from None
        except (ValueError, ProductSpecValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except ProductSpecPersistenceError:
            raise HTTPException(
                status_code=500,
                detail="product research could not be saved",
            ) from None

    @router.post("/projects/{slug}/reverify", dependencies=[auth])
    async def _project_reverify(slug: str) -> dict[str, Any]:
        try:
            return await reverify_project(state, slug)
        except ProjectReverifyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid project") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail="failed to persist local re-verification",
            ) from exc

    @router.get("/projects/{slug}/visual-quality", dependencies=[auth])
    async def _visual_quality(slug: str) -> dict[str, Any]:
        try:
            return await get_visual_quality(state, slug)
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid project") from None

    @router.post("/projects/{slug}/visual-quality/run", dependencies=[auth])
    async def _start_visual_quality(slug: str) -> dict[str, Any]:
        try:
            return await start_visual_quality(state, slug)
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid project") from None

    @router.get(
        "/projects/{slug}/visual-quality/runs/{run_id}/artifacts/{artifact_path:path}",
        dependencies=[auth],
    )
    async def _visual_quality_artifact(
        slug: str,
        run_id: str,
        artifact_path: str,
    ) -> Any:
        try:
            artifact = visual_quality_artifact(state, slug, run_id, artifact_path)
            return FileResponse(str(artifact), headers={"Cache-Control": "no-store"})
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="visual quality artifact not found") from None
    @router.post("/projects/{slug}/visual-editor/inspect", dependencies=[auth])
    async def _visual_editor_inspect(
        slug: str,
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        from skyn3t.studio.visual_editor import VisualEditorError

        signature = body.get("signature")
        if not isinstance(signature, dict):
            raise HTTPException(status_code=422, detail="signature is required")
        try:
            limit = max(1, min(int(body.get("limit", 20)), 100))
            return await visual_editor_inspect(
                state,
                slug,
                signature,
                limit=limit,
            )
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except (ValueError, VisualEditorError) as exc:
            detail = exc.to_dict() if isinstance(exc, VisualEditorError) else str(exc)
            raise HTTPException(status_code=422, detail=detail) from None

    @router.get("/projects/{slug}/visual-editor/style", dependencies=[auth])
    async def _visual_editor_style(slug: str) -> dict[str, Any]:
        from skyn3t.studio.visual_editor import VisualEditorError

        try:
            return await visual_editor_style(state, slug)
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except VisualEditorError as exc:
            raise HTTPException(status_code=422, detail=exc.to_dict()) from None

    @router.post("/projects/{slug}/visual-editor/edit", dependencies=[auth])
    async def _visual_editor_edit(
        slug: str,
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        from skyn3t.studio.visual_editor import (
            AmbiguousSourceError,
            StaleSourceError,
            VisualEditorError,
        )

        try:
            return await visual_editor_apply(state, slug, body)
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except (StaleSourceError, AmbiguousSourceError) as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from None
        except (ValueError, VisualEditorError) as exc:
            detail = exc.to_dict() if isinstance(exc, VisualEditorError) else str(exc)
            raise HTTPException(status_code=422, detail=detail) from None

    @router.post("/projects/{slug}/annotations/improve", dependencies=[auth])
    async def _annotations_improve(slug: str, body: Any = any_body) -> dict[str, Any]:
        from skyn3t.studio.visual_editor import VisualEditorError

        try:
            return await annotations_improve(state, slug, body)
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="project not found") from None
        except (ValueError, VisualEditorError) as exc:
            detail = exc.to_dict() if isinstance(exc, VisualEditorError) else str(exc)
            raise HTTPException(status_code=422, detail=detail) from None

    @router.get("/projects/{slug}/{path:path}", dependencies=[project_auth])
    async def _project_file(slug: str, path: str) -> Any:
        try:
            resolved = resolve_project_file(state, slug, path)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid path") from None
        except (FileNotFoundError, PermissionError, ProjectNotDeliveredError):
            raise HTTPException(status_code=404, detail="not found") from None
        return project_file_response(resolved, slug)

    @router.get("/project-previews/{cap}/{slug}/{path:path}")
    async def _project_preview_file(cap: str, slug: str, path: str) -> Any:
        # Return 404 for both bad capabilities and missing files so this route
        # never becomes an oracle for project names or preview contents.
        if not _valid_project_preview_capability(state, slug, cap):
            raise HTTPException(status_code=404, detail="not found")
        try:
            resolved = resolve_project_file(state, slug, path)
        except (ValueError, FileNotFoundError, PermissionError, ProjectNotDeliveredError):
            raise HTTPException(status_code=404, detail="not found") from None
        prefix = (
            f"/api/project-previews/{quote(cap, safe='')}/"
            f"{quote(slug, safe='')}/"
        )
        return project_file_response(resolved, slug, route_prefix=prefix)

    @router.get("/cortex/proposals", dependencies=[auth])
    async def _cortex_proposals(status: str = Query(default="")) -> dict[str, Any]:
        return await list_proposals(state, status=status)

    @router.get("/cortex/effects", dependencies=[auth])
    async def _cortex_effects() -> dict[str, Any]:
        return await cortex_effects_payload(state)

    @router.get("/cortex/candidates", dependencies=[auth])
    async def _cortex_candidates(
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        return await cortex_candidates_payload(state, limit=limit)

    @router.post("/cortex/candidates", dependencies=[auth])
    async def _run_cortex_candidate(
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            return await run_cortex_candidate_payload(
                state,
                goal=str(body.get("goal") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/cortex/autopilot", dependencies=[auth])
    async def _cortex_autopilot() -> dict[str, Any]:
        return await cortex_autopilot_payload(state)

    @router.post("/cortex/autopilot", dependencies=[auth])
    async def _set_cortex_autopilot(
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            enabled = _coerce_bool(body.get("enabled", body.get("on", False)))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await set_cortex_autopilot(state, enabled=enabled)

    @router.post("/cortex/autopilot/incidents", dependencies=[auth])
    async def _cortex_autopilot_incident(
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        summary = str(body.get("summary") or "").strip()
        if not summary:
            raise HTTPException(status_code=422, detail="incident summary is required")
        return await report_cortex_autopilot_incident(
            state,
            scope=str(body.get("scope") or "skyn3t"),
            category=str(body.get("category") or "unknown"),
            summary=summary,
            evidence=str(body.get("evidence") or ""),
        )

    @router.post("/cortex/autopilot/tick", dependencies=[auth])
    async def _cortex_autopilot_tick() -> dict[str, Any]:
        return await tick_cortex_autopilot(state)
    @router.get("/cortex/graphs", dependencies=[auth])
    async def _cortex_graphs(
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        return await cortex_graph_runs_payload(state, limit=limit)

    @router.post("/cortex/graphs/{run_id}/rerun", dependencies=[auth])
    async def _cortex_graph_rerun(
        run_id: str,
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            return await rerun_cortex_graph_payload(
                state,
                source_run_id=run_id,
                from_node_id=str(body.get("from_node_id") or ""),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="graph run not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/cortex/graph-reviews", dependencies=[auth])
    async def _cortex_graph_reviews(
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        return await cortex_graph_reviews_payload(state, limit=limit)

    @router.post("/cortex/graph-reviews/{comparison_id}/decide", dependencies=[auth])
    async def _cortex_graph_review_decide(
        comparison_id: str,
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            return await decide_cortex_graph_review_payload(
                state,
                comparison_id=comparison_id,
                decision=str(body.get("decision") or ""),
                note=str(body.get("note") or ""),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="graph comparison not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/cortex/graph-reviews/{comparison_id}/build", dependencies=[auth])
    async def _cortex_graph_review_build(
        comparison_id: str,
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            return await queue_cortex_graph_review_build_payload(
                state,
                comparison_id=comparison_id,
                brief=str(body.get("brief") or ""),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="graph comparison not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

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
        try:
            return await set_llm_backend(state, str(body.get("backend", "auto")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
                vision_model=str(body["vision_model"]) if "vision_model" in body else None,
                codegen_model_slot=str(body["codegen_model_slot"]) if "codegen_model_slot" in body else None,
                repair_model_slot=str(body["repair_model_slot"]) if "repair_model_slot" in body else None,
                free_only=free_only,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/settings/github", dependencies=[auth])
    async def _set_github(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        return await set_github_token(state, str(body.get("token", body.get("key", ""))))

    @router.post("/settings/replicate", dependencies=[auth])
    async def _set_replicate(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        token = body.get("token", body.get("key"))
        model = body.get("model")
        try:
            return await set_replicate_token(
                state,
                None if token is None else str(token),
                model=None if model is None else str(model),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/settings/deploy", dependencies=[auth])
    async def _deploy_settings() -> dict[str, Any]:
        return await deploy_settings_payload(state)

    @router.post("/settings/deploy/credential", dependencies=[auth])
    async def _set_deploy_credential(
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            return await set_deploy_credential(
                state,
                str(body.get("provider", "")),
                str(body.get("token", body.get("key", ""))),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/settings/deploy/allow_remote", dependencies=[auth])
    async def _set_allow_remote_deploy(
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            enabled = _coerce_bool(body.get("enabled", body.get("on", False)))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await set_allow_remote_deploy(state, enabled)

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

    @router.post("/settings/lab_autonomy", dependencies=[auth])
    async def _set_lab_autonomy(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            enabled = _coerce_bool(body.get("enabled", body.get("on", False)))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await set_lab_autonomy(state, enabled)

    @router.post("/settings/cortex_candidates", dependencies=[auth])
    async def _set_cortex_candidates(
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            return await set_cortex_candidate_policy(
                state,
                enabled=_coerce_bool(
                    body.get(
                        "enabled",
                        getattr(
                            state.settings,
                            "cortex_candidates_enabled",
                            True,
                        ),
                    )
                ),
                auto_merge=_coerce_bool(
                    body.get(
                        "auto_merge",
                        getattr(
                            state.settings,
                            "cortex_candidate_auto_merge",
                            False,
                        ),
                    )
                ),
                merge_strategy=str(
                    body.get(
                        "merge_strategy",
                        getattr(
                            state.settings,
                            "cortex_candidate_merge_strategy",
                            "ff-only",
                        ),
                    )
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/settings/similarity_research", dependencies=[auth])
    async def _set_similarity_research(
        body: dict[str, Any] = empty_body,
    ) -> dict[str, Any]:
        try:
            enabled = _coerce_bool(body.get("enabled", body.get("on", False)))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await set_similarity_research(state, enabled)

    @router.get("/lab/toolchain", dependencies=[auth])
    async def _lab_toolchain(stack: str = "") -> dict[str, Any]:
        return await lab_toolchain_payload(state, stack=stack)

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
                full_app=_coerce_bool(body.get("full_app", False)),
                moa_advisors=_normalize_moa_advisors(
                    body.get("moa_advisors"),
                    no_claude=bool(getattr(state.settings, "no_claude", False)),
                ),
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
            return await start_serve_project(state, str(body.get("slug", "")))
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
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

    @router.get("/studio/serve/history", dependencies=[auth])
    async def _serve_history(slug: str) -> dict[str, Any]:
        try:
            return await serve_history(state, slug)
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid slug") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None
    @router.get("/studio/deploy/plan", dependencies=[auth])
    async def _deploy_plan(slug: str, target: str = "") -> dict[str, Any]:
        try:
            return await deploy_plan_project(state, slug, target=target)
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
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
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except DeployPreflightError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid slug") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None

    @router.post("/studio/deploy/rollback", dependencies=[auth])
    async def _deploy_rollback(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        raw_index = body.get("deployment_index")
        try:
            index = None if raw_index in (None, "") else int(raw_index)
            return await rollback_project_deployment(
                state,
                str(body.get("slug", "")),
                reason=str(body.get("reason", "")),
                deployment_index=index,
            )
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid rollback selection") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None

    @router.post("/studio/improve", dependencies=[auth])
    async def _improve(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        try:
            return await improve_project(
                state, str(body.get("slug", "")), str(body.get("goal", "")))
        except ProjectNotDeliveredError:
            raise HTTPException(status_code=409, detail="project build is not complete") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="slug and goal are required") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found") from None

    @router.post("/studio/fanout", dependencies=[auth])
    async def _fanout(body: dict[str, Any] = empty_body) -> dict[str, Any]:
        stacks = body.get("stacks")
        if isinstance(stacks, str):
            stacks = [s.strip() for s in stacks.split(",") if s.strip()]
        raw_references = body.get("reference_images")
        references = raw_references if isinstance(raw_references, list) else []
        try:
            return await fanout_project(
                state,
                str(body.get("brief", "")),
                stacks or [],
                build_profile=str(body.get("build_profile", "cheap_learned")),
                model_override=str(body.get("model_override", "")),
                full_app=_coerce_bool(body.get("full_app", False)),
                reference_image=str(body.get("reference_image", "")),
                reference_images=[str(item) for item in references],
            )
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

    @router.post("/skills/{slug}/promote", dependencies=[auth])
    async def _promote_external_skill(slug: str) -> dict[str, Any]:
        try:
            return await promote_external_skill(state, slug)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/skills/promote-ready", dependencies=[auth])
    async def _promote_all_ready_skills() -> dict[str, Any]:
        try:
            return await promote_all_ready_skills(state)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
            activate = body.get("activate", False)
            if not isinstance(activate, bool):
                raise ValueError("catalog activate must be a boolean")
            return await import_agent_catalog(
                state,
                str(body.get("path", "")),
                limit=int(body.get("limit", 100) or 100),
                activate=activate,
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
        events = await trajectory_events(
            state,
            limit=limit,
            event_type=et,
            correlation_id=correlation_id or None,
            since=since,
            until=until,
        )
        return {"events": events, "count": len(events)}

    return router
