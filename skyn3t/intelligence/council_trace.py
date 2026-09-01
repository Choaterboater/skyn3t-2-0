"""Opt-in JSONL trace of Mixture-of-Agents council runs.

One line per council run, appended to ``logs_dir/moa/<build_id>.jsonl``. Unlike
``manifest.extra["moa"]`` — which is a bounded record persisted into the durable
build row — a trace line carries the FULL advisor input and output, for offline
audit of what each advisor actually said.

``logs_dir``, not ``data_dir``: ``data_dir`` holds durable state (the SQLite db,
model_tournament.json, cortex proposals) that tests redirect per-run, while
``skyn3t/observability/traces.py`` already writes its JSONL under ``logs_dir``.
These files are disposable — deleting them loses nothing but history.

Adapted from NousResearch/hermes-agent (MIT), ``agent/moa_trace.py``: opt-in via
a setting so there is zero overhead when off (``moa_trace.py:37-57``), the
full-fidelity record shape (``:97-167``), and — importantly — tracing must never
break the turn it is tracing (``:166-167``).

Import has zero side effects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def trace_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "moa_trace_enabled", False))


def trace_path(settings: Any, build_id: str) -> Path:
    logs_dir = Path(getattr(settings, "logs_dir", "logs"))
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(build_id or "build"))
    return logs_dir / "moa" / f"{safe or 'build'}.jsonl"


def save_council_run(
    settings: Any,
    *,
    build_id: str,
    slug: str,
    stack: str,
    task: str,
    advice: Any,
) -> Path | None:
    """Append one council run. Returns the path written, or None.

    Never raises: a trace failure must not affect the build being traced.
    """
    if not trace_enabled(settings):
        return None
    try:
        path = trace_path(settings, build_id)
        record = {
            "build_id": str(build_id),
            "slug": str(slug),
            "stack": str(stack),
            "task": task,
            "advisors": [
                {
                    **advisor.to_dict(),
                    # The bounded manifest record deliberately omits prose; the
                    # trace is where the full text belongs.
                    "text": advisor.text,
                }
                for advisor in getattr(advice, "advisors", [])
            ],
            "guidance": getattr(advice, "guidance", ""),
            "degraded": bool(getattr(advice, "degraded", False)),
            "cost_usd": float(getattr(advice, "cost_usd", 0.0) or 0.0),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return path
    except OSError as exc:
        log.warning("moa.trace_write_failed", error=str(exc)[:160])
    except Exception as exc:  # noqa: BLE001 - tracing may never break a build
        log.warning("moa.trace_failed", error=str(exc)[:160])
    return None
