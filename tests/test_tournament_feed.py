"""1A: real build stages feed the ModelTournament.

Closes the seam documented in ``model_tournament.py`` (swarm #16): ``record_win``
used to be called only by the rarely-run debate path, so the learned router
almost always abstained. These tests prove the new wiring:

  * ``TaskResult`` carries the model that produced it (``model_id``).
  * ``LLMClient`` tracks the last model it used (``last_model``).
  * ``BaseAgent`` stamps ``model_id`` onto results from its ``llm`` automatically.
  * ``StudioRunner`` records a tournament appearance per successful stage, so the
    leaderboard fills up over real build traffic and the router stops abstaining.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.model_tournament import ModelTournament
from skyn3t.studio.runner import StudioRunner

# ---------------------------------------------------------------------------
# Mechanism: model_id on TaskResult, last_model on LLMClient, base-agent stamp
# ---------------------------------------------------------------------------

def test_task_result_has_model_id_defaulting_none():
    r = TaskResult(task_id="t", success=True)
    assert r.model_id is None
    r2 = TaskResult(task_id="t", success=True, model_id="some/model:free")
    assert r2.model_id == "some/model:free"


def test_llm_client_tracks_last_model():
    from skyn3t.adapters.llm import LLMClient

    client = LLMClient()
    assert client.last_model is None  # nothing used yet
    result = asyncio.run(client.complete("hello"))
    assert client.last_model == result.model
    assert client.last_model  # non-empty


def test_base_agent_stamps_model_id_from_llm():
    class _Agent(BaseAgent):
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def execute(self, task: TaskRequest) -> TaskResult:
            # Agent forgets to stamp model_id; the base class should fill it in.
            return TaskResult(task_id=task.task_id, success=True)

    bus = EventBus()
    agent = _Agent("a", "code", "stub", bus)

    class _FakeLLM:
        last_model = "vendor/model:free"

    agent.llm = _FakeLLM()
    result = asyncio.run(agent.run(TaskRequest(type="code")))
    assert result.model_id == "vendor/model:free"


def test_base_agent_without_llm_leaves_model_id_none():
    class _Agent(BaseAgent):
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def execute(self, task: TaskRequest) -> TaskResult:
            return TaskResult(task_id=task.task_id, success=True)

    bus = EventBus()
    agent = _Agent("a", "misc", "stub", bus)
    result = asyncio.run(agent.run(TaskRequest(type="misc")))
    assert result.model_id is None  # no llm → nothing to stamp, no crash


# ---------------------------------------------------------------------------
# Solo appearances must build play counters (the documented seam relies on this)
# ---------------------------------------------------------------------------

def test_solo_record_win_counts_a_play():
    """A win with no opponent (losers=[]) is the normal single-model build case.

    It must register as one unopposed play+win so evidence accrues over real
    traffic — otherwise the learned router can never reach ``min_plays``.
    """
    t = ModelTournament(path=None)
    bucket = t.bucket_key("cheap", "code")
    t.record_win(bucket, winner="vendor/coder:free", losers=[])
    board = {s.model: s for s in t.leaderboard(bucket)}
    assert board["vendor/coder:free"].plays == 1
    assert board["vendor/coder:free"].wins == 1


def test_router_recommends_after_enough_solo_plays():
    """Once a model clears min_plays/min_win_rate via solo wins, the router stops
    abstaining and recommends it for that (tier, task_type) bucket."""
    from skyn3t.intelligence.routing_recommendations import RoutingRecommender

    t = ModelTournament(path=None)
    bucket = t.bucket_key("cheap", "code")
    for _ in range(5):
        t.record_win(bucket, winner="vendor/coder:free", losers=[])
    rr = RoutingRecommender(t)  # default min_plays=5, min_win_rate=0.55
    assert rr.recommend("cheap", task_type="code") == "vendor/coder:free"


# ---------------------------------------------------------------------------
# Integration: a real build feeds the tournament on disk
# ---------------------------------------------------------------------------

class _StubCodeAgent(BaseAgent):
    """A code agent whose result carries a model_id (as an LLM-backed one would)."""

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        wt = Path(task.payload["worktree_dir"])
        (wt / "src").mkdir(parents=True, exist_ok=True)
        (wt / "src" / "main.py").write_text("def main():\n    return 42\n")
        (wt / "src" / "__init__.py").write_text("")
        (wt / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1.0'\n")
        (wt / "README.md").write_text("# demo\n")
        return TaskResult(
            task_id=task.task_id, success=True, model_id="vendor/coder:free",
            output={"files_written": 4, "worktree_dir": str(wt)},
        )


def _settings(tmp_path):
    return Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
                    logs_dir=tmp_path / "logs", critic_enabled=False)


def test_runner_feeds_tournament_from_stage(tmp_path):
    async def run():
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        await orch.register(code)
        settings = _settings(tmp_path)
        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        await runner.start("Build a python tool", slug="feed")

        # The tournament JSON the LearnedModelRouter reads must now have evidence.
        t = ModelTournament(settings.data_dir / "model_tournament.json")
        assert t.has_data(), "real build stage did not feed the tournament"
        # The model the code agent reported should appear with at least one play.
        all_models = {
            s.model for b in t.buckets() for s in t.leaderboard(b)
        }
        assert "vendor/coder:free" in all_models
    asyncio.run(run())
