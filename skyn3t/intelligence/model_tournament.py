"""Model tournament — record per-task model wins (2.0 P2 learned router input).

Every time two or more models produce candidates for the same task, the winner
is recorded here. Over time this builds a per-(tier, task-type) leaderboard that
feeds both the learned router (:mod:`routing_recommendations`) and the debate
synthesiser (:mod:`debate`).

Scoring uses a lightweight rolling Elo plus raw win counts so the ranking is
stable with few samples and responsive with many. Persistence is best-effort
JSON; degrades to memory-only. Zero import side effects (design rule #4).

**Current feeding limitation (swarm #16)**:
``record_win`` is currently called ONLY by the debate pipeline
(:func:`skyn3t.intelligence.debate.run_debate`). The debate command is
rarely invoked in normal operation, so the leaderboard stays sparse and the
learned router (``Settings.model_evolution=True``) will almost always abstain
(see :meth:`RoutingRecommender.recommend` — it returns ``None`` with < 1
play). The router's fallback (keyword → tier → default model) remains safe.

**The seam to close this gap**:
Add a ``model_id: str | None`` field to :class:`~skyn3t.core.agent.TaskResult`
so agents can surface which LLM model they used. Then, in
:class:`~skyn3t.studio.runner.StudioRunner`, after a stage completes
successfully, call ``tournament.record_win(bucket, result.model_id, losers=[])``
where ``losers=[]`` records a solo appearance (no direct competitor) and still
builds up the win/plays counters over real build traffic. Once a few dozen
stages have run, the router has enough signal to start routing.

Until that wiring lands, the tournament only learns from debate runs.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - defensive
    _log = None  # type: ignore[assignment]


_K = 24.0  # Elo K-factor.
_BASE = 1000.0


def _expected(a: float, b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((b - a) / 400.0))


@dataclass(slots=True)
class ModelStats:
    model: str
    rating: float = _BASE
    wins: int = 0
    losses: int = 0
    plays: int = 0
    last_seen: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.plays if self.plays else 0.0


@dataclass(slots=True)
class MatchRecord:
    bucket: str
    winner: str
    losers: list[str]
    task_type: str = ""
    ts: float = field(default_factory=time.time)


class ModelTournament:
    """Per-bucket model leaderboard with Elo + win counts.

    A *bucket* is usually ``"<tier>:<task_type>"`` so routing can be learned
    per stage. Use the convenience ``bucket_key`` helper.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        # bucket -> {model -> ModelStats}
        self._boards: dict[str, dict[str, ModelStats]] = {}
        self._matches: list[MatchRecord] = []
        if self.path is not None:
            self._load()

    @staticmethod
    def bucket_key(tier: Any = "", task_type: str = "") -> str:
        tier_s = getattr(tier, "value", tier) or ""
        return f"{tier_s}:{task_type}".strip(":") or "default"

    # ---- persistence --------------------------------------------------
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            for bucket, models in raw.get("boards", {}).items():
                self._boards[bucket] = {
                    m: ModelStats(**d) for m, d in models.items()
                }
            self._matches = [MatchRecord(**r) for r in raw.get("matches", [])][-500:]
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("tournament.load_failed", error=str(exc))

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "boards": {
                    b: {m: asdict(s) for m, s in board.items()}
                    for b, board in self._boards.items()
                },
                "matches": [asdict(r) for r in self._matches[-500:]],
            }
            from skyn3t.atomic_io import atomic_write_text
            atomic_write_text(self.path, json.dumps(payload, indent=2))
            return True
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("tournament.save_failed", error=str(exc))
            return False

    # ---- recording ----------------------------------------------------
    def _stat(self, bucket: str, model: str) -> ModelStats:
        board = self._boards.setdefault(bucket, {})
        if model not in board:
            board[model] = ModelStats(model=model)
        return board[model]

    def record_win(
        self,
        bucket: str,
        winner: str,
        losers: list[str],
        *,
        task_type: str = "",
    ) -> None:
        """Record that ``winner`` beat each model in ``losers`` for ``bucket``."""
        now = time.time()
        w = self._stat(bucket, winner)
        for loser in losers:
            if loser == winner:
                continue
            l = self._stat(bucket, loser)
            exp_w = _expected(w.rating, l.rating)
            exp_l = _expected(l.rating, w.rating)
            w.rating += _K * (1.0 - exp_w)
            l.rating += _K * (0.0 - exp_l)
            w.wins += 1
            l.losses += 1
            w.plays += 1
            l.plays += 1
            w.last_seen = l.last_seen = now
        self._matches.append(
            MatchRecord(bucket=bucket, winner=winner, losers=list(losers), task_type=task_type)
        )
        self.save()

    # ---- querying -----------------------------------------------------
    def has_data(self) -> bool:
        """Return True if any model has been recorded in any bucket.

        The learned router uses this to decide whether to consult the
        tournament or fall back to the default heuristic. Currently the
        tournament is only fed by the debate pipeline (see module docstring
        for the seam that would close this gap).
        """
        return bool(self._boards)

    def leaderboard(self, bucket: str, limit: int = 10) -> list[ModelStats]:
        board = self._boards.get(bucket, {})
        if not board and _log:
            _log.debug(
                "tournament.empty_bucket",
                bucket=bucket,
                note=(
                    "Tournament has no data for this bucket. "
                    "Currently only fed by debate runs — see module docstring "
                    "(swarm #16) for the seam to wire real build-stage outcomes."
                ),
            )
        ranked = sorted(board.values(), key=lambda s: s.rating, reverse=True)
        return ranked[:limit]

    def champion(self, bucket: str, *, min_plays: int = 1) -> str | None:
        ranked = [
            s for s in self.leaderboard(bucket, limit=50) if s.plays >= min_plays
        ]
        return ranked[0].model if ranked else None

    def buckets(self) -> list[str]:
        return list(self._boards.keys())

    def snapshot(self) -> dict[str, Any]:
        return {
            b: [
                {"model": s.model, "rating": round(s.rating, 1), "win_rate": round(s.win_rate, 3), "plays": s.plays}
                for s in self.leaderboard(b)
            ]
            for b in self._boards
        }
