"""1.3: a stage feeds the tournament for EVERY (tier, task_type) it used.

Before this, the runner recorded only the last route an agent took, so a code
stage writing backend .py + a ui file fed only the last file's bucket — the
`backend` bucket the router queries stayed empty. Now the LLMClient accumulates
the routes of every completion, BaseAgent snapshots the ones a stage used, and
_feed_tournament records each distinct bucket (with a single batched save).
"""

from __future__ import annotations

import asyncio
import types

from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings
from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.model_tournament import ModelTournament
from skyn3t.studio.runner import StudioRunner


def test_llm_client_accumulates_routes():
    c = LLMClient()
    assert c.routes == []
    asyncio.run(c.complete("hi", tier=Tier.BACKEND, task_type="codegen"))
    asyncio.run(c.complete("yo", tier=Tier.UI, task_type="codegen"))
    assert len(c.routes) == 2
    tiers = {r[0] for r in c.routes}
    assert "backend" in tiers and "ui" in tiers
    assert all(r[1] == "codegen" for r in c.routes)
    assert all(r[2] for r in c.routes)  # model non-empty


def test_base_agent_snapshots_routes_used_during_execute():
    class _Multi(BaseAgent):
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def execute(self, task: TaskRequest) -> TaskResult:
            await self.llm.complete("a", tier=Tier.BACKEND, task_type="codegen")
            await self.llm.complete("b", tier=Tier.UI, task_type="codegen")
            return TaskResult(task_id=task.task_id, success=True)

    bus = EventBus()
    agent = _Multi("m", "codegen", "stub", bus)
    agent.llm = LLMClient()
    result = asyncio.run(agent.run(TaskRequest(type="codegen")))
    routes = result.metadata.get("routes")
    assert routes and len(routes) == 2
    assert {r[0] for r in routes} == {"backend", "ui"}


def _settings(tmp_path):
    return Settings(projects_dir=tmp_path / "P", data_dir=tmp_path / "d",
                    logs_dir=tmp_path / "l", critic_enabled=False)


def test_feed_records_all_routes(tmp_path):
    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=_settings(tmp_path), memory=None)
    result = TaskResult(
        task_id="t", success=True, model_id="m1",
        metadata={"routes": [("backend", "codegen", "m1"), ("ui", "codegen", "m2")]},
    )
    spec = types.SimpleNamespace(agent_type="codegen", name="code")
    runner._feed_tournament(spec, result)
    buckets = set(ModelTournament(runner.settings.data_dir / "model_tournament.json").buckets())
    assert "backend:codegen" in buckets
    assert "ui:codegen" in buckets


def test_record_win_save_false_defers_write(tmp_path):
    p = tmp_path / "t.json"
    t = ModelTournament(p)
    t.record_win("backend:codegen", "m1", losers=[], save=False)
    assert not p.exists()  # write deferred
    t.save()
    assert p.exists()
