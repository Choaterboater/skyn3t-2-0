# tests/test_orchestrator_idempotency.py
"""The idempotency cache must be bounded like _results — it had no cap, so a
long-lived web process accumulated TaskResults forever."""
from __future__ import annotations

from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator


async def test_idempotency_cache_is_bounded():
    orch = Orchestrator(event_bus=EventBus())
    orch._idempotency_max = 3
    for i in range(10):
        task = TaskRequest(type="t", payload={}, idempotency_key=f"k{i}")
        res = TaskResult(task_id=task.task_id, success=True)
        await orch._finalize(task, res)
    assert len(orch._idempotency) <= 3
