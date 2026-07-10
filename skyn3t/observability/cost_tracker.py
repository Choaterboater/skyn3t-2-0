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

_MISSING_BUILD_ID = object()


@dataclass
class CostSnapshot:
    spent_build_usd: float
    spent_day_usd: float
    tokens_day: int
    daily_cap_usd: float
    per_build_cap_usd: float
    token_cap: int

    @property
    def daily_remaining_usd(self) -> float | None:
        if self.daily_cap_usd <= 0:
            return None
        return max(0.0, self.daily_cap_usd - self.spent_day_usd)

    @property
    def build_remaining_usd(self) -> float | None:
        if self.per_build_cap_usd <= 0:
            return None
        return max(0.0, self.per_build_cap_usd - self.spent_build_usd)

    @property
    def daily_fraction(self) -> float:
        return (self.spent_day_usd / self.daily_cap_usd) if self.daily_cap_usd else 0.0

    def to_dict(self) -> dict[str, Any]:
        daily_remaining = self.daily_remaining_usd
        build_remaining = self.build_remaining_usd
        return {
            "spent_build_usd": round(self.spent_build_usd, 6),
            "spent_day_usd": round(self.spent_day_usd, 6),
            "tokens_day": self.tokens_day,
            "daily_cap_enabled": self.daily_cap_usd > 0,
            "build_cap_enabled": self.per_build_cap_usd > 0,
            "token_cap_enabled": self.token_cap > 0,
            "daily_remaining_usd": (
                round(daily_remaining, 6) if daily_remaining is not None else None
            ),
            "build_remaining_usd": (
                round(build_remaining, 6) if build_remaining is not None else None
            ),
            "daily_fraction": round(self.daily_fraction, 4),
        }


