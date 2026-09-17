"""Outer retry ownership preserves normal recovery and explicit resubmission."""

from __future__ import annotations

import pytest

from skyn3t.agents.echo_agent import EchoAgent
from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator


class _RecoveringAgent(EchoAgent):
    def __init__(self, bus: EventBus, error: str, retryable: bool | None) -> None:
        super().__init__("recovering", bus)
        self.error = error
        self.retryable = retryable
        self.calls = 0

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.calls += 1
        if self.calls == 1:
            result = TaskResult(task_id=task.task_id, success=False, error=self.error)
            if self.retryable is not None:
                result.retryable = self.retryable
            return result
        return await super().execute(task)


@pytest.mark.parametrize(
    ("error", "retryable", "attempts", "success"),
    [
        ("429 rate limit", None, 2, True),
        ("429 rate limit", True, 2, True),
        ("429 rate limit", False, 1, False),
        ("invalid syntax", True, 1, False),
    ],
)
async def test_outer_retry_permission_preserves_error_classification(
    error, retryable, attempts, success
):
    bus = EventBus()
    orchestrator = Orchestrator(bus)
    agent = _RecoveringAgent(bus, error, retryable)
    await orchestrator.register(agent)
    result = await orchestrator.submit(TaskRequest(type="echo", max_retries=2))

    assert result.success is success
    assert result.attempts == agent.calls == attempts
    assert len(bus.history(event_type=EventType.TASK_RETRYING)) == attempts - 1


async def test_retry_opt_out_does_not_block_a_later_explicit_submission():
    bus = EventBus()
    orchestrator = Orchestrator(bus)
    agent = _RecoveringAgent(bus, "429 rate limit", False)
    await orchestrator.register(agent)
    first = await orchestrator.submit(TaskRequest(type="echo", idempotency_key="repair"))
    assert not first.success and agent.calls == 1

    second = await orchestrator.submit(TaskRequest(
        type="echo", payload={"new_evidence": True}, idempotency_key="repair"
    ))
    assert second.success and agent.calls == 2
    assert second.output == {"echo": {"new_evidence": True}}
