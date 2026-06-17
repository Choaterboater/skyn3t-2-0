"""Recovery — restore EventBus + build state from a checkpoint on boot.

P2 GRANULAR 3-MODE RESTORE: a checkpoint can be restored in three modes so a
crashed build can resume at the right granularity:

* ``files``        — restore only the written-files manifest / worktree state,
                     discard task progress (re-run the pipeline from scratch
                     but keep generated files).
* ``task``         — restore only task/stage progress (resume the plan), but
                     start from clean files.
* ``files+task``   — restore both (full warm resume — the default).

Restoration mutates the provided EventBus in place and returns a structured
:class:`RecoveryResult` describing what was rehydrated. Side-effect free import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import structlog

from skyn3t.core.events import EventBus, EventType
from skyn3t.persistence.checkpoint import Checkpoint, CheckpointManager

log = structlog.get_logger(__name__)


class RestoreMode(str, Enum):
    FILES = "files"
    TASK = "task"
    FILES_AND_TASK = "files+task"


@dataclass
class RecoveryResult:
    restored: bool
    mode: Optional[str] = None
    label: Optional[str] = None
    events_restored: int = 0
    files_state: dict[str, Any] = field(default_factory=dict)
    task_state: dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored": self.restored,
            "mode": self.mode,
            "label": self.label,
            "events_restored": self.events_restored,
            "files_state": self.files_state,
            "task_state": self.task_state,
            "detail": self.detail,
        }


# Keys within checkpoint.state recognised for each restore facet.
_FILES_KEYS = ("files", "files_written", "worktree_dir", "manifest", "artifact_dir")
_TASK_KEYS = ("task", "stage", "plan", "prior", "build_id", "slug", "stack", "stage_outputs")


class RecoveryManager:
    """Boot-time restore orchestrator with 3-mode granularity."""

    def __init__(self, manager: CheckpointManager | None = None) -> None:
        self.manager = manager or CheckpointManager()

    def restore(
        self,
        *,
        event_bus: EventBus | None = None,
        mode: RestoreMode | str = RestoreMode.FILES_AND_TASK,
        label: str = "latest",
    ) -> RecoveryResult:
        """Restore from a checkpoint into ``event_bus`` per ``mode``."""
        mode = RestoreMode(mode) if not isinstance(mode, RestoreMode) else mode
        cp = self.manager.load(label)
        if cp is None:
            return RecoveryResult(restored=False, mode=mode.value, label=label,
                                  detail="no checkpoint found")

        events_restored = 0
        # EventBus is restored whenever any mode is selected — it is the audit
        # spine. Restoring it never replays handlers (snapshot/restore is inert).
        if event_bus is not None and cp.event_bus:
            event_bus.restore(cp.event_bus)
            events_restored = len(cp.event_bus.get("history", []))

        files_state = self._extract(cp, _FILES_KEYS) if mode in (
            RestoreMode.FILES, RestoreMode.FILES_AND_TASK) else {}
        task_state = self._extract(cp, _TASK_KEYS) if mode in (
            RestoreMode.TASK, RestoreMode.FILES_AND_TASK) else {}

        result = RecoveryResult(
            restored=True, mode=mode.value, label=label,
            events_restored=events_restored,
            files_state=files_state, task_state=task_state,
            detail=f"restored mode={mode.value} from '{label}'",
        )
        log.info("recovery.restored", **{k: v for k, v in result.to_dict().items()
                                         if k in ("mode", "label", "events_restored")})
        return result

    async def restore_and_announce(
        self,
        event_bus: EventBus,
        *,
        mode: RestoreMode | str = RestoreMode.FILES_AND_TASK,
        label: str = "latest",
    ) -> RecoveryResult:
        """Restore then emit a SYSTEM event so observers learn of the resume."""
        result = self.restore(event_bus=event_bus, mode=mode, label=label)
        await event_bus.emit(
            EventType.SYSTEM, "recovery",
            {"event": "restore", **result.to_dict()},
        )
        return result

    @staticmethod
    def _extract(cp: Checkpoint, keys: tuple[str, ...]) -> dict[str, Any]:
        return {k: cp.state[k] for k in keys if k in cp.state}