@dataclass
class CostTracker:
    """Attribution layer over the LLM ``BudgetTracker``."""

    settings: Settings = field(default_factory=get_settings)
    budget: Any | None = None  # an LLMClient.budget (BudgetTracker)
    llm: Any | None = None
    metrics: MetricsRegistry = field(default_factory=get_metrics)
    # build_id -> {"cost_usd": float, "tokens": int, "started": float}
    _builds: dict[str, dict[str, Any]] = field(default_factory=dict)
    _last_seen_calls: int = 0
    # id() of LLMResult call records already attributed to a finished build, so
    # overlapping builds never double-count the same call.
    _claimed_call_ids: set[int] = field(default_factory=set)
    # Idempotent terminal reports. Finalization/persistence can fail after cost
    # settlement and enter another exception path; a repeated end_build must
    # return the original spend instead of fabricating a zero-cost failure.
    _finished_builds: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_llm(cls, llm: Any, settings: Settings | None = None) -> CostTracker:
        return cls(
            settings=settings or get_settings(),
            budget=getattr(llm, "budget", None),
            llm=llm,
        )

    def attach(self, llm: Any) -> None:
        self.llm = llm
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
        remaining = snap.daily_remaining_usd
        self.metrics.budget_remaining.set(
            remaining if remaining is not None else float("inf")
        )

    # ---- build attribution ----------------------------------------------
    def start_build(self, build_id: str) -> None:
        self._finished_builds.pop(build_id, None)
        # A BudgetTracker build context is isolated from overlapping builds but
        # deliberately shared with child asyncio tasks created by this build.
        begin = getattr(self.budget, "begin_build", None)
        budget_token = begin(build_id) if callable(begin) else None
        if budget_token is None:
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
            "cost_usd": 0.0, "tokens": 0, "_budget_token": budget_token,
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
            return {
                "stage": stage,
                "cost_usd": 0.0,
                "tokens": 0,
                "call_count": 0,
                "backend": "none",
                "backends": {},
                "models": [],
                "cost_source_counts": {},
                "cost_usd_known": True,
                "cost_classification": "none",
            }
        # Consume the base (pop, not get) so each start/end is paired — robust
        # even if a stage name repeats across iterations. An unpaired/duplicate
        # end_stage (no recorded start boundary) must attribute NOTHING rather
        # than fall back to build-start, which would re-count every prior stage.
        stage_base = entry.get("_stage_base", {})
        if stage not in stage_base:
            return {
                "stage": stage,
                "cost_usd": 0.0,
                "tokens": 0,
                "call_count": 0,
                "backend": "none",
                "backends": {},
                "models": [],
                "cost_source_counts": {},
                "cost_usd_known": True,
                "cost_classification": "none",
            }
        base = stage_base.pop(stage)
        calls = list(getattr(self.budget, "calls", [])) if self.budget else []
        cost = 0.0
        tokens = 0
        exposure = 0.0
        failed_attempts = 0
        call_count = 0
        backends: dict[str, int] = {}
        models: list[str] = []
        cost_source_counts: dict[str, int] = {}
        for r in calls[base:]:
            owner = getattr(r, "build_id", _MISSING_BUILD_ID)
            if owner is not _MISSING_BUILD_ID and owner != build_id:
                continue
            call_count += 1
            cost += getattr(r, "cost_usd", 0.0)
            tokens += getattr(r, "prompt_tokens", 0) + getattr(r, "completion_tokens", 0)
            exposure += getattr(r, "estimated_exposure_usd", 0.0)
            if str(getattr(r, "status", "")).startswith("failed_"):
                failed_attempts += 1
            backend = str(getattr(r, "backend", "") or "unknown")
            backends[backend] = backends.get(backend, 0) + 1
            model = str(getattr(r, "model", "") or "")
            if model and model not in models and len(models) < 16:
                models.append(model)
            source = str(getattr(r, "cost_source", "") or "unknown")
            if source == "none" and backend == "stub":
                source = "offline_stub"
            cost_source_counts[source] = cost_source_counts.get(source, 0) + 1

        # A local CLI deliberately reports a $0 placeholder because the CLI
        # owns billing. Keep that amount separate from whether it is KNOWN, so
        # a stage can never be read as free merely because SkyN3t lacks a dollar
        # receipt. Catalog/flat provider prices are estimates, not confirmations.
        unknown_sources = {"unknown", "unconfirmed", "not_reported_by_cli"}
        estimate_sources = {"catalog_estimate", "flat_estimate"}
        source_names = set(cost_source_counts)
        if not call_count:
            cost_classification = "none"
            cost_usd_known = True
        elif source_names & unknown_sources:
            cost_classification = "partial" if len(source_names) > 1 else "unknown"
            cost_usd_known = False
        elif source_names & estimate_sources:
            cost_classification = "mixed" if len(source_names) > 1 else "estimate"
            cost_usd_known = False
        elif source_names <= {"offline_stub"}:
            cost_classification = "offline"
            cost_usd_known = True
        else:
            cost_classification = "provider_confirmed"
            cost_usd_known = True

        backend = (
            next(iter(backends))
            if len(backends) == 1
            else "mixed" if backends else "none"
        )
        rec = {
            "stage": stage,
            "cost_usd": round(max(0.0, cost), 6),
            "tokens": max(0, tokens),
            "failed_attempts": failed_attempts,
            "max_unconfirmed_exposure_usd": round(max(0.0, exposure), 6),
            "call_count": call_count,
            "backend": backend,
            "backends": dict(sorted(backends.items())),
            "models": models,
            "cost_source_counts": dict(sorted(cost_source_counts.items())),
            "cost_usd_known": cost_usd_known,
            "cost_classification": cost_classification,
        }
        entry.setdefault("stages", []).append(rec)
        return rec

    def end_build(self, build_id: str) -> dict[str, Any]:
        self.sync()
        entry = self._builds.pop(build_id, None)
        if entry is None:
            finished = self._finished_builds.get(build_id)
            if finished is not None:
                return {
                    **finished,
                    "stages": [dict(stage) for stage in finished.get("stages", [])],
                    "usage_evidence": [
                        dict(item) for item in finished.get("usage_evidence", [])
                    ],
                    "usage_evidence_source_counts": dict(
                        finished.get("usage_evidence_source_counts", {})
                    ),
                }
            return {"build_id": build_id, "cost_usd": 0.0, "tokens": 0, "stages": []}
        calls = list(getattr(self.budget, "calls", [])) if self.budget else []
        # BudgetTracker records carry an owning build id, so completion order
        # cannot make one overlapping build steal another's calls. Untagged
        # records retain the legacy first-claimer behavior for fake/old budgets.
        cost = 0.0
        tokens = 0
        exposure = 0.0
        failed_attempts = 0
        usage_evidence: list[dict[str, Any]] = []
        usage_evidence_source_counts: dict[str, int] = {}
        for r in calls[entry["base_calls"]:]:
            owner = getattr(r, "build_id", _MISSING_BUILD_ID)
            if owner is not _MISSING_BUILD_ID:
                if owner != build_id:
                    continue
            else:
                rid = id(r)
                if rid in self._claimed_call_ids:
                    continue
                self._claimed_call_ids.add(rid)
            cost += getattr(r, "cost_usd", 0.0)
            tokens += getattr(r, "prompt_tokens", 0) + getattr(r, "completion_tokens", 0)
            exposure += getattr(r, "estimated_exposure_usd", 0.0)
            if str(getattr(r, "status", "")).startswith("failed_"):
                failed_attempts += 1
            generation_id = str(getattr(r, "generation_id", "") or "")
            status = str(getattr(r, "status", "") or "succeeded")
            backend = str(getattr(r, "backend", "") or "unknown")
            if (
                backend == "openrouter"
                or backend.endswith("_cli")
                or generation_id
                or status.startswith("failed_")
                or status == "malformed_response"
            ):
                cost_source = str(getattr(r, "cost_source", "") or "unknown")
                evidence = {
                    "generation_id": generation_id or None,
                    "model": str(getattr(r, "model", "") or "unknown"),
                    "backend": backend,
                    "status": status,
                    "cost_usd": round(max(0.0, float(getattr(r, "cost_usd", 0.0))), 8),
                    "cost_source": cost_source,
                    "max_unconfirmed_exposure_usd": round(
                        max(0.0, float(getattr(r, "estimated_exposure_usd", 0.0))),
                        8,
                    ),
                }
                if cost_source == "not_reported_by_cli":
                    evidence["cost_usd"] = None
                    evidence["cost_usd_known"] = False
                usage_evidence.append(evidence)
                usage_evidence_source_counts[cost_source] = (
                    usage_evidence_source_counts.get(cost_source, 0) + 1
                )
        entry["cost_usd"] = round(max(0.0, cost), 6)
        entry["tokens"] = max(0, tokens)
        entry["failed_attempts"] = failed_attempts
        entry["max_unconfirmed_exposure_usd"] = round(max(0.0, exposure), 6)
        entry["usage_evidence"] = usage_evidence[-256:]
        entry["usage_evidence_truncated"] = max(0, len(usage_evidence) - 256)
        entry["usage_evidence_source_counts"] = dict(
            sorted(usage_evidence_source_counts.items())
        )
        entry["duration_s"] = round(time() - entry["started"], 3)
        token = entry.pop("_budget_token", None)
        finish = getattr(self.budget, "end_build", None)
        if token is not None and callable(finish):
            finish(token)
        report = {"build_id": build_id, "stages": list(entry.get("stages", [])),
                  **{
                      k: entry[k]
                      for k in (
                          "cost_usd",
                          "tokens",
                          "duration_s",
                          "failed_attempts",
                          "max_unconfirmed_exposure_usd",
                          "usage_evidence",
                          "usage_evidence_truncated",
                          "usage_evidence_source_counts",
                      )
                  }}
        self._finished_builds[build_id] = report
        while len(self._finished_builds) > 256:
            self._finished_builds.pop(next(iter(self._finished_builds)))
        return {
            **report,
            "stages": [dict(stage) for stage in report["stages"]],
            "usage_evidence": [dict(item) for item in report["usage_evidence"]],
            "usage_evidence_source_counts": dict(
                report["usage_evidence_source_counts"]
            ),
        }

    def close_build(self, build_id: str) -> None:
        """Release a build context after a failed/cancelled runner exit."""
        if build_id in self._builds:
            self.end_build(build_id)

    # ---- snapshot --------------------------------------------------------
    def snapshot(self) -> CostSnapshot:
        b = self.budget
        current_spend = getattr(b, "current_build_spend", None) if b else None
        return CostSnapshot(
            spent_build_usd=(
                current_spend() if callable(current_spend)
                else getattr(b, "spent_build", 0.0) if b else 0.0
            ),
            spent_day_usd=getattr(b, "spent_day", 0.0) if b else 0.0,
            tokens_day=getattr(b, "tokens_day", 0) if b else 0,
            daily_cap_usd=self.settings.daily_usd_cap,
            per_build_cap_usd=self.settings.per_build_usd_cap,
            token_cap=self.settings.daily_token_cap,
        )

    def report(self) -> dict[str, Any]:
        self.sync()
        return self.snapshot().to_dict()
