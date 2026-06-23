"""Verify-first acceptance gate for closing the model-learning loop.

A real offline (stub) build feeds the ModelTournament from the stages whose
agents call ``complete()``. Today every route is ``(tier, "")`` because no agent
passes a ``task_type``, so the tournament only ever has bare-tier buckets
("cheap", "strong", "ui"). Once ``task_type`` is threaded at the call sites,
recorded buckets carry the stage role (``tier:task_type``) — the deterministic
offline signal that the loop's bucket keys match what the learned router queries.

(The per-file ``backend`` tier only arises from code_agent's non-stub per-file
path, which short-circuits under the stub backend — that granularity is covered
by a focused unit test, not this e2e.)
"""

from __future__ import annotations

import asyncio

from skyn3t.adapters.llm import LLMClient
from skyn3t.cli.main import build_agents
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.model_tournament import ModelTournament
from skyn3t.studio.runner import StudioRunner


def _settings(tmp_path):
    # Minimize the pipeline to the codegen + verifier path: no critic gate, no
    # liveness boot, no generated test/build subprocesses — fast and hermetic.
    return Settings(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs", critic_enabled=False,
        liveness_check_enabled=False, run_generated_tests=False,
        run_generated_build=False,
    )


def test_build_feeds_task_typed_buckets(tmp_path):
    async def run():
        bus = EventBus()
        settings = _settings(tmp_path)
        llm = LLMClient(settings)  # stub backend (pinned in conftest)
        orch = Orchestrator(bus)
        for agent in build_agents(event_bus=bus, llm=llm, memory=None):
            await orch.register(agent)
        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        await runner.start("a small flask todo api", slug="loop")

        t = ModelTournament(settings.data_dir / "model_tournament.json")
        assert t.has_data(), "build did not feed the tournament at all"
        buckets = t.buckets()
        # The real signal: at least one bucket is task-typed (tier:task_type),
        # not a bare tier — proving task_type now reaches complete().
        assert any(":" in b for b in buckets), f"no task-typed buckets: {buckets}"

    asyncio.run(run())
