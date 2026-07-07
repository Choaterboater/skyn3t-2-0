"""Durable collaboration notes for delivered projects."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from time import time
from typing import Any

from skyn3t.atomic_io import atomic_write_text

COLLABORATION_FILENAME = "skyn3t-collaboration.jsonl"


def list_collaboration_events(project_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(project_dir) / COLLABORATION_FILENAME
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def append_collaboration_event(
    project_dir: str | Path,
    *,
    actor: str,
    action: str,
    body: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "id": uuid.uuid4().hex,
        "ts": time(),
        "actor": str(actor or "unknown"),
        "action": str(action or "note"),
        "body": str(body or ""),
        "metadata": dict(metadata or {}),
    }
    pdir = Path(project_dir)
    events = [*list_collaboration_events(pdir), event]
    text = "".join(json.dumps(item, sort_keys=False) + "\n" for item in events)
    atomic_write_text(pdir / COLLABORATION_FILENAME, text)
    _update_manifest_summary(pdir, events)
    return event


def _update_manifest_summary(project_dir: Path, events: list[dict[str, Any]]) -> None:
    try:
        from skyn3t.studio.manifest import BuildManifest

        manifest = BuildManifest.load(project_dir)
        if manifest is None:
            return
        latest = events[-1] if events else {}
        manifest.extra["collaboration"] = {
            "path": COLLABORATION_FILENAME,
            "event_count": len(events),
            "latest_action": str(latest.get("action") or ""),
            "latest_actor": str(latest.get("actor") or ""),
            "updated_at": latest.get("ts"),
        }
        manifest.save(project_dir)
    except Exception:
        return
