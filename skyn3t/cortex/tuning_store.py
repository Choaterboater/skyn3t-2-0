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
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


# Writers are cross-process (the serve/cortex process persists on approval while
# a CLI build persists post-build against the same data_dir), so the lock must be
# an OS advisory lock, not a threading primitive. Bounded so a wedged holder can
# only stall a synchronous caller briefly.
_LOCK_TIMEOUT_S = 2.0
_LOCK_POLL_S = 0.02


@contextmanager
def overrides_file_lock(target: Path) -> Iterator[None]:
    """Best-effort exclusive advisory lock on a ``<target>.lock`` sidecar.

    Serializes the load->merge->write cycle of :func:`persist_overrides` (and
    the prompt store's twin) across processes: ``atomic_write_text`` only
    prevents torn files, not lost updates, so two unlocked writers that read
    the same baseline drop each other's keys (last replace wins). Degrade-
    don't-crash: any failure to create or acquire the lock yields WITHOUT it —
    the caller's write is then merely as racy as before locking, never blocked
    or broken.
    """
    handle = None
    locked = False
    try:
        lock_path = target.with_name(f"{target.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")  # create-if-missing, never truncate
        if handle.tell() == 0:
            # msvcrt.locking needs an existing byte at the locked offset.
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        if sys.platform == "win32":
            import msvcrt

            while not locked:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_LOCK_POLL_S)
        else:
            import fcntl

            while not locked:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_LOCK_POLL_S)
    except Exception:  # noqa: BLE001 - locking must never add a failure mode
        pass
    try:
        yield
    finally:
        if handle is not None:
            try:
                if locked:
                    if sys.platform == "win32":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001 - the OS releases locks on close
                pass
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass


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
        if not safe:
            return load_overrides(data_dir)
        p = overrides_path(data_dir)
        # The load must happen INSIDE the lock: a concurrent writer that also
        # read the pre-lock baseline would have its keys overwritten by this
        # merge. Lock acquired only once a write is certain, so an empty
        # persist stays a no-file no-op.
        with overrides_file_lock(p):
            current = load_overrides(data_dir)
            current.update(safe)
            atomic_write_text(p, json.dumps(current, indent=2))
        return current
    except Exception:  # noqa: BLE001
        return {}
