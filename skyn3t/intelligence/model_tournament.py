"""Model tournament — record per-task model wins (2.0 P2 learned router input).

Every time two or more models produce candidates for the same task, the winner
is recorded here. Over time this builds a per-(tier, task-type) leaderboard that
feeds both the learned router (:mod:`routing_recommendations`) and the debate
synthesiser (:mod:`debate`).

Scoring uses a lightweight rolling Elo plus raw win counts so the ranking is
stable with few samples and responsive with many. Persistence is best-effort
JSON; degrades to memory-only. Zero import side effects (design rule #4).

**Feeding (swarm #16 — now closed)**:
``record_win`` is fed from two places:
  * the debate pipeline (:func:`skyn3t.intelligence.debate.run_debate`), and
  * **every successful build stage** — :class:`~skyn3t.core.agent.TaskResult`
    carries a ``model_id`` (stamped automatically by :class:`BaseAgent` from its
    ``llm`` client), and :meth:`~skyn3t.studio.runner.StudioRunner._feed_tournament`
    records a solo appearance into the same ``(tier, task_type)`` bucket the
    :class:`LearnedModelRouter` queries.

A *solo appearance* (``losers=[]``) is the normal single-model build case: it
counts as one unopposed play+win (see :meth:`record_win`), so the win/plays
counters accrue over real traffic. After a few dozen stages a model can clear
:class:`RoutingRecommender`'s ``min_plays``/``min_win_rate`` thresholds and the
learned router (``Settings.model_evolution`` + ``auto_route``) starts routing to
it; until then it abstains and the deterministic base router is used (safe).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
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
        # Buckets we've already logged as empty, so an abstaining router querying
        # the same empty bucket on every build doesn't spam the debug log.
        self._warned_empty: set[str] = set()
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
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - unreadable/corrupt file
            if _log:
                _log.warning("tournament.load_failed", error=str(exc))
            return
        # Construct per-record and DROP unknown keys, so one drifted / legacy /
        # forward-compatible field can't wipe the WHOLE leaderboard (slots
        # dataclasses raise TypeError on an unexpected kwarg). A single corrupt
        # record is skipped, not all of them.
        stat_fields = {f.name for f in fields(ModelStats)}
        match_fields = {f.name for f in fields(MatchRecord)}
        for bucket, models in raw.get("boards", {}).items():
            board: dict[str, ModelStats] = {}
            for m, d in models.items():
                try:
                    board[m] = ModelStats(**{k: v for k, v in d.items() if k in stat_fields})
                except Exception:  # noqa: BLE001 - skip one corrupt record
                    continue
            if board:
                self._boards[bucket] = board
        matches: list[MatchRecord] = []
        for r in raw.get("matches", []):
            try:
                matches.append(MatchRecord(**{k: v for k, v in r.items() if k in match_fields}))
            except Exception:  # noqa: BLE001
                continue
        self._matches = matches[-500:]

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
    # Competitor ids are already unambiguous across providers without extra
    # namespacing: a CLI result is labelled ``<provider>-cli[:model]`` by
    # ``LLMClient._cli`` and an OpenRouter result is a ``vendor/model`` id, so
    # the two families cannot collide. Multi-provider fan-out therefore needs no
    # change here — the distinct labels arrive on their own.
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
        save: bool = True,
    ) -> None:
        """Record that ``winner`` beat each model in ``losers`` for ``bucket``.

        ``save=False`` defers the disk write so a caller recording several
        buckets in one pass (a build stage with multiple routes) can batch them
        and call :meth:`save` once instead of writing per record.
        """
        now = time.time()
        w = self._stat(bucket, winner)
        real_losers = [loser for loser in losers if loser != winner]
        for loser in real_losers:
            loser_stats = self._stat(bucket, loser)
            exp_w = _expected(w.rating, loser_stats.rating)
            exp_l = _expected(loser_stats.rating, w.rating)
            w.rating += _K * (1.0 - exp_w)
            loser_stats.rating += _K * (0.0 - exp_l)
            w.wins += 1
            loser_stats.losses += 1
            w.plays += 1
            loser_stats.plays += 1
            w.last_seen = loser_stats.last_seen = now
        if not real_losers:
            # Solo appearance — the normal single-model build case. Count it as
            # one unopposed play+win (no opponent → no Elo exchange) so evidence
            # accrues over real traffic; otherwise the learned router could never
            # reach ``min_plays`` from ordinary builds (closes swarm #16).
            w.wins += 1
            w.plays += 1
            w.last_seen = now
        self._matches.append(
            MatchRecord(bucket=bucket, winner=winner, losers=list(losers), task_type=task_type)
        )
        if save:
            self.save()

    # ---- querying -----------------------------------------------------
    def has_data(self) -> bool:
        """Return True if any model has been recorded in any bucket.

        The learned router uses this to decide whether to consult the
        tournament or fall back to the default heuristic. Fed per build stage
        (StudioRunner._feed_tournament) and by the debate pipeline.
        """
        return bool(self._boards)

    def leaderboard(self, bucket: str, limit: int = 10) -> list[ModelStats]:
        board = self._boards.get(bucket, {})
        if not board and _log and bucket not in self._warned_empty:
            # Once per bucket per process — the router abstains and re-queries the
            # same empty bucket on every build, which used to spam this log.
            self._warned_empty.add(bucket)
            _log.debug(
                "tournament.empty_bucket",
                bucket=bucket,
                note=(
                    "No tournament evidence for this bucket yet; the learned "
                    "router abstains here until builds feed it (fed per build "
                    "stage via StudioRunner._feed_tournament, and by debate)."
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
