"""Append-only audit log.

Every security-relevant action (sandbox exec, permission decision, secret
access, approval) is recorded as a hash-chained JSONL line. The chain makes
tampering detectable: each record carries the SHA-256 of the previous record,
so removing or editing a line breaks ``verify``.

Import is side-effect free — the log file is created lazily on first write.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Any

from skyn3t.config.settings import get_settings
from skyn3t.security.secrets import SecretsStore, scrub_text

_GENESIS = "0" * 64


def _hash_record(prev_hash: str, body: dict[str, Any]) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


@dataclass
class AuditLog:
    """Hash-chained append-only audit trail (JSONL).

    Thread-safe append. Records are scrubbed of known secret values before
    being written (defence in depth).
    """

    path: Path | None = None
    secrets: SecretsStore | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_hash: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = get_settings().logs_dir / "audit.jsonl"
        self.path = Path(self.path)

    @property
    def _path(self) -> Path:
        if self.path is None:
            self.path = get_settings().logs_dir / "audit.jsonl"
        return Path(self.path)

    # ---- internals -------------------------------------------------------
    def _tail_hash(self) -> str:
        if self._last_hash is not None:
            return self._last_hash
        path = self._path
        if not path.exists():
            self._last_hash = _GENESIS
            return self._last_hash
        last = _GENESIS
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        last = rec.get("hash", last)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            last = _GENESIS
        self._last_hash = last
        return last

    # ---- public api ------------------------------------------------------
    def record(
        self,
        action: str,
        *,
        actor: str = "system",
        outcome: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one audit record and return it (with its chain hash)."""
        body = {
            "ts": time(),
            "action": action,
            "actor": actor,
            "outcome": outcome,
            "detail": detail or {},
            "pid": os.getpid(),
        }
        # Scrub any secret values that leaked into the detail dict.
        raw = json.dumps(body, sort_keys=True, default=str)
        scrubbed = scrub_text(raw, self.secrets)
        body = json.loads(scrubbed)
        with self._lock:
            prev = self._tail_hash()
            digest = _hash_record(prev, body)
            record = {**body, "prev": prev, "hash": digest}
            path = self._path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
            self._last_hash = digest
        return record

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        path = self._path
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if limit is not None:
            out = out[-limit:]
        return out

    def verify(self) -> bool:
        """Return True if the hash chain is intact (no tampering detected)."""
        prev = _GENESIS
        for rec in self.read():
            body = {k: rec[k] for k in ("ts", "action", "actor", "outcome", "detail", "pid") if k in rec}
            expected = _hash_record(prev, body)
            if rec.get("prev") != prev or rec.get("hash") != expected:
                return False
            prev = rec["hash"]
        return True
