"""Durable, local-only Cortex autopilot receipts.

This module deliberately decides *what* should be worked on next.  Existing
Studio and Cortex candidate paths continue to perform the actual work, so the
same proof and isolated-worktree rules apply to autonomous repairs.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from skyn3t.atomic_io import atomic_write_text

RunKind = Literal["repair", "skill_experiment", "research"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "quarantined"]


@dataclass(slots=True)
class AutopilotIncident:
    incident_id: str
    scope: str
    category: str
    summary: str
    evidence: str = ""
    status: str = "open"
    occurrences: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class AutopilotRun:
    run_id: str
    kind: RunKind
    status: RunStatus
    summary: str
    incident_id: str | None = None
    selected_skills: list[str] = field(default_factory=list)
    proof_summary: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class LabAutopilot:
    """Small persistent priority queue for local autonomous work.

    It is intentionally executable without a web server, which keeps the
    prioritisation and deduplication contract directly testable.
    """

    schema_version = 1

    def __init__(self, data_dir: Path | str, *, enabled: bool = False) -> None:
        self.path = Path(data_dir) / "cortex" / "lab_autopilot.json"
        self.enabled = bool(enabled)
        self.incidents: list[AutopilotIncident] = []
        self.runs: list[AutopilotRun] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.enabled = bool(raw.get("enabled", self.enabled))
            self.incidents = [AutopilotIncident(**item) for item in raw.get("incidents", [])]
            self.runs = [AutopilotRun(**item) for item in raw.get("runs", [])]
        except (OSError, ValueError, TypeError):
            return

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "incidents": [asdict(item) for item in self.incidents[-100:]],
            "runs": [asdict(item) for item in self.runs[-100:]],
        }
        atomic_write_text(self.path, json.dumps(payload, sort_keys=True, indent=2) + "\n")

    @staticmethod
    def _id(prefix: str, *parts: str) -> str:
        text = "\x1f".join(part.strip() for part in parts)
        return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self._save()

    def report_incident(self, *, scope: str, category: str, summary: str, evidence: str = "") -> AutopilotIncident:
        safe_scope = str(scope or "skyn3t").strip()[:120]
        safe_category = str(category or "unknown").strip()[:120]
        safe_summary = str(summary or "Autopilot detected a failure").strip()[:1000]
        incident_id = self._id("incident", safe_scope, safe_category, safe_summary)
        for item in self.incidents:
            if item.incident_id == incident_id and item.status == "open":
                item.occurrences += 1
                item.evidence = str(evidence or item.evidence)[:2000]
                item.updated_at = time.time()
                self._save()
                return item
        item = AutopilotIncident(
            incident_id=incident_id,
            scope=safe_scope,
            category=safe_category,
            summary=safe_summary,
            evidence=str(evidence or "")[:2000],
        )
        self.incidents.append(item)
        self._save()
        return item

    def queue_skill_experiment(self, *, summary: str, skills: list[str]) -> AutopilotRun:
        return self._queue("skill_experiment", summary=summary, selected_skills=skills)

    def queue_research(self, *, summary: str) -> AutopilotRun:
        return self._queue("research", summary=summary)

    def next_run(self) -> AutopilotRun | None:
        if not self.enabled:
            return None
        for incident in self.incidents:
            if incident.status == "open":
                return self._queue("repair", summary=incident.summary, incident_id=incident.incident_id)
        queued = next((run for run in self.runs if run.status == "queued"), None)
        if queued is not None:
            queued.status = "running"
            queued.updated_at = time.time()
            self._save()
        return queued

    def finish(self, run_id: str, *, succeeded: bool, proof_summary: str = "") -> AutopilotRun:
        run = next(item for item in self.runs if item.run_id == run_id)
        run.status = "succeeded" if succeeded else "quarantined"
        run.proof_summary = str(proof_summary)[:2000]
        run.updated_at = time.time()
        if run.incident_id and succeeded:
            for incident in self.incidents:
                if incident.incident_id == run.incident_id:
                    incident.status = "resolved"
                    incident.updated_at = run.updated_at
        self._save()
        return run

    def _queue(self, kind: RunKind, *, summary: str, incident_id: str | None = None, selected_skills: list[str] | None = None) -> AutopilotRun:
        if incident_id:
            existing = next((item for item in self.runs if item.incident_id == incident_id and item.status in {"queued", "running"}), None)
            if existing is not None:
                return existing
        run = AutopilotRun(
            run_id=self._id("run", kind, incident_id or "", str(time.time_ns())),
            kind=kind,
            status="queued",
            summary=str(summary)[:1000],
            incident_id=incident_id,
            selected_skills=[str(skill)[:120] for skill in (selected_skills or [])[:8]],
        )
        self.runs.append(run)
        self._save()
        return run

    def payload(self) -> dict[str, Any]:
        active = next((item for item in self.runs if item.status == "running"), None)
        return {
            "enabled": self.enabled,
            "local_only": True,
            "remote_push": False,
            "active": asdict(active) if active else None,
            "open_incidents": [asdict(item) for item in self.incidents if item.status == "open"],
            "recent_runs": [asdict(item) for item in self.runs[-12:]][::-1],
        }
