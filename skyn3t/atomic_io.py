"""Atomic text writes.

A direct ``Path.write_text`` can leave a truncated/corrupt file if the process
crashes mid-write. ``atomic_write_text`` writes to a temp file in the same
directory, fsyncs it, then ``os.replace``s it over the target — an atomic swap
on POSIX — so a reader never sees a partial file. Used for durable JSON state
(build manifests, tuning store, tournament/bench ledgers). Import is side-effect
free.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _atomic_write(path: str | Path, content: str | bytes, *, binary: bool) -> Path:
    """Write content atomically (temp + fsync + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        mode = "wb" if binary else "w"
        kwargs = {} if binary else {"encoding": "utf-8"}
        with os.fdopen(fd, mode, **kwargs) as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Write UTF-8 ``text`` atomically (temp + fsync + rename)."""
    return _atomic_write(path, text, binary=False)


def atomic_write_bytes(path: str | Path, content: bytes) -> Path:
    """Write exact ``content`` atomically without text newline conversion."""
    return _atomic_write(path, content, binary=True)
