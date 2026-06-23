"""BuildManifest dataclass + per-project JSON persistence.

The manifest is the durable, human-readable record of a build: the brief, the
detected stack, every stage's status/output summary, scores, cost, and the list
of delivered files. It is written into the delivered project directory so a
build is auditable on disk (design rule #3 — verify behavior, not vibes).

Import has zero side effects; nothing is written until :meth:`save` is called.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text

MANIFEST_FILENAME = "skyn3t_manifest.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class StageRecord:
    """A single stage outcome captured in the manifest."""

    name: str
    agent_type: str
    capability: str
    status: str = "pending"  # pending|completed|skipped|failed
    score: float | None = None
    duration_ms: float = 0.0
    agent_name: str | None = None
    error: str | None = None
    output_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BuildManifest:
    """Durable record of one brief->app build."""

    slug: str
    brief: str
    build_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stack: str = ""
    status: str = "pending"  # pending|running|completed|completed_no_go|failed
    verdict: str = ""  # go|no_go
    score: float | None = None
    cost_usd: float = 0.0
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    artifact_dir: str | None = None
    worktree_dir: str | None = None
    files: list[str] = field(default_factory=list)
    stages: list[StageRecord] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- mutation helpers ------------------------------------------------
    def add_stage(self, record: StageRecord) -> StageRecord:
        self.stages.append(record)
        self.touch()
        return record

    def touch(self) -> None:
        self.updated_at = _utcnow_iso()

    @property
    def files_count(self) -> int:
        return len(self.files)

    @property
    def completed_stages(self) -> int:
        return sum(1 for s in self.stages if s.status == "completed")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BuildManifest":
        stages = [StageRecord(**s) for s in d.get("stages", [])]
        kwargs = {k: v for k, v in d.items() if k != "stages"}
        kwargs["stages"] = stages
        return cls(**kwargs)

    # ---- persistence -----------------------------------------------------
    def save(self, project_dir: str | Path) -> Path:
        """Write the manifest JSON into ``project_dir`` and return its path."""
        self.touch()
        pdir = Path(project_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / MANIFEST_FILENAME
        # Atomic write: a crash mid-write must not truncate the durable record.
        atomic_write_text(path, json.dumps(self.to_dict(), indent=2, sort_keys=False))
        return path

    @classmethod
    def load(cls, project_dir: str | Path) -> "BuildManifest | None":
        path = Path(project_dir) / MANIFEST_FILENAME
        if not path.exists():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError, KeyError):
            return None
