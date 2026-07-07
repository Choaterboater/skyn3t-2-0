"""Editable flow-canvas artifact for workflow builds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text

FLOW_CANVAS_FILENAME = "skyn3t-flow.json"
FLOW_CANVAS_SCHEMA = "skyn3t.flow_canvas.v1"


def build_flow_canvas(plan: Any) -> dict[str, Any]:
    stages = list(getattr(plan, "stages", []) or [])
    nodes = [{
        "id": "start",
        "type": "trigger",
        "label": "Trigger",
        "position": {"x": 0, "y": 0},
    }]
    for idx, stage in enumerate(stages, start=1):
        nodes.append({
            "id": f"stage:{stage.name}",
            "type": "agent_stage",
            "label": stage.name,
            "agent_type": stage.agent_type,
            "capability": stage.capability,
            "gated": bool(getattr(stage, "gated", False)),
            "optional": bool(getattr(stage, "optional", False)),
            "position": {"x": idx * 220, "y": 0},
        })
    nodes.append({
        "id": "deliver",
        "type": "output",
        "label": "Deliver",
        "position": {"x": (len(stages) + 1) * 220, "y": 0},
    })
    ids = [node["id"] for node in nodes]
    edges = [{"source": ids[idx], "target": ids[idx + 1]} for idx in range(len(ids) - 1)]
    return {
        "schema": FLOW_CANVAS_SCHEMA,
        "slug": str(getattr(plan, "slug", "")),
        "brief": str(getattr(plan, "brief", "")),
        "stack": str(getattr(plan, "stack", "")),
        "editable": True,
        "nodes": nodes,
        "edges": edges,
    }


def write_flow_canvas(project_dir: str | Path, plan: Any) -> dict[str, Any]:
    flow = build_flow_canvas(plan)
    path = Path(project_dir) / FLOW_CANVAS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(flow, indent=2, sort_keys=False))
    return flow


def read_flow_canvas(project_dir: str | Path) -> dict[str, Any] | None:
    path = Path(project_dir) / FLOW_CANVAS_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != FLOW_CANVAS_SCHEMA:
        return None
    if not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        return None
    return data
