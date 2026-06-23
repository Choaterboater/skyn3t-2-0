"""Persist a small allow-list of SAFE tuning changes across CLI builds.

The Cortex's SelfTuningEngine nudges live agent config in-process, but those
changes evaporate when the process exits — so a 'studio build' never benefited
from what a prior build learned. This module persists only a whitelisted set of
benign Settings fields to ``data/cortex/settings_overrides.json`` and lets
``Settings`` read them back on construction, so a tuned value carries forward.

stdlib-only, import-side-effect-free (NO reverse import of config.settings), and
degrade-don't-crash: every function swallows errors and never raises. NEVER
persists paths, secrets, autonomy flags, or budget caps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text

# Real, benign Settings fields safe to carry across builds. The INSIGHT ->
# SelfTuning path only ever writes ``critic_enabled`` / ``reflective_retry``
# today (the SAFE_FLAGS that are also Settings fields); the rest are valid
# Settings fields kept allow-listed for forward use. Anything not here is
# dropped on both write and read.
PERSISTABLE_TUNING = frozenset(
    {
        "critic_enabled",
        "reflective_retry",
        "best_of_n",
        "debate_enabled",
        "run_generated_tests",
        "run_generated_build",
    }
)


def overrides_path(data_dir: Any) -> Path:
    return Path(data_dir) / "cortex" / "settings_overrides.json"


def load_overrides(data_dir: Any) -> dict[str, Any]:
    """Return the persisted overrides (allow-list filtered). ``{}`` on any error."""
    try:
        p = overrides_path(data_dir)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in PERSISTABLE_TUNING}
    except Exception:  # noqa: BLE001 - a corrupt/unreadable file must not break Settings()
        return {}


def persist_overrides(data_dir: Any, applied: dict[str, Any]) -> dict[str, Any]:
    """Merge SAFE applied tuning into the overrides file. Never raises.

    Returns the resulting overrides dict (or ``{}`` on failure).
    """
    try:
        safe = {k: v for k, v in dict(applied or {}).items() if k in PERSISTABLE_TUNING}
        current = load_overrides(data_dir)
        if not safe:
            return current
        current.update(safe)
        p = overrides_path(data_dir)
        atomic_write_text(p, json.dumps(current, indent=2))
        return current
    except Exception:  # noqa: BLE001
        return {}
