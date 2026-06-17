"""EchoAgent — the minimal reference agent.

Proves the spine end-to-end: it registers, receives a routed task over the
bus, and returns a persisted result. Used in tests and as the template for
real agents.
"""

from __future__ import annotations

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


class EchoAgent(BaseAgent):
    def __init__(self, name: str, event_bus: EventBus, config: dict | None = None) -> None:
        super().__init__(name, agent_type="echo", provider="local", event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(name="echo", description="Echoes its payload back"))

    async def initialize(self) -> None:
        self.metadata["ready"] = True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(task_id=task.task_id, success=True, output={"echo": task.payload})

    async def health_check(self) -> bool:
        return True
