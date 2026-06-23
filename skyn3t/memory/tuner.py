"""SelfTuningEngine — applies safe reflection suggestions to live agent configs.

Listens for ``KNOWLEDGE_UPDATED`` events (and can be driven directly by the
MetaAgent's ``INSIGHT_PUBLISHED`` hypotheses). When a suggestion is judged
*safe*, it mutates the targeted live agent's ``config`` dict in place — bounded
to a small allow-list of numeric/boolean knobs with hard clamps. Anything
outside the allow-list is rejected, so the engine can only nudge, never break
(design rule #4 safe by default).

The engine is registry-driven: you hand it a mapping of agent name -> BaseAgent
(or any object exposing a ``config`` dict). It never imports concrete agents and
never touches the orchestrator's internals beyond the public ``.agents`` dict.

Design rules: #4 safe, #6 degrade, #7 everything is an event.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from skyn3t.core.events import Event, EventBus, EventType


# Allow-list of tunable config knobs with (min, max) clamps. Only these may be
# written by the tuner — everything else is ignored.
#
# Consumer notes (every advertised knob MUST have a real consumer):
#
#   best_of_n   — read by StudioRunner via ``plan.best_of_n`` and
#                 ``settings.best_of_n`` (planner._resolve_best_of_n); tuner
#                 writes it to agent.config AND cortex.tuning_store persists it
#                 into Settings so subsequent planner calls pick it up.
#   max_retries — read by Orchestrator via ``task.max_retries`` on AgentTask,
#                 NOT from agent.config; kept here so the tuner can write it to
#                 configs that are ALSO used directly as AgentTask kwargs by the
#                 runner (runner.py passes plan-level extra dict to task).
#
# Removed from a previous version (were writing into the void):
#   temperature — LLM adapter takes temperature as a call-site param, never
#                 reads agent.config["temperature"]; no consumer.
#   max_tokens  — hardcoded at each llm.complete() call site; not read from
#                 agent.config.
#   timeout_s   — nothing in the codebase reads agent.config["timeout_s"].
SAFE_KNOBS: dict[str, tuple[float, float]] = {
    "best_of_n": (1.0, 4.0),
    "max_retries": (0.0, 5.0),
}
# Boolean flags that may be flipped on (never required, just enabled).
#
# Consumer notes:
#   critic_enabled   — read by StudioRunner (runner.py:797 ``self.settings.
#                      critic_enabled``) and persisted by tuning_store into
#                      Settings overrides so it carries across builds.
#   reflective_retry — read by reflection.py and persisted into Settings;
#                      also emitted by InsightPublished as a concrete action.
#
# Removed from a previous version (were writing into the void):
#   json_mode — agents hardcode json_mode=True/False at each llm.complete()
#               call site; writing it to agent.config has no effect.
SAFE_FLAGS = {"critic_enabled", "reflective_retry"}


@dataclass(slots=True)
class TuningChange:
    agent: str
    key: str
    old: Any
    new: Any
    reason: str
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "key": self.key, "old": self.old,
                "new": self.new, "reason": self.reason, "at": self.at}


class SelfTuningEngine:
    """Applies bounded, reversible config nudges to live agents."""

    def __init__(
        self,
        event_bus: EventBus,
        agents: dict[str, Any] | None = None,
        *,
        max_changes_per_event: int = 3,
        dry_run: bool = False,
    ) -> None:
        self.event_bus = event_bus
        self.agents: dict[str, Any] = agents if agents is not None else {}
        self.max_changes_per_event = int(max_changes_per_event)
        self.dry_run = bool(dry_run)
        self.history: list[TuningChange] = []
        self._unsubs: list[Any] = []

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._unsubs:
            return
        self._unsubs.append(
            self.event_bus.subscribe(EventType.KNOWLEDGE_UPDATED, self._on_knowledge_updated)
        )
        self._unsubs.append(
            self.event_bus.subscribe(EventType.INSIGHT_PUBLISHED, self._on_insight)
        )

    def stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # pragma: no cover
                pass
        self._unsubs.clear()

    def register_agent(self, name: str, agent: Any) -> None:
        self.agents[name] = agent

    # ---- event handlers --------------------------------------------------
    async def _on_knowledge_updated(self, event: Event) -> None:
        payload = event.payload or {}
        suggestions = payload.get("suggestions") or payload.get("reflections") or []
        if isinstance(suggestions, dict):
            suggestions = [suggestions]
        await self._apply_many(suggestions, source="knowledge_updated")

    async def _on_insight(self, event: Event) -> None:
        payload = event.payload or {}
        suggestion = payload.get("suggestion")
        if not suggestion:
            return
        # Only act on suggestions confident enough and not merely informational.
        if float(payload.get("confidence", 0.0)) < 0.6:
            return
        await self._apply_many([self._insight_to_suggestion(payload, suggestion)],
                               source="insight")

    # ---- translation -----------------------------------------------------
    def _insight_to_suggestion(self, payload: dict[str, Any], suggestion: dict[str, Any]) -> dict[str, Any]:
        """Map a MetaAgent hypothesis suggestion to a concrete config change.

        Hypotheses describe *intent* (e.g. action='lower_temperature_or_add_critic');
        here we translate the safe ones into explicit knob writes.
        """
        action = (suggestion.get("action") or "").lower()
        target_agent = suggestion.get("agent")
        changes: dict[str, Any] = {}
        # NOTE: ``temperature`` is intentionally NOT generated here. Nothing reads
        # ``agent.config['temperature']`` (the LLM adapter takes temperature as a
        # call-site param), so writing it would be a dead change silently dropped
        # by ``_apply_knob``. The common "lower_temperature_or_add_critic" insight
        # is enacted through its consumed, effective half: enabling the critic.
        if "add_critic" in action or "enable_critic" in action:
            changes["critic_enabled"] = True
        if "increase_retries" in action:
            changes["max_retries"] = {"op": "add", "delta": 1}
        if "reflective_retry" in action:
            changes["reflective_retry"] = True
        return {"agent": target_agent, "changes": changes, "reason": payload.get("title", action)}

    # ---- application -----------------------------------------------------
    async def _apply_many(self, suggestions: list[dict[str, Any]], source: str) -> list[TuningChange]:
        applied: list[TuningChange] = []
        for sug in suggestions:
            if len(applied) >= self.max_changes_per_event:
                break
            applied.extend(self._apply_one(sug, source))
        for change in applied:
            try:
                await self.event_bus.emit(
                    EventType.SYSTEM, "self_tuning",
                    {"kind": "config_tuned", **change.to_dict()},
                )
            except Exception:  # degrade, don't crash
                pass
        return applied

    def _apply_one(self, sug: dict[str, Any], source: str) -> list[TuningChange]:
        if not isinstance(sug, dict):
            return []
        changes = sug.get("changes") or {}
        reason = sug.get("reason", source)
        agent_name = sug.get("agent")
        targets = self._resolve_targets(agent_name)
        out: list[TuningChange] = []
        for name, agent in targets:
            cfg = getattr(agent, "config", None)
            if not isinstance(cfg, dict):
                continue
            for key, spec in changes.items():
                change = self._apply_knob(name, cfg, key, spec, reason)
                if change is not None:
                    out.append(change)
                    if not self.dry_run:
                        self.history.append(change)
        return out

    def _resolve_targets(self, agent_name: str | None) -> list[tuple[str, Any]]:
        if agent_name and agent_name in self.agents:
            return [(agent_name, self.agents[agent_name])]
        if agent_name:
            return []  # named but unknown -> nothing to do
        return list(self.agents.items())  # broadcast

    def _apply_knob(self, name: str, cfg: dict[str, Any], key: str, spec: Any, reason: str) -> TuningChange | None:
        # Boolean flags: only allowed to enable.
        if key in SAFE_FLAGS:
            if spec is not True:
                return None
            old = cfg.get(key)
            if old is True:
                return None
            if not self.dry_run:
                cfg[key] = True
            return TuningChange(name, key, old, True, reason)

        if key not in SAFE_KNOBS:
            return None
        lo, hi = SAFE_KNOBS[key]
        old = cfg.get(key)
        new = self._compute_value(old, spec, lo, hi)
        if new is None or new == old:
            return None
        if not self.dry_run:
            cfg[key] = new
        return TuningChange(name, key, old, new, reason)

    def _compute_value(self, old: Any, spec: Any, lo: float, hi: float) -> float | None:
        try:
            if isinstance(spec, dict):
                op = spec.get("op", "set")
                base = float(old) if isinstance(old, (int, float)) else lo
                if op == "scale":
                    val = base * float(spec.get("factor", 1.0))
                elif op == "add":
                    val = base + float(spec.get("delta", 0.0))
                else:
                    val = float(spec.get("value", base))
            else:
                val = float(spec)
        except (TypeError, ValueError):
            return None
        clamped = max(lo, min(hi, val))
        # Preserve int-ness for whole-number knobs.
        if isinstance(old, int) or clamped.is_integer():
            return int(clamped)
        return round(clamped, 4)

    def stats(self) -> dict[str, Any]:
        return {"agents": len(self.agents), "changes_applied": len(self.history),
                "dry_run": self.dry_run}
