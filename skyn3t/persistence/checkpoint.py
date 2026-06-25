"""Checkpointing — snapshot EventBus + key build state to disk.

A :class:`CheckpointManager` writes atomic JSON snapshots under
``data_dir/checkpoints``. Each checkpoint captures the EventBus history snapshot
plus arbitrary caller-supplied state (current build, stage outputs, written
files manifest). Writes are atomic (temp file + ``os.replace``) so a crash mid
write never corrupts an existing checkpoint.

Import is side-effect free; directories are created lazily on first save.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Any

import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import EventBus

log = structlog.get_logger(__name__)

CHECKPOINT_VERSION = 1


@dataclass
class Checkpoint:
    version: int
    created: float
    label: str
    event_bus: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created": self.created,
            "label": self.label,
            "event_bus": self.event_bus,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Checkpoint:
        return cls(
            version=d.get("version", CHECKPOINT_VERSION),
            created=d.get("created", 0.0),
            label=d.get("label", ""),
            event_bus=d.get("event_bus", {}),
            state=d.get("state", {}),
        )


@dataclass
class CheckpointManager:
    settings: Settings = field(default_factory=get_settings)
    _dir: Path | None = field(default=None, repr=False)

    @property
    def dir(self) -> Path:
        if self._dir is None:
            self._dir = self.settings.data_dir / "checkpoints"
        return self._dir

    # ---- save ------------------------------------------------------------
    def save(
        self,
        label: str = "boot",
        *,
        event_bus: EventBus | None = None,
        state: dict[str, Any] | None = None,
    ) -> Path:
        """Atomically write a checkpoint. Returns the file path."""
        cp = Checkpoint(
            version=CHECKPOINT_VERSION,
            created=time(),
            label=label,
            event_bus=event_bus.snapshot() if event_bus is not None else {},
            state=state or {},
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / f"{label}.json"
        self._atomic_write(target, cp.to_dict())
        # Also keep a rolling "latest" pointer.
        self._atomic_write(self.dir / "latest.json", cp.to_dict())
        log.info("checkpoint.saved", label=label, path=str(target))
        return target

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ---- load ------------------------------------------------------------
    def load(self, label: str = "latest") -> Checkpoint | None:
        path = self.dir / f"{label}.json"
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return Checkpoint.from_dict(json.load(fh))
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
            log.warning("checkpoint.load_failed", label=label, error=str(exc))
            return None

    def list_checkpoints(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def delete(self, label: str) -> bool:
        path = self.dir / f"{label}.json"
        if path.exists():
            try:
                path.unlink()
                return True
            except OSError:
                return False
        return False
