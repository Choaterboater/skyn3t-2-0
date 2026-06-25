"""Token + USD cost tracking.

Reads from :class:`LLMClient.budget` (a ``BudgetTracker``) to surface live
spend, and mirrors it into Prometheus metrics. Also keeps a per-build ledger so
the cost of a single build can be attributed and reported in the manifest.

Pure in-memory; no I/O at import. Degrades to its own counters if no LLM client
is attached (rule #6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any

from skyn3t.config.settings import Settings, get_settings
from skyn3t.observability.metrics import MetricsRegistry, get_metrics


@dataclass
class CostSnapshot:
    spent_build_usd: float
    spent_day_usd: float
    tokens_day: int
    daily_cap_usd: float
    per_build_cap_usd: float
    token_cap: int

    @property
    def daily_remaining_usd(self) -> float:
        return max(0.0, self.daily_cap_usd - self.spent_day_usd)

    @property
    def build_remaining_usd(self) -> float:
        return max(0.0, self.per_build_cap_usd - self.spent_build_usd)

    @property
    def daily_fraction(self) -> float:
        return (self.spent_day_usd / self.daily_cap_usd) if self.daily_cap_usd else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "spent_build_usd": round(self.spent_build_usd, 6),
            "spent_day_usd": round(self.spent_day_usd, 6),
            "tokens_day": self.tokens_day,
            "daily_remaining_usd": round(self.daily_remaining_usd, 6),
            "build_remaining_usd": round(self.build_remaining_usd, 6),
            "daily_fraction": round(self.daily_fraction, 4),
        }


@dataclass
class CostTracker:
    """Attribution layer over the LLM ``BudgetTracker``."""

    settings: Settings = field(default_factory=get_settings)
    budget: Any | None = None  # an LLMClient.budget (BudgetTracker)
    metrics: MetricsRegistry = field(default_factory=get_metrics)
    # build_id -> {"cost_usd": float, "tokens": int, "started": float}
    _builds: dict[str, dict[str, Any]] = field(default_factory=dict)
    _last_seen_calls: int = 0
    # id() of LLMResult call records already attributed to a finished build, so
    # overlapping builds never double-count the same call.
    _claimed_call_ids: set[int] = field(default_factory=set)

    @classmethod
    def from_llm(cls, llm: Any, settings: Settings | None = None) -> CostTracker:
        return cls(settings=settings or get_settings(), budget=getattr(llm, "budget", None))

    def attach(self, llm: Any) -> None:
        self.budget = getattr(llm, "budget", None)

    # ---- ingestion -------------------------------------------------------
    def sync(self) -> None:
        """Pull any new LLM calls from the budget into Prometheus + ledger."""
        if self.budget is None:
            return
        calls = list(getattr(self.budget, "calls", []))
        new = calls[self._last_seen_calls:]
        for r in new:
            backend = getattr(r, "backend", "unknown")
            model = getattr(r, "model", "unknown")
            pt = getattr(r, "prompt_tokens", 0)
            ct = getattr(r, "completion_tokens", 0)
            cost = getattr(r, "cost_usd", 0.0)
            self.metrics.llm_calls_total.labels(backend, model).inc()
            self.metrics.llm_tokens_total.labels("prompt").inc(pt)
            self.metrics.llm_tokens_total.labels("completion").inc(ct)
            self.metrics.llm_cost_usd.inc(cost)
        self._last_seen_calls = len(calls)
        snap = self.snapshot()
        self.metrics.budget_remaining.set(snap.daily_remaining_usd)

    # ---- build attribution ----------------------------------------------
    def start_build(self, build_id: str) -> None:
        # Reset the per-build spend counter so the per-build USD cap applies to
        # THIS build, not cumulatively across a long-lived (web) process.
        reset = getattr(self.budget, "reset_build", None)
        if callable(reset):
            reset()
        # Snapshot the call ledger length at start. Cost is attributed by
        # summing the individual LLM call records appended *after* this point
        # (and not already claimed by another build), so concurrent/overlapping
        # builds do not double-count via a shared daily counter delta.
        base_calls = len(getattr(self.budget, "calls", [])) if self.budget else 0
        self._builds[build_id] = {
            "started": time(), "base_calls": base_calls,
            "cost_usd": 0.0, "tokens": 0,
        }

    # ---- per-stage attribution (Spec 2 "wasted tokens") ------------------
    # Slices the same call ledger at stage boundaries to show WHERE a build's
    # tokens go. Read-only — it never claims calls, so build-level attribution
    # (and overlapping builds) are unaffected. Sequential stages => disjoint
    # slices that partition the build's spend.
    def start_stage(self, build_id: str, stage: str) -> None:
        entry = self._builds.get(build_id)
        if entry is None:
            return
        base = len(getattr(self.budget, "calls", [])) if self.budget else 0
        entry.setdefault("_stage_base", {})[stage] = base

    def end_stage(self, build_id: str, stage: str) -> dict[str, Any]:
        self.sync()
        entry = self._builds.get(build_id)
        if entry is None:
            return {"stage": stage, "cost_usd": 0.0, "tokens": 0}
        # Consume the base (pop, not get) so each start/end is paired — robust
        # even if a stage name repeats across iterations. An unpaired/duplicate
        # end_stage (no recorded start boundary) must attribute NOTHING rather
        # than fall back to build-start, which would re-count every prior stage.
        stage_base = entry.get("_stage_base", {})
        if stage not in stage_base:
            return {"stage": stage, "cost_usd": 0.0, "tokens": 0}
        base = stage_base.pop(stage)
        calls = list(getattr(self.budget, "calls", [])) if self.budget else []
        cost = 0.0
        tokens = 0
        for r in calls[base:]:
            cost += getattr(r, "cost_usd", 0.0)
            tokens += getattr(r, "prompt_tokens", 0) + getattr(r, "completion_tokens", 0)
        rec = {"stage": stage, "cost_usd": round(max(0.0, cost), 6), "tokens": max(0, tokens)}
        entry.setdefault("stages", []).append(rec)
        return rec

    def end_build(self, build_id: str) -> dict[str, Any]:
        self.sync()
        entry = self._builds.get(build_id)
        if entry is None:
            return {"build_id": build_id, "cost_usd": 0.0, "tokens": 0, "stages": []}
        calls = list(getattr(self.budget, "calls", [])) if self.budget else []
        # Attribute only the calls recorded during this build's lifetime that
        # have not already been claimed by an earlier-finishing overlapping
        # build. Each call is owned by exactly one build.
        cost = 0.0
        tokens = 0
        for r in calls[entry["base_calls"]:]:
            rid = id(r)
            if rid in self._claimed_call_ids:
                continue
            self._claimed_call_ids.add(rid)
            cost += getattr(r, "cost_usd", 0.0)
            tokens += getattr(r, "prompt_tokens", 0) + getattr(r, "completion_tokens", 0)
        entry["cost_usd"] = round(max(0.0, cost), 6)
        entry["tokens"] = max(0, tokens)
        entry["duration_s"] = round(time() - entry["started"], 3)
        return {"build_id": build_id, "stages": list(entry.get("stages", [])),
                **{k: entry[k] for k in ("cost_usd", "tokens", "duration_s")}}

    # ---- snapshot --------------------------------------------------------
    def snapshot(self) -> CostSnapshot:
        b = self.budget
        return CostSnapshot(
            spent_build_usd=getattr(b, "spent_build", 0.0) if b else 0.0,
            spent_day_usd=getattr(b, "spent_day", 0.0) if b else 0.0,
            tokens_day=getattr(b, "tokens_day", 0) if b else 0,
            daily_cap_usd=self.settings.daily_usd_cap,
            per_build_cap_usd=self.settings.per_build_usd_cap,
            token_cap=self.settings.daily_token_cap,
        )

    def report(self) -> dict[str, Any]:
        self.sync()
        return self.snapshot().to_dict()
