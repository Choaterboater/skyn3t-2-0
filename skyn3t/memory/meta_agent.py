"""MetaAgent — observes system trends and proposes improvement hypotheses.

The MetaAgent reads recent tasks/builds from the MemoryStore (and, optionally,
live counts from the EventBus history) to detect trends — rising failure rates,
expensive builds, chronically failing stages — and emits ``INSIGHT_PUBLISHED``
events carrying actionable hypotheses. Those insights can be consumed by the
SelfTuningEngine, the Cortex, or a human dashboard.

It is read-only with respect to system state: it never mutates the store or
agent configs; it only publishes events (design rule #7). All store access is
guarded so a missing/empty store degrades to "no insights" rather than crashing
(design rule #6).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from skyn3t.core.events import EventBus, EventType


@dataclass(slots=True)
class Hypothesis:
    """A structured improvement suggestion derived from observed trends."""

    title: str
    rationale: str
    suggestion: dict[str, Any] = field(default_factory=dict)
    severity: str = "info"  # info | warn | critical
    confidence: float = 0.5
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "rationale": self.rationale,
            "suggestion": self.suggestion,
            "severity": self.severity,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "created_at": self.created_at,
        }


class MetaAgent:
    """Trend observer + hypothesis generator."""

    def __init__(
        self,
        event_bus: EventBus,
        store: Any | None = None,
        *,
        failure_rate_threshold: float = 0.4,
        cost_threshold_usd: float = 1.0,
        min_samples: int = 5,
    ) -> None:
        self.event_bus = event_bus
        self.store = store
        self.failure_rate_threshold = float(failure_rate_threshold)
        self.cost_threshold_usd = float(cost_threshold_usd)
        self.min_samples = int(min_samples)

    # ---- analysis --------------------------------------------------------
    async def analyze(self, task_limit: int = 100, build_limit: int = 50) -> list[Hypothesis]:
        """Inspect recent history and return improvement hypotheses (no emit)."""
        if self.store is None:
            tasks: list[Any] = []
            builds: list[Any] = []
        else:
            tasks = await self._safe(lambda: self.store.recent_tasks(limit=task_limit)) or []
            builds = await self._safe(lambda: self.store.recent_builds(limit=build_limit)) or []

        hyps: list[Hypothesis] = []
        hyps.extend(self._task_failure_hypotheses(tasks))
        hyps.extend(self._build_quality_hypotheses(builds))
        hyps.extend(self._cost_hypotheses(builds))
        return hyps

    async def observe_and_publish(self, **kwargs: Any) -> list[Hypothesis]:
        """Analyze and emit one INSIGHT_PUBLISHED event per hypothesis."""
        hyps = await self.analyze(**kwargs)
        for h in hyps:
            try:
                await self.event_bus.emit(
                    EventType.INSIGHT_PUBLISHED, "meta_agent", h.to_dict()
                )
            except Exception:  # degrade, don't crash
                pass
        return hyps

    # ---- per-dimension trend detectors -----------------------------------
    def _task_failure_hypotheses(self, tasks: list[dict[str, Any]]) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        if len(tasks) < self.min_samples:
            return out
        # Group by task type and by agent.
        by_type: dict[str, list[bool]] = {}
        by_agent: dict[str, list[bool]] = {}
        for t in tasks:
            ok = bool(t.get("success"))
            by_type.setdefault(t.get("type", "?"), []).append(ok)
            agent = t.get("agent") or t.get("agent_name")
            if agent:
                by_agent.setdefault(agent, []).append(ok)

        for ttype, results in by_type.items():
            if len(results) < self.min_samples:
                continue
            fail_rate = 1.0 - (sum(results) / len(results))
            if fail_rate >= self.failure_rate_threshold:
                out.append(Hypothesis(
                    title=f"Stage '{ttype}' is failing frequently",
                    rationale=f"{fail_rate:.0%} of recent '{ttype}' tasks failed.",
                    suggestion={"target": "stage", "stage": ttype, "action": "increase_retries_or_swap_model"},
                    severity="critical" if fail_rate >= 0.6 else "warn",
                    confidence=min(0.95, 0.5 + fail_rate / 2),
                    evidence={"samples": len(results), "fail_rate": round(fail_rate, 3)},
                ))

        for agent, results in by_agent.items():
            if len(results) < self.min_samples:
                continue
            fail_rate = 1.0 - (sum(results) / len(results))
            if fail_rate >= self.failure_rate_threshold:
                out.append(Hypothesis(
                    title=f"Agent '{agent}' underperforming",
                    rationale=f"{fail_rate:.0%} of {agent}'s recent tasks failed.",
                    suggestion={"target": "agent", "agent": agent, "action": "lower_temperature_or_add_critic"},
                    severity="warn",
                    confidence=min(0.9, 0.5 + fail_rate / 2),
                    evidence={"samples": len(results), "fail_rate": round(fail_rate, 3)},
                ))
        return out

    def _build_quality_hypotheses(self, builds: list[dict[str, Any]]) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        scored = [b for b in builds if b.get("score") is not None]
        if len(scored) < self.min_samples:
            return out
        avg = sum(float(b.get("score", 0.0)) for b in scored) / len(scored)
        if avg < 60.0:
            out.append(Hypothesis(
                title="Average build quality is low",
                rationale=f"Mean build score is {avg:.1f}/100 across {len(scored)} builds.",
                suggestion={"target": "pipeline", "action": "enable_best_of_n_or_critic"},
                severity="warn",
                confidence=0.7,
                evidence={"avg_score": round(avg, 1), "samples": len(scored)},
            ))
        no_go = [b for b in scored if (b.get("verdict") or "").lower() == "no_go"]
        if scored and len(no_go) / len(scored) >= 0.5:
            out.append(Hypothesis(
                title="Majority of builds rejected (no_go)",
                rationale=f"{len(no_go)}/{len(scored)} recent builds got a no_go verdict.",
                suggestion={"target": "pipeline", "action": "tighten_architecture_stage"},
                severity="critical",
                confidence=0.75,
                evidence={"no_go": len(no_go), "samples": len(scored)},
            ))
        return out

    def _cost_hypotheses(self, builds: list[dict[str, Any]]) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        costs = [float(b.get("cost_usd", 0.0) or 0.0) for b in builds]
        if len(costs) < self.min_samples:
            return out
        avg_cost = sum(costs) / len(costs)
        if avg_cost >= self.cost_threshold_usd:
            out.append(Hypothesis(
                title="Builds are expensive",
                rationale=f"Mean build cost is ${avg_cost:.2f}, above ${self.cost_threshold_usd:.2f}.",
                suggestion={"target": "router", "action": "prefer_cheaper_tier_for_non_critical_stages"},
                severity="warn",
                confidence=0.65,
                evidence={"avg_cost_usd": round(avg_cost, 3), "samples": len(costs)},
            ))
        return out

    # ---- helpers ---------------------------------------------------------
    async def _safe(self, fn: Any) -> Any:
        if self.store is None:
            return None
        try:
            res = fn()
            if hasattr(res, "__await__"):
                return await res
            return res
        except Exception:  # degrade, don't crash
            return None
