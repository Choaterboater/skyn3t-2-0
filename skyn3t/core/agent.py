"""BaseAgent contract and the task request/result types.

All specialist agents subclass :class:`BaseAgent` and implement three
methods: ``initialize``, ``execute``, and ``health_check``. The base class
handles event emission, status tracking, and capability matching so subclasses
focus only on their work.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from time import time
from typing import Any

import structlog

from skyn3t.core.events import EventBus, EventType

log = structlog.get_logger(__name__)


class AgentStatus(StrEnum):
    CREATED = "created"
    READY = "ready"
    BUSY = "busy"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(slots=True)
class AgentCapability:
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class TaskRequest:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    capabilities_required: tuple[str, ...] = ()
    priority: int = 5  # lower = higher priority
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    correlation_id: str | None = None
    idempotency_key: str | None = None
    max_retries: int = 2
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskResult:
    task_id: str
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    agent_name: str | None = None
    duration_ms: float = 0.0
    attempts: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    # Which LLM model produced this result, when the agent used one. Stamped
    # automatically by BaseAgent from its ``llm`` client so StudioRunner can
    # feed the ModelTournament from real build traffic (closes swarm #16).
    model_id: str | None = None


class BaseAgent(ABC):
    """Abstract base for every agent in the swarm."""

    def __init__(
        self,
        name: str,
        agent_type: str,
        provider: str,
        event_bus: EventBus | None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.agent_type = agent_type
        self.provider = provider
        if event_bus is None:
            log.warning("agent.detached_event_bus", agent=name, agent_type=agent_type)
            event_bus = EventBus()
        self.event_bus = event_bus
        self.config: dict[str, Any] = config or {}
        self.capabilities: list[AgentCapability] = []
        self.status = AgentStatus.CREATED
        self.metadata: dict[str, Any] = {}
        self._last_heartbeat = time()
        # Serialize task execution per agent: one agent is one worker, so two
        # concurrently-routed tasks don't race on status/metadata.
        self._run_lock = asyncio.Lock()

    # ---- prompt evolution ------------------------------------------------
    def system_prompt(self, base: str) -> str:
        """Return ``base`` augmented with any cortex prompt override.

        Cortex's prompt-evolution loop (gated, human-approved) writes an evolved
        instruction onto ``config['prompt_override']``; agents call this when
        assembling their system prompt so an approved improvement actually takes
        effect on the next build. No override → ``base`` unchanged.
        """
        override = (self.config or {}).get("prompt_override")
        if not override:
            return base
        return f"{base}\n\nAdditional guidance (learned):\n{override}"

    # ---- capability registration ----------------------------------------
    def add_capability(self, capability: AgentCapability) -> None:
        self.capabilities.append(capability)

    def has_capabilities(self, required: tuple[str, ...]) -> bool:
        owned = {c.name for c in self.capabilities}
        return all(r in owned for r in required)

    @property
    def capability_names(self) -> set[str]:
        return {c.name for c in self.capabilities}

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        await self.initialize()
        self.status = AgentStatus.READY
        await self.event_bus.emit(
            EventType.AGENT_REGISTERED, self.name, {"type": self.agent_type, "provider": self.provider}
        )

    async def run(self, task: TaskRequest) -> TaskResult:
        """Execute a task with timing + event emission around it.

        Serialized per agent via ``_run_lock`` so concurrent routing of the same
        agent doesn't corrupt status/metadata.
        """
        async with self._run_lock:
            return await self._run_locked(task)

    async def _run_locked(self, task: TaskRequest) -> TaskResult:
        self.status = AgentStatus.BUSY
        started = time()
        # Scope this run's LLM-route capture: agents share one client, but
        # ``run`` holds ``_run_lock`` across this whole method, so clearing here
        # attributes exactly the completions THIS run produces to the tournament
        # AND bounds the list (no unbounded growth over a long-lived process).
        llm = getattr(self, "llm", None)
        if llm is not None:
            # Start an ISOLATED, task-local route capture for this run: the client
            # is shared across agents, so a bare shared-list clear() would race
            # concurrent runs. Fall back to routes.clear() for test doubles.
            if hasattr(llm, "begin_run_capture"):
                with contextlib.suppress(Exception):
                    llm.begin_run_capture()
            elif hasattr(llm, "routes"):
                with contextlib.suppress(Exception):
                    llm.routes.clear()
        await self.event_bus.emit(
            EventType.TASK_STARTED, self.name,
            {
                "task_id": task.task_id,
                "type": task.type,
                "metadata": dict(task.metadata),
            },
            correlation_id=task.correlation_id,
        )
        try:
            result = await self.execute(task)
            result.agent_name = self.name
            result.duration_ms = (time() - started) * 1000
            # Stamp the model the agent's LLM last used, so stage outcomes can
            # feed the ModelTournament. Single chokepoint: agents that hold a
            # ``self.llm`` get this for free without plumbing it through every
            # ``TaskResult(...)`` site. Agents without an LLM leave it None.
            if result.model_id is None:
                if llm is not None:
                    result.model_id = getattr(llm, "last_model", None)
                    route = getattr(llm, "last_route", None)
                    if route and "route" not in result.metadata:
                        # (tier, task_type) the model routed through, so the
                        # runner records into the bucket the router queries.
                        result.metadata["route"] = route
                    # The full set of (tier, task_type, model) this stage used,
                    # so every bucket it touched is fed (not just the last file).
                    routes = list(getattr(llm, "routes", ()))
                    if routes and "routes" not in result.metadata:
                        result.metadata["routes"] = routes
            self.status = AgentStatus.READY
            ev = EventType.TASK_COMPLETED if result.success else EventType.TASK_FAILED
            await self.event_bus.emit(
                ev, self.name,
                {
                    "task_id": task.task_id,
                    "success": result.success,
                    "error": result.error,
                    "metadata": dict(task.metadata),
                },
                correlation_id=task.correlation_id,
            )
            return result
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed result
            self.status = AgentStatus.READY
            await self.event_bus.emit(
                EventType.TASK_FAILED, self.name,
                {
                    "task_id": task.task_id,
                    "error": str(exc),
                    "metadata": dict(task.metadata),
                },
                correlation_id=task.correlation_id,
            )
            return TaskResult(
                task_id=task.task_id, success=False, error=str(exc),
                agent_name=self.name, duration_ms=(time() - started) * 1000,
            )

    async def heartbeat(self) -> None:
        self._last_heartbeat = time()
        await self.event_bus.emit(EventType.AGENT_HEARTBEAT, self.name, {"status": self.status.value})

    # ---- abstract surface ------------------------------------------------
    @abstractmethod
    async def initialize(self) -> None:
        """Set up clients/state. Called once before the agent serves tasks."""

    @abstractmethod
    async def execute(self, task: TaskRequest) -> TaskResult:
        """Perform the task. Return a TaskResult (never raise for task errors)."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the agent is healthy and reachable."""
