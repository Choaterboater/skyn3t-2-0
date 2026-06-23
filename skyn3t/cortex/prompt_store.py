"""Persist cortex prompt-evolution overrides across CLI builds.

The prompt-evolution loop (PromptReflectionLoop → gated PROMPT proposal) writes
an evolved instruction onto a live agent's ``config['prompt_override']`` when a
human approves it. That in-process change evaporates when the process exits, so
a fresh ``studio build`` would lose what a prior build's reviewers learned. This
module mirrors the approved overrides to ``data/cortex/prompt_overrides.json``
keyed by the agent's capability/stage name, so a new process can re-apply them
to matching agents at startup.

stdlib-only, import-side-effect-free, and degrade-don't-crash: every function
swallows errors and never raises (design rule #6).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text


def overrides_path(data_dir: Any) -> Path:
    return Path(data_dir) / "cortex" / "prompt_overrides.json"


def load_prompt_overrides(data_dir: Any) -> dict[str, str]:
    """Return persisted ``{agent: instruction}`` overrides. ``{}`` on any error."""
    try:
        p = overrides_path(data_dir)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v}
    except Exception:  # noqa: BLE001 - a corrupt/unreadable file must not break a build
        return {}


def persist_prompt_override(data_dir: Any, agent: str, instruction: str) -> dict[str, str]:
    """Merge one approved override into the store. Never raises.

    Returns the resulting overrides dict (or ``{}`` on failure).
    """
    try:
        agent = str(agent or "").strip()
        instruction = str(instruction or "").strip()
        current = load_prompt_overrides(data_dir)
        if not agent or not instruction:
            return current
        current[agent] = instruction
        atomic_write_text(overrides_path(data_dir), json.dumps(current, indent=2))
        return current
    except Exception:  # noqa: BLE001
        return {}
