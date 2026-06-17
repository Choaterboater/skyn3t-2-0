"""Orchestrator — agent registry, capability routing, retries, self-healing.

The orchestrator owns the agent registry and turns a :class:`TaskRequest`
into a :class:`TaskResult` by routing it to a capable agent. It enforces
idempotency, bounded concurrency, and retry classification, and emits events
at every step so the rest of the system can observe and learn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import time
from typing import Any

import structlog

from skyn3t.core.agent import AgentStatus, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType

log = structlog.get_logger(__name__)

# A persistence sink: anything with async ``save_task(task, result)``.
PersistFn = Callable[[TaskRequest, TaskResult], Awaitable[None]]

# Errors whose message contains these substrings are treated as transient.
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "rate limit", "429", "503", "502", "500",
    "connection", "temporarily", "overloaded", "reset by peer",
)


def classify_error(error: str | None) -> str:
    """Return 'transient' (retry) or 'permanent' (don't)."""
    if not error:
        return "permanent"
    low = error.lower()
    return "transient" if any(m in low for m in _TRANSIENT_MARKERS) else "permanent"


class Orchestrator:
    def __init__(
        self,
        event_bus: EventBus,
        max_concurrency: int = 8,
        persist: PersistFn | None = None,
    ) -> None:
        self.event_bus = event_bus
        self._agents: dict[str, BaseAgent] = {}
        self._sem = asyncio.Semaphore(max_concurrency)
        self._persist = persist
        self._idempotency: dict[str, TaskResult] = {}
        self._self_healing: SelfHealingManager | None = None
        self._results: dict[str, TaskResult] = {}

    # ---- registry --------------------------------------------------------
    async def register(self, agent: BaseAgent) -> None:
        if agent.status == AgentStatus.CREATED:
            await agent.start()
        self._agents[agent.name] = agent
        log.info("agent.registered", agent=agent.name, type=agent.agent_type)

    async def unregister(self, name: str) -> None:
        agent = self._agents.pop(name, None)
        if agent is not None:
            await self.event_bus.emit(EventType.AGENT_UNREGISTERED, name, {})

    @property
    def agents(self) -> dict[str, BaseAgent]:
        return dict(self._agents)

    def attach_self_healing(self, manager: SelfHealingManager) -> None:
        self._self_healing = manager

    # ---- routing ---------------------------------------------------------
    def _route(self, task: TaskRequest) -> BaseAgent | None:
        """Pick the best agent for a task.

        Match priority: exact agent_type == task.type AND required caps,
        then any agent satisfying required caps, preferring the least busy.
        """
        candidates = [
            a for a in self._agents.values()
            if a.has_capabilities(task.capabilities_required)
            and a.status in (AgentStatus.READY, AgentStatus.BUSY)
        ]
        if not candidates:
            return None
        typed = [a for a in candidates if a.agent_type == task.type]
        pool = typed or candidates
        # Prefer ready over busy (least-loaded heuristic).
        pool.sort(key=lambda a: (a.status != AgentStatus.READY,))
        return pool[0]

    # ---- submission ------------------------------------------------------
    async def submit(self, task: TaskRequest) -> TaskResult:
        # Idempotency: identical key returns the cached result.
        if task.idempotency_key and task.idempotency_key in self._idempotency:
            log.info("task.idempotent_hit", key=task.idempotency_key)
            return self._idempotency[task.idempotency_key]

        await self.event_bus.emit(
            EventType.TASK_SUBMITTED, "orchestrator",
            {"task_id": task.task_id, "type": task.type},
            correlation_id=task.correlation_id,
        )

        agent = self._route(task)
        if agent is None:
            result = TaskResult(
                task_id=task.task_id, success=False,
                error=f"no agent for type={task.type} caps={task.capabilities_required}",
            )
            await self._finalize(task, result)
            return result

        await self.event_bus.emit(
            EventType.TASK_ROUTED, "orchestrator",
            {"task_id": task.task_id, "agent": agent.name},
            correlation_id=task.correlation_id,
        )

        result = await self._run_with_retries(agent, task)
        await self._finalize(task, result)
        return result

    async def _run_with_retries(self, agent: BaseAgent, task: TaskRequest) -> TaskResult:
        attempt = 0
        last: TaskResult | None = None
        while attempt <= task.max_retries:
            attempt += 1
            async with self._sem:
                result = await agent.run(task)
            result.attempts = attempt
            if result.success:
                return result
            last = result
            if classify_error(result.error) == "permanent":
                return result
            if attempt <= task.max_retries:
                await self.event_bus.emit(
                    EventType.TASK_RETRYING, "orchestrator",
                    {"task_id": task.task_id, "attempt": attempt, "error": result.error},
                    correlation_id=task.correlation_id,
                )
                # Exponential backoff, capped.
                await asyncio.sleep(min(2 ** attempt * 0.1, 5.0))
                # Re-route in case the original agent is now unhealthy.
                if self._self_healing is not None:
                    await self._self_healing.on_agent_error(agent, result.error or "")
                rerouted = self._route(task)
                if rerouted is not None:
                    agent = rerouted
        return last or TaskResult(task_id=task.task_id, success=False, error="exhausted retries")

    async def _finalize(self, task: TaskRequest, result: TaskResult) -> None:
        self._results[task.task_id] = result
        if task.idempotency_key:
            self._idempotency[task.idempotency_key] = result
        if self._persist is not None:
            try:
                await self._persist(task, result)
            except Exception as exc:  # noqa: BLE001 - persistence must not break flow
                log.warning("persist.failed", error=str(exc))

    async def wait_for_result(self, task_id: str, timeout: float = 30.0) -> TaskResult | None:
        deadline = time() + timeout
        while time() < deadline:
            if task_id in self._results:
                return self._results[task_id]
            await asyncio.sleep(0.05)
        return None

    async def health_report(self) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for name, agent in self._agents.items():
            try:
                healthy = await agent.health_check()
            except Exception:  # noqa: BLE001
                healthy = False
            report[name] = {"status": agent.status.value, "healthy": healthy}
        return report


class SelfHealingManager:
    """Pluggable recovery: restart/replace agents that fail repeatedly."""

    def __init__(self, orchestrator: Orchestrator, event_bus: EventBus, threshold: int = 3) -> None:
        self.orchestrator = orchestrator
        self.event_bus = event_bus
        self.threshold = threshold
        self._failures: dict[str, int] = {}

    async def on_agent_error(self, agent: BaseAgent, error: str) -> None:
        self._failures[agent.name] = self._failures.get(agent.name, 0) + 1
        if self._failures[agent.name] < self.threshold:
            return
        await self.event_bus.emit(EventType.AGENT_FAILED, agent.name, {"error": error})
        try:
            await agent.initialize()
            agent.status = AgentStatus.READY
            self._failures[agent.name] = 0
            await self.event_bus.emit(EventType.AGENT_RECOVERED, agent.name, {})
        except Exception as exc:  # noqa: BLE001
            agent.status = AgentStatus.FAILED
            log.error("self_heal.failed", agent=agent.name, error=str(exc))
