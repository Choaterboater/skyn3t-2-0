# skyn3t/studio/improve.py
"""Headless 'improve an existing project' engine (Spec 3, Layer A).

Loads an already-delivered project, runs the code_improver toward a goal in an
isolated worktree, verifies with proof_run, and delivers the change back —
never leaving a partial result. Emits IMPROVE_* events for the cockpit."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path
from threading import Lock, RLock
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the in-process lock
    fcntl = None  # type: ignore[assignment]

import structlog

from skyn3t.agents.stack_detector import StackDetector
from skyn3t.config.settings import get_settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus, EventType
from skyn3t.rag.repo_map import build_repo_context_pack
from skyn3t.studio.design_tokens import read_design_md
from skyn3t.studio.grounding_check import check_grounding
from skyn3t.studio.layout_profiles import (
    is_valid_profile_payload,
    layout_contract_block,
    profile_from_payload,
)
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.product_spec import (
    PRODUCT_SPEC_RELATIVE_PATH,
    ProductSpecV1,
    product_contract_prompt_block,
)
from skyn3t.studio.proof_run import apply_deterministic_repairs, proof_run
from skyn3t.worktree import (
    cleanup_worktree,
    create_worktree,
    deliverable_tree_snapshot,
    delivery_staging_dir,
    list_files,
    merge_back,
    source_tree_snapshot,
)

_log = structlog.get_logger(__name__)
_PROJECT_LOCKS_GUARD = RLock()
_PROJECT_LOCKS: dict[str, Lock] = {}
_IMPROVE_SNAPSHOT_MAX_FILES = 20_000
_IMPROVE_SNAPSHOT_MAX_BYTES = 268_435_456
_IMPROVE_SNAPSHOT_MAX_ENTRIES = 40_000
_CONTROL_FILE_MAX_BYTES = 16_777_216
_MANIFEST_RELATIVE_PATH = Path("skyn3t_manifest.json")
_OBSERVABILITY_RELATIVE_PATH = Path("skyn3t-observability.json")
_CONTROL_RELATIVE_PATHS = (
    PRODUCT_SPEC_RELATIVE_PATH,
    _MANIFEST_RELATIVE_PATH,
    _OBSERVABILITY_RELATIVE_PATH,
)


def _grounding_fix_hints(project_dir: Path, stack: str) -> list[str]:
    """Advisory grounding-lint warnings for the delivered project, phrased
    as fix hints for the improver. Never raises — a lint failure must not
    break an improve run."""
    try:
        result = check_grounding(project_dir, stack)
    except Exception as exc:  # noqa: BLE001
        _log.warning("improve.grounding_check_failed", error=str(exc))
        return []
    return [str(w) for w in (result.get("warnings") or [])][:10]


def _project_lock(project_dir: Path) -> Lock:
    """Return a process-wide project lock shared across event loops."""
    key = str(project_dir.resolve())
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            _PROJECT_LOCKS[key] = lock
        return lock


async def _acquire_thread_lock(lock: Lock) -> None:
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.05)


@dataclass(slots=True)
class _InterprocessProjectLock:
    path: Path
    handle: Any = None

    def try_acquire(self) -> bool:
        if self.handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            self.handle = os.fdopen(descriptor, "a+b")
        if fcntl is None:
            return True
        try:
            fcntl.flock(  # type: ignore[attr-defined]
                self.handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )
            return True
        except BlockingIOError:
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            self.handle.close()
            self.handle = None


async def _acquire_interprocess_lock(
    project_dir: Path,
    projects_root: Path,
) -> _InterprocessProjectLock:
    digest = hashlib.sha256(str(project_dir.resolve()).encode()).hexdigest()
    lock = _InterprocessProjectLock(
        projects_root.parent / ".skyn3t_project_locks" / f"{digest}.lock"
    )
    try:
        while not lock.try_acquire():
            await asyncio.sleep(0.05)
    except BaseException:
        lock.release()
        raise
    return lock


async def _proof_run_without_blocking(
    project_dir: str,
    **kwargs: Any,
) -> Any:
    """Run synchronous proof off-loop and never clean its tree underneath it."""
    worker = asyncio.create_task(
        asyncio.to_thread(proof_run, project_dir, **kwargs)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling ``to_thread`` does not stop its OS thread. Wait for the
        # verifier to release files/processes before Improve's finally block
        # removes the worktree and unlocks the delivered project.
        try:
            await worker
        except Exception as exc:  # noqa: BLE001 - preserve original cancellation
            _log.warning(
                "improve.proof_failed_during_cancellation",
                error=str(exc),
            )
        raise


def _file_identity(path: Path) -> tuple[str, int, str]:
    """Return a bounded exact identity for a runner-owned control file."""
    if path.is_symlink():
        return ("unsafe_alias", 0, "")
    try:
        if not path.exists():
            return ("missing", 0, "")
        if not path.is_file():
            return ("not_file", 0, "")
        before = path.stat()
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                size += len(block)
                if size > _CONTROL_FILE_MAX_BYTES:
                    return ("oversized", size, "")
                digest.update(block)
        after = path.stat()
    except OSError:
        return ("unreadable", 0, "")
    stable = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if not stable:
        return ("unstable", size, "")
    return ("file", size, digest.hexdigest())


def _source_snapshot(path: Path) -> dict[str, Any]:
    return source_tree_snapshot(
        path,
        max_files=_IMPROVE_SNAPSHOT_MAX_FILES,
        max_bytes=_IMPROVE_SNAPSHOT_MAX_BYTES,
        max_entries=_IMPROVE_SNAPSHOT_MAX_ENTRIES,
    )


def _delivery_snapshot(path: Path) -> dict[str, Any]:
    return deliverable_tree_snapshot(
        path,
        max_files=_IMPROVE_SNAPSHOT_MAX_FILES,
        max_bytes=_IMPROVE_SNAPSHOT_MAX_BYTES,
        max_entries=_IMPROVE_SNAPSHOT_MAX_ENTRIES,
    )


def _same_source_snapshot(
    before: dict[str, Any],
    after: dict[str, Any],
) -> bool:
    """Compare valid canonical source snapshots, including their file set."""
    if not before.get("valid") or not after.get("valid"):
        return False
    return (
        before.get("algorithm"),
        before.get("sha256"),
        tuple(before.get("files") or ()),
        before.get("file_count"),
        before.get("byte_count"),
    ) == (
        after.get("algorithm"),
        after.get("sha256"),
        tuple(after.get("files") or ()),
        after.get("file_count"),
        after.get("byte_count"),
    )


def _control_identities(root: Path) -> dict[Path, tuple[str, int, str]]:
    return {
        relative: _file_identity(root / relative)
        for relative in _CONTROL_RELATIVE_PATHS
    }


def _restore_control_files(
    source: Path,
    destination: Path,
    expected: dict[Path, tuple[str, int, str]],
) -> None:
    """Copy authoritative control files, or preserve their exact absence."""
    if _control_identities(source) != expected:
        raise RuntimeError("project control metadata changed during delivery")
    for relative, identity in expected.items():
        source_path = source / relative
        destination_path = destination / relative
        if identity[0] == "missing":
            if destination_path.is_dir() and not destination_path.is_symlink():
                shutil.rmtree(destination_path)
            else:
                destination_path.unlink(missing_ok=True)
            continue
        if identity[0] != "file":
            raise RuntimeError(f"unsafe project control file: {relative}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.is_dir() and not destination_path.is_symlink():
            shutil.rmtree(destination_path)
        elif destination_path.is_symlink():
            destination_path.unlink()
        shutil.copy2(source_path, destination_path)
        if _file_identity(destination_path) != identity:
            raise RuntimeError(f"control file copy was incomplete: {relative}")


def _copy_directory_metadata(source: Path, destination: Path) -> None:
    """Preserve candidate/new and authoritative/existing directory metadata."""
    directories: list[Path] = []
    for current, dirnames, _filenames in os.walk(
        destination,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        dirnames[:] = sorted(
            name for name in dirnames
            if name.casefold() != ".git"
            and not (current_path / name).is_symlink()
        )
        directories.append(current_path)
    # Children first, root last: later control-file writes cannot accidentally
    # leave an existing private project root with a permissive default mode.
    for destination_dir in sorted(
        directories,
        key=lambda path: len(path.relative_to(destination).parts),
        reverse=True,
    ):
        relative = destination_dir.relative_to(destination)
        source_dir = source / relative
        if source_dir.is_dir() and not source_dir.is_symlink():
            shutil.copystat(
                source_dir,
                destination_dir,
                follow_symlinks=False,
            )


def _snapshot_file_paths(snapshot: dict[str, Any]) -> list[Path]:
    """Return validated relative regular-file paths from a valid snapshot."""
    if not snapshot.get("valid"):
        raise RuntimeError("cannot transact an invalid tree snapshot")
    paths: list[Path] = []
    for raw in snapshot.get("files") or ():
        relative = Path(str(raw))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RuntimeError(f"unsafe snapshot path: {raw!r}")
        paths.append(relative)
    return paths


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


def _open_confined_directory(root: Path, parts: tuple[str, ...]) -> int:
    """Open a project-relative directory without following swapped ancestors."""
    flags = _directory_open_flags()
    current = os.open(root, flags)
    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise OSError("unsafe relative directory")
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _ensure_confined_directory(
    root: Path,
    parts: tuple[str, ...],
    *,
    template_root: Path | None = None,
) -> int:
    """Open or create a confined directory chain and return its descriptor."""
    flags = _directory_open_flags()
    current = os.open(root, flags)
    traversed: list[str] = []
    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise OSError("unsafe relative directory")
            traversed.append(part)
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                mode = 0o755
                if template_root is not None:
                    template = template_root.joinpath(*traversed)
                    template_stat = template.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(template_stat.st_mode):
                        raise OSError(
                            "directory template is not a directory"
                        ) from None
                    mode = stat.S_IMODE(template_stat.st_mode)
                try:
                    os.mkdir(part, mode, dir_fd=current)
                except FileExistsError:
                    # A concurrent creator won. The no-follow open below decides
                    # whether it is still a safe directory.
                    pass
                child = os.open(part, flags, dir_fd=current)
                if template_root is not None:
                    os.fchmod(child, mode)  # type: ignore[attr-defined]
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _prune_empty_snapshot_parents(root: Path, paths: list[Path]) -> None:
    """Best-effort removal of now-empty authored directories, deepest first."""
    parents = {
        parent
        for relative in paths
        for parent in relative.parents
        if parent != Path(".")
    }
    for relative in sorted(parents, key=lambda value: len(value.parts), reverse=True):
        try:
            (root / relative).rmdir()
        except OSError:
            # Ignored runtime state, a concurrent writer, or a still-needed
            # sibling keeps the directory alive.
            continue


def _move_snapshot_files(
    source_root: Path,
    recovery_root: Path,
    snapshot: dict[str, Any],
) -> None:
    """Atomically journal every snapshotted file without overwriting recovery.

    Moving the live pathname before installing a candidate closes the destructive
    check/use gap in copy-over delivery: an edit that wins the race is moved into
    the journal rather than overwritten. The journal is compared with the
    expected snapshot before candidate installation continues.
    """
    paths = _snapshot_file_paths(snapshot)
    recovery_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    supports_dir_fd = (
        os.open in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
    )
    for relative in paths:
        if supports_dir_fd:
            source_parent = _open_confined_directory(
                source_root,
                relative.parts[:-1],
            )
            recovery_parent = _ensure_confined_directory(
                recovery_root,
                relative.parts[:-1],
                template_root=source_root,
            )
            try:
                os.rename(
                    relative.name,
                    relative.name,
                    src_dir_fd=source_parent,
                    dst_dir_fd=recovery_parent,
                )
            finally:
                os.close(source_parent)
                os.close(recovery_parent)
        else:  # pragma: no cover - compatibility fallback for Windows
            source = (source_root / relative).resolve(strict=True)
            if not source.is_relative_to(source_root.resolve()):
                raise OSError("source path escaped transaction root")
            destination = recovery_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            os.rename(source, destination)
    _prune_empty_snapshot_parents(source_root, paths)


def _link_snapshot_files(
    source_root: Path,
    destination_root: Path,
    snapshot: dict[str, Any],
) -> None:
    """Install a snapshot with atomic no-replace hard links.

    Candidate files are fully materialized in a same-filesystem sibling stage.
    ``link`` fails when a concurrent writer recreates a destination path, so the
    writer's file is preserved and the transaction can recover instead of
    silently overwriting it.
    """
    paths = _snapshot_file_paths(snapshot)
    supports_dir_fd = (
        os.open in os.supports_dir_fd
        and os.link in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
    )
    for relative in paths:
        if supports_dir_fd:
            source_parent = _open_confined_directory(
                source_root,
                relative.parts[:-1],
            )
            destination_parent = _ensure_confined_directory(
                destination_root,
                relative.parts[:-1],
                template_root=source_root,
            )
            try:
                source_stat = os.stat(
                    relative.name,
                    dir_fd=source_parent,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(source_stat.st_mode):
                    raise OSError("transaction source is not a regular file")
                os.link(
                    relative.name,
                    relative.name,
                    src_dir_fd=source_parent,
                    dst_dir_fd=destination_parent,
                    follow_symlinks=False,
                )
            finally:
                os.close(source_parent)
                os.close(destination_parent)
        else:  # pragma: no cover - compatibility fallback for Windows
            source = (source_root / relative).resolve(strict=True)
            if not source.is_relative_to(source_root.resolve()):
                raise OSError("source path escaped transaction root")
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, destination, follow_symlinks=False)


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
        payload = asdict(self)
        if self.status == "failed":
            error = (
                self.detail.get("error")
                or self.detail.get("improver_error")
                or self.detail.get("delivery_blocked")
            )
            if error:
                # Keep the structured detail while making every existing GUI
                # consumer that renders ``payload.error`` show a useful reason.
                payload["error"] = str(error)
        return payload


class ImproveEngine:
    """Improve an existing project toward a goal. Mirrors StudioRunner's deps."""

    def __init__(self, event_bus: EventBus, orchestrator: Any, *,
                 settings: Any | None = None, memory: Any | None = None,
                 skills: Any | None = None, rag: Any | None = None,
                 llm_client: Any | None = None,
                 routing_snapshot: dict[str, Any] | None = None,
                 record_history: bool = True) -> None:
        self.event_bus = event_bus
        self.orchestrator = orchestrator
        self.settings = settings or get_settings()
        self.memory = memory
        self.skills = skills
        self.rag = rag
        self.llm_client = llm_client
        self.routing_snapshot = deepcopy(routing_snapshot)
        self.record_history = bool(record_history)

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

    def _routing_client(self) -> Any | None:
        if self.llm_client is not None:
            return self.llm_client
        try:
            agents = getattr(self.orchestrator, "agents", {})
            values = agents.values() if isinstance(agents, dict) else ()
            for agent in values:
                client = getattr(agent, "llm", None)
                if callable(getattr(client, "build_routing_snapshot", None)):
                    return client
        except Exception:  # noqa: BLE001 - routing discovery is best-effort
            pass
        return None

    def _capture_routing_snapshot(self, client: Any | None) -> dict[str, Any]:
        if isinstance(self.routing_snapshot, dict):
            return deepcopy(self.routing_snapshot)
        active_snapshot = getattr(
            client,
            "active_build_routing_snapshot",
            None,
        )
        if callable(active_snapshot):
            return deepcopy(active_snapshot())
        snapshot = getattr(client, "build_routing_snapshot", None)
        if callable(snapshot):
            return deepcopy(snapshot())
        # Compatibility for narrow settings/test doubles. The exact explicit
        # codegen pin is still frozen even when no shared LLM client is exposed.
        requested_backend = str(
            getattr(self.settings, "llm_backend", "auto") or "auto"
        ).strip().lower()
        provider = str(
            getattr(self.settings, "codegen_cli_provider", "") or ""
        ).strip().lower()
        model = str(
            getattr(self.settings, "codegen_cli_model", "") or ""
        ).strip()
        requested_codegen_backend = (
            f"{provider}_cli" if provider else requested_backend
        )
        return {
            "requested_backend": requested_backend,
            "effective_backend": requested_backend,
            "codegen": {
                "source": "codegen_cli_pin" if provider else "global_backend",
                "requested_backend": requested_codegen_backend,
                "effective_backend": requested_codegen_backend,
                "requested_model": model,
                "effective_model": model or (
                    f"{provider}-cli:default" if provider else ""
                ),
            },
        }

    @staticmethod
    def _routing_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            key: deepcopy(snapshot[key])
            for key in (
                "requested_backend",
                "effective_backend",
                "requested_model",
                "effective_model",
                "submission",
                "codegen",
            )
            if key in snapshot
        }
        return summary

    @staticmethod
    def _frozen_codegen_pin(snapshot: dict[str, Any]) -> tuple[str, str]:
        codegen = snapshot.get("codegen")
        if not isinstance(codegen, dict):
            return "", ""
        requested_backend = str(
            codegen.get("requested_backend") or ""
        ).strip().lower()
        provider = (
            requested_backend.removesuffix("_cli")
            if codegen.get("source") == "codegen_cli_pin"
            and requested_backend.endswith("_cli")
            else ""
        )
        model = str(codegen.get("requested_model") or "").strip()
        return provider, model

    async def improve(self, project: str, goal: str, *,
                      correlation_id: str | None = None) -> ImproveOutcome:
        """Run with the exact provider/model route captured at submission."""
        client = self._routing_client()
        routing_snapshot = self._capture_routing_snapshot(client)
        routing_scope = getattr(client, "build_routing_scope", None)
        if callable(routing_scope):
            with routing_scope(routing_snapshot):
                return await self._improve_scoped(
                    project,
                    goal,
                    correlation_id=correlation_id,
                    routing_snapshot=routing_snapshot,
                )
        return await self._improve_scoped(
            project,
            goal,
            correlation_id=correlation_id,
            routing_snapshot=routing_snapshot,
        )

    async def _improve_scoped(
        self,
        project: str,
        goal: str,
        *,
        correlation_id: str | None,
        routing_snapshot: dict[str, Any],
    ) -> ImproveOutcome:
        project_dir = self._resolve_project(project)
        cid = correlation_id or uuid.uuid4().hex
        routing_summary = self._routing_summary(routing_snapshot)
        routing_provider, routing_model = self._frozen_codegen_pin(
            routing_snapshot
        )
        improve_agentic = bool(
            getattr(self.settings, "improve_agentic", True)
        )
        improve_agentic_timeout = int(
            getattr(self.settings, "improve_agentic_timeout", 900)
        )
        manifest = BuildManifest.load(project_dir)
        slug = manifest.slug if manifest else project_dir.name
        stack = (manifest.stack if manifest and manifest.stack
                 else StackDetector.detect(project_dir))
        stored_layout_profile = (
            manifest.extra.get("layout_profile") if manifest is not None else None
        )
        layout_profile = profile_from_payload(stored_layout_profile).to_dict()

        async def _emit_failed_outcome(outcome: ImproveOutcome) -> None:
            outcome.detail.setdefault("layout_profile", dict(layout_profile))
            await self._emit(EventType.IMPROVE_FAILED, outcome.to_dict(), cid)

        from skyn3t.adapters.llm import RoutingLockError, enforce_explicit_routing_lock

        try:
            # Improve is an executable automatic edit path, so ``auto`` must
            # resolve to the local Codex CLI before it makes any model call or
            # writes a worktree. Explicit hosted backends remain available.
            enforce_explicit_routing_lock(
                self.settings,
                require_codex_for_auto=hasattr(self.settings, "llm_backend"),
            )
        except RoutingLockError as exc:
            outcome = ImproveOutcome(
                project_dir=str(project_dir),
                slug=slug,
                stack=stack,
                goal=goal,
                status="failed",
                detail={
                    "error": str(exc),
                    "delivery_blocked": "routing_lock",
                    "routing_locked": True,
                    "project_preserved": True,
                    "routing_snapshot": routing_summary,
                },
            )
            await _emit_failed_outcome(outcome)
            return outcome

        project_lock = _project_lock(project_dir)
        await _acquire_thread_lock(project_lock)
        process_lock: _InterprocessProjectLock | None = None
        wt = None
        delivery_stage_root: Path | None = None
        backup_root: Path | None = None
        delivery_mutated = False
        delivery_committed = False
        rollback_confirmed = False
        preserve_recovery_artifacts = False
        context_pack_summary: dict[str, object] = {}
        try:
            process_lock = await _acquire_interprocess_lock(
                project_dir,
                Path(self.settings.projects_dir).resolve(),
            )
            # A prior Improve may have delivered while this call waited. Reload
            # all state only after acquiring process and cross-process locks.
            manifest = BuildManifest.load(project_dir)
            slug = manifest.slug if manifest else project_dir.name
            stack = (
                manifest.stack
                if manifest and manifest.stack
                else StackDetector.detect(project_dir)
            )
            stored_layout_profile = (
                manifest.extra.get("layout_profile") if manifest is not None else None
            )
            profile = profile_from_payload(stored_layout_profile)
            layout_profile = profile.to_dict()
            has_stored_layout_profile = is_valid_profile_payload(
                stored_layout_profile
            )
            project_preimage = await asyncio.to_thread(
                _source_snapshot,
                project_dir,
            )
            project_delivery_preimage = await asyncio.to_thread(
                _delivery_snapshot,
                project_dir,
            )
            if (
                not project_preimage.get("valid")
                or not project_delivery_preimage.get("valid")
                or (project_dir / ".git").is_symlink()
            ):
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    status="failed",
                    detail={
                        "delivery_blocked": "project_snapshot_invalid",
                        "project_preserved": True,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome
            control_preimages = _control_identities(project_dir)
            contract_preimage = control_preimages[PRODUCT_SPEC_RELATIVE_PATH]
            await self._emit(
                EventType.IMPROVE_STARTED,
                {
                    "slug": slug,
                    "stack": stack,
                    "goal": goal,
                    "project_dir": str(project_dir),
                    "routing_snapshot": routing_summary,
                    "layout_profile": layout_profile,
                },
                cid,
            )
            product_spec = ProductSpecV1.load(project_dir)
            improvement_prompt = goal
            if product_spec is not None:
                improvement_prompt = (
                    "IMPROVEMENT REQUEST (apply within the current product contract):\n"
                    f"{goal}\n\n"
                    f"{product_contract_prompt_block(product_spec)}"
                )
            if has_stored_layout_profile:
                improvement_prompt = "\n\n".join((
                    improvement_prompt,
                    layout_contract_block(profile),
                ))
            wt = create_worktree(str(self.settings.projects_dir), f"improve-{slug}")
            # Seed the worktree with the existing project files.
            merge_back(str(project_dir), wt.dir, overwrite=True, clean=False)
            context_pack = await asyncio.to_thread(
                build_repo_context_pack,
                wt.dir,
                query=goal,
                product_contract_version=(
                    product_spec.version if product_spec is not None else None
                ),
                max_tokens=2000,
            )
            repo_ctx = context_pack.context
            # Anti-drift: the delivered DESIGN.md (from the project dir — the
            # source of truth, not a manifest copy) locks the build's design
            # direction into the improver's context, so styling stays
            # consistent unless the goal explicitly restyles.
            design_md = read_design_md(project_dir)
            if design_md:
                repo_ctx = (
                    "The project's locked design direction (DESIGN.md) — keep "
                    "styling consistent with it unless the goal explicitly "
                    "restyles:\n"
                    f"{design_md}\n\n{repo_ctx}"
                )
            # Semantic grounding lint: undefined design tokens and phantom
            # local imports in the DELIVERED project (the source of truth,
            # not the worktree copy) are deterministic facts the improver
            # can act on. Advisory hints only — they inform the fix, they
            # never block the run.
            grounding_hints = _grounding_fix_hints(project_dir, stack)
            if grounding_hints:
                repo_ctx = (
                    "Advisory grounding-lint findings in the current project "
                    "(fix when relevant to the goal; these are hints, not "
                    "failures):\n- " + "\n- ".join(grounding_hints)
                    + f"\n\n{repo_ctx}"
                )
            context_pack_summary = context_pack.summary()
            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "localize",
                              "repo_map_chars": len(repo_ctx),
                              "repo_context_pack": context_pack_summary}, cid)

            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "generating"}, cid)
            files_changed, improver_ok, improver_err, skipped = await self._run_improver(
                wt.dir,
                slug,
                stack,
                improvement_prompt,
                repo_ctx,
                context_pack_summary,
                routing_provider,
                routing_model,
                improve_agentic,
                improve_agentic_timeout,
                routing_summary,
                cid,
                layout_profile,
                has_stored_layout_profile,
            )
            if routing_provider and not improver_ok:
                # An explicit codegen CLI is a provider lock. Do not run
                # deterministic/config rewrites or deliver the unchanged
                # worktree after its agentic invocation failed; report a clean
                # failure and preserve the existing project byte-for-byte.
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    status="failed",
                    detail={
                        "delivered": 0,
                        "improver_success": False,
                        "improver_error": improver_err,
                        "delivery_blocked": "routing_lock",
                        "routing_locked": True,
                        "routing_lock_provider": routing_provider,
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                        "routing_snapshot": routing_summary,
                        "layout_profile": layout_profile,
                        **({"skipped": skipped} if skipped else {}),
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome

            # Same deterministic, build-readying repairs the main build pipeline
            # runs (StudioRunner._deterministic_repairs -> apply_deterministic_repairs)
            # — so an improve that turns a server component into a client one (needs
            # "use client"), adds a new dependency, or introduces a missing local
            # import does NOT ship an app that won't build. Without this, improve was
            # a do-no-harm violation: it delivered a broken tree while reporting
            # success (found live on the Apple-SEO site — a contact-form improve
            # added useState/onSubmit with no "use client", breaking `next build`).
            # Best-effort; never blocks a delivery.
            repairs: dict[str, Any] = {}
            try:
                repairs = apply_deterministic_repairs(wt.dir, stack=stack)
                changed = {k: v for k, v in repairs.items() if v}
                if changed:
                    await self._emit(EventType.IMPROVE_STAGE,
                                     {"slug": slug, "stage": "repairing", **changed}, cid)
            except Exception as exc:  # noqa: BLE001 - repairs never break an improve
                _log.warning("improve.deterministic_repairs_failed", slug=slug, error=str(exc))

            # Config surfacing can write source/config files. Run it in the
            # isolated worktree before proof so those writes are covered by the
            # same transaction instead of mutating the delivered project after
            # it was already verified.
            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "finalizing"}, cid)
            config_summary = await self._surface_config(
                Path(wt.dir), goal, stack, slug, cid
            )

            if _file_identity(
                Path(wt.dir) / PRODUCT_SPEC_RELATIVE_PATH
            ) != contract_preimage:
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "product_contract_mutated",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome

            await self._emit(EventType.IMPROVE_STAGE,
                             {"slug": slug, "stage": "verifying"}, cid)
            candidate_before_proof = await asyncio.to_thread(
                _source_snapshot,
                Path(wt.dir),
            )
            if not candidate_before_proof.get("valid"):
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "candidate_snapshot_invalid",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome
            proof = await _proof_run_without_blocking(
                wt.dir, stack=stack,
                execution_backend=getattr(self.settings, "execution_backend", "auto"),
                run_tests=bool(getattr(self.settings, "run_generated_tests", False)),
                test_timeout=int(getattr(self.settings, "generated_test_timeout", 90)),
                run_build=bool(getattr(self.settings, "run_generated_build", False)),
                build_timeout=int(getattr(self.settings, "generated_build_timeout", 300)),
            )
            proof_payload = proof.to_dict()
            if not proof.passed:
                # The original project has not been touched yet. Record the
                # rejected attempt without replacing its still-valid GO/proof
                # evidence, then return an honest failed outcome.
                rejection_history_safe = (
                    self.record_history
                    and manifest is not None
                    and _same_source_snapshot(
                        project_preimage,
                        _source_snapshot(project_dir),
                    )
                    and _control_identities(project_dir) == control_preimages
                )
                if rejection_history_safe:
                    try:
                        self._record_history(
                            manifest,
                            project_dir,
                            goal,
                            list_files(project_dir),
                            proof,
                            stack,
                            slug,
                            files_changed=files_changed,
                            delivered_change=False,
                        )
                    except Exception as rec_exc:  # noqa: BLE001
                        _log.warning(
                            "improve.record_rejection_failed",
                            slug=slug,
                            error=str(rec_exc),
                        )
                failure_detail: dict[str, Any] = {
                    "delivered": 0,
                    "proof": proof_payload,
                    "improver_success": improver_ok,
                    "improver_error": improver_err,
                    "delivery_blocked": "proof_failed",
                    "project_preserved": True,
                    "repo_context_pack": context_pack_summary,
                    "layout_profile": layout_profile,
                }
                if skipped:
                    failure_detail["skipped"] = skipped
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=False,
                    score=float(proof.score),
                    status="failed",
                    detail=failure_detail,
                )
                await _emit_failed_outcome(outcome)
                return outcome

            if _file_identity(
                Path(wt.dir) / PRODUCT_SPEC_RELATIVE_PATH
            ) != contract_preimage:
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=bool(proof.passed),
                    score=float(proof.score),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "proof": proof_payload,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "product_contract_mutated",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome

            candidate_after_proof = await asyncio.to_thread(
                _source_snapshot,
                Path(wt.dir),
            )
            if not _same_source_snapshot(
                candidate_before_proof,
                candidate_after_proof,
            ):
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=bool(proof.passed),
                    score=float(proof.score),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "proof": proof_payload,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "proof_source_changed",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome

            deliverable_after_proof = await asyncio.to_thread(
                _delivery_snapshot,
                Path(wt.dir),
            )
            if not deliverable_after_proof.get("valid"):
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=bool(proof.passed),
                    score=float(proof.score),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "proof": proof_payload,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "candidate_snapshot_invalid",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome

            # Emit before the final compare so an event subscriber cannot write
            # into either tree in the gap between comparison and promotion.
            await self._emit(
                EventType.IMPROVE_STAGE,
                {"slug": slug, "stage": "delivering"},
                cid,
            )
            candidate_for_delivery = await asyncio.to_thread(
                _source_snapshot,
                Path(wt.dir),
            )
            deliverable_for_delivery = await asyncio.to_thread(
                _delivery_snapshot,
                Path(wt.dir),
            )
            if not _same_source_snapshot(
                candidate_after_proof,
                candidate_for_delivery,
            ) or not _same_source_snapshot(
                deliverable_after_proof,
                deliverable_for_delivery,
            ):
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=bool(proof.passed),
                    score=float(proof.score),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "proof": proof_payload,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "verified_candidate_changed",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome
            if _file_identity(
                Path(wt.dir) / PRODUCT_SPEC_RELATIVE_PATH
            ) != contract_preimage:
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=bool(proof.passed),
                    score=float(proof.score),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "proof": proof_payload,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "product_contract_mutated",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome

            current_project = await asyncio.to_thread(
                _source_snapshot,
                project_dir,
            )
            current_delivery = await asyncio.to_thread(
                _delivery_snapshot,
                project_dir,
            )
            current_controls = await asyncio.to_thread(
                _control_identities,
                project_dir,
            )
            if (
                not _same_source_snapshot(project_preimage, current_project)
                or not _same_source_snapshot(
                    project_delivery_preimage,
                    current_delivery,
                )
                or current_controls != control_preimages
            ):
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=bool(proof.passed),
                    score=float(proof.score),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "proof": proof_payload,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "project_changed",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome

            # Materialize an immutable sibling candidate first. If a lingering
            # proof child changes the worktree during this copy, its snapshot
            # will not match the exact tree that passed proof.
            source_files = list_files(wt.dir)
            expected_files = set(source_files)
            # delivery_staging_dir, not raw mkdtemp: mkdtemp's owner-only
            # Windows ACL rode the same-volume swap into delivered projects
            # and made every file unreadable to Docker preview/proof mounts.
            delivery_stage_root = delivery_staging_dir(
                f".improve-stage-{slug}-", project_dir.parent
            )
            staged_project = delivery_stage_root / "candidate"
            staged_project.mkdir()
            staged_files = set(
                merge_back(
                    wt.dir,
                    str(staged_project),
                    overwrite=True,
                    clean=False,
                )
            )
            if staged_files != expected_files or set(
                list_files(staged_project)
            ) != expected_files:
                raise RuntimeError("improve candidate staging was incomplete")
            _copy_directory_metadata(Path(wt.dir), staged_project)
            staged_snapshot = _source_snapshot(staged_project)
            staged_delivery_snapshot = _delivery_snapshot(staged_project)
            if not _same_source_snapshot(
                candidate_after_proof,
                staged_snapshot,
            ) or not _same_source_snapshot(
                deliverable_after_proof,
                staged_delivery_snapshot,
            ):
                raise RuntimeError(
                    "improve candidate changed after proof while staging"
                )
            if _file_identity(
                staged_project / PRODUCT_SPEC_RELATIVE_PATH
            ) != contract_preimage:
                raise RuntimeError(
                    "Product Contract changed after proof while staging"
                )

            # Compare again after staging, then atomically rename the exact
            # original aside before promoting the immutable sibling candidate.
            if (
                not _same_source_snapshot(
                    project_preimage,
                    _source_snapshot(project_dir),
                )
                or not _same_source_snapshot(
                    project_delivery_preimage,
                    _delivery_snapshot(project_dir),
                )
                or _control_identities(project_dir) != control_preimages
            ):
                outcome = ImproveOutcome(
                    project_dir=str(project_dir),
                    slug=slug,
                    stack=stack,
                    goal=goal,
                    files_changed=sorted(files_changed),
                    proof_passed=bool(proof.passed),
                    score=float(proof.score),
                    status="failed",
                    detail={
                        "delivered": 0,
                        "proof": proof_payload,
                        "improver_success": improver_ok,
                        "improver_error": improver_err,
                        "delivery_blocked": "project_changed",
                        "project_preserved": True,
                        "repo_context_pack": context_pack_summary,
                    },
                )
                await _emit_failed_outcome(outcome)
                return outcome

            backup_root = delivery_staging_dir(
                f".improve-backup-{slug}-", project_dir.parent
            )
            backup_project = backup_root / "project"
            backup_project.mkdir()
            try:
                original_files = set(list_files(project_dir))
                backup_files = set(
                    merge_back(
                        str(project_dir),
                        str(backup_project),
                        overwrite=True,
                        clean=False,
                    )
                )
                _copy_directory_metadata(project_dir, backup_project)
                if (
                    backup_files != original_files
                    or not _same_source_snapshot(
                        project_preimage,
                        _source_snapshot(backup_project),
                    )
                    or not _same_source_snapshot(
                        project_delivery_preimage,
                        _delivery_snapshot(backup_project),
                    )
                    or _control_identities(backup_project) != control_preimages
                ):
                    raise RuntimeError(
                        "project changed while delivery backup was created"
                    )

                # Never deliver agent/proof copies of runner control state.
                _restore_control_files(
                    backup_project,
                    staged_project,
                    control_preimages,
                )
                delivered = list_files(staged_project)
                if self.record_history:
                    try:
                        self._record_history(
                            manifest,
                            staged_project,
                            goal,
                            delivered,
                            proof,
                            stack,
                            slug,
                            config_summary,
                            files_changed=files_changed,
                        )
                    except Exception as rec_exc:  # noqa: BLE001
                        _log.warning(
                            "improve.record_history_failed",
                            slug=slug,
                            error=str(rec_exc),
                        )
                    delivered = list_files(staged_project)

                _copy_directory_metadata(backup_project, staged_project)

                expected_delivery_snapshot = _delivery_snapshot(staged_project)
                expected_source_snapshot = _source_snapshot(staged_project)
                if (
                    not _same_source_snapshot(
                        candidate_after_proof,
                        expected_source_snapshot,
                    )
                    or not expected_delivery_snapshot.get("valid")
                ):
                    raise RuntimeError(
                        "final staged candidate no longer matches verified proof"
                    )

                # Last compare occurs synchronously after every await and after
                # backup creation. Delivery keeps the project root inode and
                # machine-local ignored state, but journals each live file
                # before installing the verified candidate with no-replace links.
                if (
                    not _same_source_snapshot(
                        project_preimage,
                        _source_snapshot(project_dir),
                    )
                    or not _same_source_snapshot(
                        project_delivery_preimage,
                        _delivery_snapshot(project_dir),
                    )
                    or _control_identities(project_dir) != control_preimages
                ):
                    raise RuntimeError(
                        "project changed immediately before delivery"
                    )
                delivery_mutated = True
                displaced_project = backup_root / "displaced-live"
                _move_snapshot_files(
                    project_dir,
                    displaced_project,
                    project_delivery_preimage,
                )
                if not _same_source_snapshot(
                    project_delivery_preimage,
                    _delivery_snapshot(displaced_project),
                ):
                    raise RuntimeError(
                        "project changed while live files were journaled"
                    )
                _link_snapshot_files(
                    staged_project,
                    project_dir,
                    expected_delivery_snapshot,
                )
                if not _same_source_snapshot(
                    candidate_after_proof,
                    _source_snapshot(project_dir),
                ):
                    raise RuntimeError(
                        "delivered project does not match verified source"
                    )
                if not _same_source_snapshot(
                    expected_delivery_snapshot,
                    _delivery_snapshot(project_dir),
                ):
                    raise RuntimeError(
                        "delivered project does not match verified artifacts"
                    )
                if _file_identity(
                    project_dir / PRODUCT_SPEC_RELATIVE_PATH
                ) != contract_preimage:
                    raise RuntimeError(
                        "delivered project changed the Product Contract"
                    )
                delivered = list_files(project_dir)
                delivery_committed = True
            except Exception as delivery_exc:
                if delivery_mutated:
                    preserve_recovery_artifacts = True
                    try:
                        failed_delivery_snapshot = _delivery_snapshot(
                            project_dir
                        )
                        if not failed_delivery_snapshot.get("valid"):
                            raise RuntimeError(
                                "failed delivery tree is unstable; automatic "
                                "rollback would risk a concurrent edit"
                            )
                        failed_project = backup_root / "failed-delivery"
                        _move_snapshot_files(
                            project_dir,
                            failed_project,
                            failed_delivery_snapshot,
                        )
                        if not _same_source_snapshot(
                            failed_delivery_snapshot,
                            _delivery_snapshot(failed_project),
                        ):
                            raise RuntimeError(
                                "failed delivery changed while being preserved"
                            )
                        _link_snapshot_files(
                            backup_project,
                            project_dir,
                            project_delivery_preimage,
                        )
                        if (
                            not _same_source_snapshot(
                                project_preimage,
                                _source_snapshot(project_dir),
                            )
                            or not _same_source_snapshot(
                                project_delivery_preimage,
                                _delivery_snapshot(project_dir),
                            )
                            or _control_identities(project_dir)
                            != control_preimages
                        ):
                            raise RuntimeError(
                                "restored project did not match its preimage"
                            )
                        rollback_confirmed = True
                    except Exception as rollback_error:  # noqa: BLE001
                        _log.error(
                            "improve.delivery_rollback_failed",
                            slug=slug,
                            error=str(rollback_error),
                            recovery_root=str(backup_root),
                        )
                        raise RuntimeError(
                            "improve delivery failed and automatic rollback "
                            f"requires recovery from {backup_root}"
                        ) from delivery_exc
                raise
            try:
                shutil.rmtree(backup_root)
            except OSError as cleanup_exc:
                _log.warning(
                    "improve.backup_cleanup_failed",
                    slug=slug,
                    error=str(cleanup_exc),
                )
            if not backup_root.exists():
                backup_root = None
            try:
                shutil.rmtree(delivery_stage_root)
            except OSError as cleanup_exc:
                _log.warning(
                    "improve.stage_cleanup_failed",
                    slug=slug,
                    error=str(cleanup_exc),
                )
            if not delivery_stage_root.exists():
                delivery_stage_root = None

            detail: dict[str, Any] = {
                "delivered": len(delivered), "proof": proof_payload,
                "improver_success": improver_ok, "improver_error": improver_err,
                "repo_context_pack": context_pack_summary,
                "routing_snapshot": routing_summary,
                "layout_profile": layout_profile,
            }
            if skipped:
                detail["skipped"] = skipped
            # An honest signal for the dashboard: 0 files touched must not read
            # like a quiet success just because proof_run/merge_back didn't
            # error — surface it explicitly so the UI can tell the user their
            # goal wasn't actually acted on. Two distinct cases: the improver
            # found target files but declined/failed every rewrite (skipped
            # carries per-file reasons), vs. target discovery itself came up
            # empty (see code_improver's discovery fallback).
            if not files_changed:
                if skipped:
                    detail["no_files_changed"] = True
                else:
                    detail["no_targets_found"] = True
            outcome = ImproveOutcome(
                project_dir=str(project_dir), slug=slug, stack=stack, goal=goal,
                files_changed=sorted(files_changed), proof_passed=bool(proof.passed),
                score=float(proof.score), status="completed",
                detail=detail,
            )
            await self._emit(EventType.IMPROVE_COMPLETED, outcome.to_dict(), cid)
            return outcome
        except Exception as exc:  # noqa: BLE001 - report failure, never a partial deliver
            failure_payload: dict[str, Any] = {
                "slug": slug,
                "goal": goal,
                "error": str(exc),
            }
            if context_pack_summary:
                failure_payload["repo_context_pack"] = context_pack_summary
            failure_payload["routing_snapshot"] = routing_summary
            failure_payload["layout_profile"] = layout_profile
            if preserve_recovery_artifacts and backup_root is not None:
                failure_payload["recovery_root"] = str(backup_root)
                failure_payload["project_preserved"] = rollback_confirmed
            await self._emit(EventType.IMPROVE_FAILED, failure_payload, cid)
            return ImproveOutcome(project_dir=str(project_dir), slug=slug, stack=stack,
                                  goal=goal, status="failed",
                                  detail={
                                      "error": str(exc),
                                      **(
                                          {
                                              "delivery_blocked": "delivery_failed",
                                              "project_preserved": rollback_confirmed,
                                          }
                                          if delivery_mutated
                                          else {"project_preserved": True}
                                      ),
                                      "routing_snapshot": routing_summary,
                                      "layout_profile": layout_profile,
                                      **(
                                          {"recovery_root": str(backup_root)}
                                          if preserve_recovery_artifacts
                                          and backup_root is not None
                                          else {}
                                      ),
                                      **(
                                          {"repo_context_pack": context_pack_summary}
                                          if context_pack_summary
                                          else {}
                                      ),
                                  })
        finally:
            try:
                if wt is not None:
                    cleanup_worktree(wt)
            finally:
                try:
                    cleanup_delivery_artifacts = (
                        delivery_committed
                        or (
                            rollback_confirmed
                            and not preserve_recovery_artifacts
                        )
                        or not delivery_mutated
                    )
                    if cleanup_delivery_artifacts:
                        if delivery_stage_root is not None:
                            shutil.rmtree(
                                delivery_stage_root,
                                ignore_errors=True,
                            )
                        if backup_root is not None:
                            shutil.rmtree(backup_root, ignore_errors=True)
                    elif backup_root is not None:
                        _log.error(
                            "improve.recovery_artifacts_preserved",
                            slug=slug,
                            recovery_root=str(backup_root),
                        )
                finally:
                    try:
                        if process_lock is not None:
                            process_lock.release()
                    finally:
                        project_lock.release()

    async def _run_improver(
        self,
        worktree_dir: str,
        slug: str,
        stack: str,
        goal: str,
        repo_ctx: str,
        context_pack_summary: dict[str, object],
        routing_provider: str,
        routing_model: str,
        improve_agentic: bool,
        improve_agentic_timeout: int,
        routing_summary: dict[str, Any],
        cid: str,
        layout_profile: dict[str, str | int | bool],
        layout_profile_is_stored: bool,
    ) -> tuple[list[str], bool, str, dict[str, str]]:
        task = TaskRequest(
            type="code_improver",
            payload={"worktree_dir": worktree_dir, "brief": goal, "slug": slug,
                     "stack": stack, "gaps": [goal], "repo_map": repo_ctx,
                     "repo_context_pack": dict(context_pack_summary),
                     "routing_snapshot": deepcopy(routing_summary),
                     "layout_profile": dict(layout_profile),
                     "layout_profile_is_stored": layout_profile_is_stored,
                     # Free-text goals get the whole-project agentic session
                     # (multi-file, can create pages); the per-file path stays
                     # the automatic fallback inside the improver.
                     "agentic": improve_agentic,
                     "agentic_timeout": improve_agentic_timeout,
                     "agentic_provider": routing_provider,
                     "agentic_model": routing_model},
            capabilities_required=("code_improve",),
            correlation_id=cid,
            metadata={"routing_snapshot": deepcopy(routing_summary)},
        )
        result = await self.orchestrator.submit(task)
        ok = bool(result and getattr(result, "success", False))
        output = (getattr(result, "output", None) or {}) if result else {}
        files = list(output.get("files", [])) if ok else []
        # rel -> reason the improver declined the file ("already_satisfied",
        # "unchanged", "invalid_rewrite", ...) — the difference between "your
        # goal was already done" and "the rewrite failed" for the cockpit.
        skipped = dict(output.get("skipped", {}) or {})
        err = "" if ok else str(
            getattr(result, "error", "")
            or output.get("routing_lock_reason")
            or "improver did not succeed"
        )
        return files, ok, err, skipped

    async def _surface_config(self, project_dir: Path, goal: str, stack: str,
                              slug: str, cid: str) -> dict[str, Any]:
        """Detect/generate/verify config for the edited project. Never raises."""
        try:
            from skyn3t.agents.config_ui_agent import apply_config

            summary = apply_config(str(project_dir), goal, stack)
        except Exception as exc:  # noqa: BLE001 - config surfacing never breaks improve
            _log.warning("improve.config_surface_failed", slug=slug, error=str(exc))
            return {}
        await self._emit(EventType.CONFIG_CHECK,
                         {"slug": slug, "stack": stack, **summary}, cid)
        return summary

    def _record_history(self, manifest: BuildManifest | None, project_dir: Path,
                        goal: str, delivered: list[str], proof: Any,
                        stack: str, slug: str,
                        config_summary: dict[str, Any] | None = None,
                        files_changed: list[str] | None = None,
                        delivered_change: bool = True) -> None:
        from datetime import datetime

        man = manifest or BuildManifest(slug=slug, brief="", stack=stack, status="completed")
        hist = man.extra.setdefault("improve_history", [])
        # `files` is the TOTAL delivered to the project dir (kept for compat);
        # `files_changed` is the honest signal — it stayed at "files: 261" while
        # 8 consecutive improves changed nothing, hiding the silent no-op.
        hist.append({"goal": goal, "files": len(delivered),
                     "files_changed": len(files_changed or []),
                     "at": datetime.now(UTC).isoformat(),
                     "proof_passed": bool(proof.passed), "score": float(proof.score),
                     "delivered": bool(delivered_change)})
        if delivered_change and config_summary:
            man.extra["config_spec"] = config_summary.get("config_spec", {})
            man.extra["config_wiring"] = config_summary.get("wiring", {})
        if delivered_change:
            man.status = "completed"
            man.verdict = "go"
            man.files = list(delivered)
            man.extra["proof"] = proof.to_dict()
        man.touch()
        man.save(project_dir)
