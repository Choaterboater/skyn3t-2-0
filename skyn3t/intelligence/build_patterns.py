"""Build-pattern scoreboard — winning build *shapes* per stack (2.0 P2).

A "build shape" is the durable, reusable fingerprint of how a build was put
together: its stack, the model tier per stage, the high-level plan steps, etc.
We score each shape by the rolling outcome (mean score / win-rate) so the
studio can prefer shapes that have historically won for a given stack.

This is the scoreboard half of the population/archive evolutionary loop; the
generation/mutation half lives in the studio. Persistence is best-effort JSON
under ``data/`` and degrades to pure in-memory when disk is unavailable
(design rule #6). Import has zero side effects (design rule #4).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - defensive
    _log = None  # type: ignore[assignment]


def fingerprint(shape: dict[str, Any]) -> str:
    """Stable short id for a build shape (order-insensitive on keys)."""
    norm = json.dumps(shape, sort_keys=True, default=str)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class PatternRecord:
    fp: str
    stack: str
    shape: dict[str, Any]
    uses: int = 0
    wins: int = 0
    score_sum: float = 0.0
    last_used: float = 0.0
    examples: list[str] = field(default_factory=list)

    @property
    def mean_score(self) -> float:
        return self.score_sum / self.uses if self.uses else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.uses if self.uses else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mean_score"] = self.mean_score
        d["win_rate"] = self.win_rate
        return d


class BuildPatternBoard:
    """Rolling scoreboard of build shapes keyed by fingerprint."""

    WIN_SCORE = 80.0  # >= this counts as a win.

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self._records: dict[str, PatternRecord] = {}
        if self.path is not None:
            self._load()

    # ---- persistence (best-effort) ------------------------------------
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for fp, d in raw.items():
                self._records[fp] = PatternRecord(
                    fp=d["fp"],
                    stack=d.get("stack", "generic"),
                    shape=d.get("shape", {}),
                    uses=d.get("uses", 0),
                    wins=d.get("wins", 0),
                    score_sum=d.get("score_sum", 0.0),
                    last_used=d.get("last_used", 0.0),
                    examples=d.get("examples", []),
                )
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("patterns.load_failed", error=str(exc))

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {fp: r.to_dict() for fp, r in self._records.items()}
            # Atomic write: temp file in the same dir + os.replace, so a crash
            # mid-write can never truncate/corrupt the existing scoreboard and
            # silently wipe accumulated stats.
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, indent=2, default=str))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            return True
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("patterns.save_failed", error=str(exc))
            return False

    # ---- recording ----------------------------------------------------
    def record(
        self,
        stack: str,
        shape: dict[str, Any],
        score: float,
        *,
        example: str | None = None,
    ) -> PatternRecord:
        """Record one build outcome for a shape and update its rolling stats."""
        fp = fingerprint({"stack": stack, **shape})
        rec = self._records.get(fp)
        if rec is None:
            rec = PatternRecord(fp=fp, stack=stack, shape=dict(shape))
            self._records[fp] = rec
        rec.uses += 1
        rec.score_sum += float(score)
        rec.last_used = time.time()
        if float(score) >= self.WIN_SCORE:
            rec.wins += 1
        if example and example not in rec.examples:
            rec.examples.append(example)
            rec.examples = rec.examples[-10:]
        self.save()
        return rec

    # ---- querying -----------------------------------------------------
    def best_for(
        self, stack: str, *, min_uses: int = 1, limit: int = 3
    ) -> list[PatternRecord]:
        """Return the highest-scoring shapes for a stack, ranked.

        Ranked by mean score, then win-rate, then recency.
        """
        cands = [
            r
            for r in self._records.values()
            if r.stack == stack and r.uses >= min_uses
        ]
        cands.sort(key=lambda r: (r.mean_score, r.win_rate, r.last_used), reverse=True)
        return cands[:limit]

    def recommend(self, stack: str) -> dict[str, Any] | None:
        """Return the single best shape dict for a stack, or ``None``."""
        best = self.best_for(stack, limit=1)
        return best[0].shape if best else None

    def scoreboard(self, stack: str | None = None) -> list[dict[str, Any]]:
        recs: Iterable[PatternRecord] = self._records.values()
        if stack:
            recs = [r for r in recs if r.stack == stack]
        out = sorted(recs, key=lambda r: r.mean_score, reverse=True)
        return [r.to_dict() for r in out]
