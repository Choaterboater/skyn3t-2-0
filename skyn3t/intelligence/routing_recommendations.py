"""Learned model router (2.0 P2).

Turns the :class:`ModelTournament` leaderboard into concrete routing advice and,
optionally, a drop-in replacement for :meth:`ModelRouter.resolve`.

Two layers:

  * :class:`RoutingRecommender` — reads the tournament and recommends a model id
    for a (tier, task_type), only once it has enough evidence; otherwise it
    abstains and falls back to the deterministic base router (safe-by-default,
    design rule #4; cheap-by-default until learning is confident, rule #5).
  * :class:`LearnedModelRouter` — subclasses :class:`ModelRouter`, overriding
    ``resolve`` to consult the recommender first, then the base policy. Honors
    ``free_only`` / ``no_claude`` because it only ever returns models the
    tournament has actually seen win (which themselves came from the router).

Gated by ``Settings.model_evolution`` / ``auto_route`` at the call site. Import
is side-effect-free and never requires a live model (design rule #4, #6).
"""

from __future__ import annotations

from typing import Any

try:
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - defensive
    _log = None  # type: ignore[assignment]

from skyn3t.core.model_router import ModelRouter, Tier


class RoutingRecommender:
    """Recommend a model id from tournament evidence, or abstain."""

    def __init__(
        self,
        tournament: Any,
        *,
        min_plays: int = 5,
        min_win_rate: float = 0.55,
    ) -> None:
        self.tournament = tournament
        self.min_plays = min_plays
        self.min_win_rate = min_win_rate

    def recommend(
        self, tier: Tier | str, task_type: str = ""
    ) -> str | None:
        """Return a model id if the evidence is strong enough, else ``None``."""
        bucket = self.tournament.bucket_key(tier, task_type)
        board = self.tournament.leaderboard(bucket, limit=1)
        if not board:
            # Fall back to a tier-only bucket if the task-specific one is empty.
            board = self.tournament.leaderboard(
                self.tournament.bucket_key(tier, ""), limit=1
            )
        if not board:
            return None
        top = board[0]
        if top.plays < self.min_plays or top.win_rate < self.min_win_rate:
            if _log:
                _log.debug(
                    "routing.abstain",
                    bucket=bucket,
                    plays=top.plays,
                    win_rate=round(top.win_rate, 3),
                )
            return None
        return top.model

    def explain(self, tier: Tier | str, task_type: str = "") -> dict[str, Any]:
        bucket = self.tournament.bucket_key(tier, task_type)
        return {
            "bucket": bucket,
            "recommendation": self.recommend(tier, task_type),
            "leaderboard": [
                {"model": s.model, "rating": round(s.rating, 1), "plays": s.plays, "win_rate": round(s.win_rate, 3)}
                for s in self.tournament.leaderboard(bucket)
            ],
        }


class LearnedModelRouter(ModelRouter):
    """Router that prefers learned recommendations over the static policy."""

    def __init__(
        self,
        recommender: RoutingRecommender,
        settings: Any | None = None,
    ) -> None:
        super().__init__(settings=settings)
        self.recommender = recommender

    def resolve(
        self,
        tier: Tier,
        file_hint: str | None = None,
        task_type: str = "",
        profile: str = "balanced",
    ) -> str:  # type: ignore[override]
        # Respect manual locks / free-only via the base policy first as the
        # canonical fallback, then *prefer* a confident learned pick.
        effective_tier = self._tier_for_file(tier, file_hint)
        base = super().resolve(tier, file_hint=file_hint, profile=profile)
        if self._has_explicit_tier_pin(effective_tier):
            return base
        try:
            learned = self.recommender.recommend(effective_tier, task_type)
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("routing.recommend_failed", error=str(exc))
            learned = None
        if not learned:
            return base
        # Safety: don't override into a Claude model if the policy forbade it.
        if self._forbidden(learned, effective_tier, profile):
            return base
        if _log:
            _log.info("routing.learned", tier=effective_tier.value, model=learned)
        return learned

    def _forbidden(self, model: str, tier: Tier, profile: str) -> bool:
        m = model.lower()
        no_claude = bool(getattr(self.settings, "no_claude", False))
        free_only = bool(getattr(self.settings, "free_only", False))
        if no_claude and ("claude" in m or "anthropic" in m):
            return True
        if free_only and not (m.endswith(":free") or "free" in m):
            return True
        if not self.auto_model_allowed(model, tier, profile):
            if _log:
                info = self.model_cost_info(model)
                _log.info(
                    "routing.learned_price_rejected",
                    model=model,
                    tier=tier.value,
                    profile=profile,
                    price_class=info.get("price_class"),
                    input_per_million=info.get("prompt_usd_per_million"),
                    output_per_million=info.get("completion_usd_per_million"),
                )
            return True
        return False

    def describe(self) -> dict[str, Any]:  # type: ignore[override]
        base = super().describe() if hasattr(super(), "describe") else {}
        out: dict[str, Any] = dict(base) if isinstance(base, dict) else {"base": base}
        out["learned"] = True
        return out
