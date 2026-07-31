"""1.3: a stage feeds the tournament for EVERY (tier, task_type) it used.

Before this, the runner recorded only the last route an agent took, so a code
stage writing backend .py + a ui file fed only the last file's bucket — the
`backend` bucket the router queries stayed empty. Now the LLMClient accumulates
the routes of every completion, BaseAgent snapshots the ones a stage used, and
_feed_tournament buffers each distinct (bucket, model, task_type) tuple.

Buffer + flush contract (tournament rewire): _feed_tournament records NOTHING —
solo appearances used to be written as unconditional wins at stage time, which
made the learned router's confidence vacuous (a model whose builds all ended
no_go still had win_rate 1.0). Tuples are buffered per build and flushed at the
build's settle points via _flush_tournament, graded by the terminal verdict:
record_solo(won=True) only for a "go" build, won=False otherwise, with one
batched save per build.
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


def _runner(tmp_path):
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=_settings(tmp_path), memory=None)


def _two_route_result():
    return TaskResult(
        task_id="t", success=True, model_id="m1",
        metadata={"routes": [("backend", "codegen", "m1"), ("ui", "codegen", "m2")]},
    )


_SPEC = types.SimpleNamespace(agent_type="codegen", name="code")


def _stat(settings, bucket, model):
    t = ModelTournament(settings.data_dir / "model_tournament.json")
    return {s.model: s for s in t.leaderboard(bucket)}[model]


def test_feed_buffers_all_routes_without_recording(tmp_path):
    runner = _runner(tmp_path)
    runner._feed_tournament(_SPEC, _two_route_result(), "b1")
    # Every distinct route is buffered for the build's settle-time flush...
    assert set(runner._tournament_pending["b1"]) == {
        ("backend:codegen", "m1", "codegen"),
        ("ui:codegen", "m2", "codegen"),
    }
    # ...but NOTHING hits the tournament yet: a stage completing says nothing
    # about whether the build shipped.
    assert not (runner.settings.data_dir / "model_tournament.json").exists()


def test_feed_dedupes_repeat_routes_across_stages(tmp_path):
    runner = _runner(tmp_path)
    runner._feed_tournament(_SPEC, _two_route_result(), "b1")
    runner._feed_tournament(_SPEC, _two_route_result(), "b1")
    assert len(runner._tournament_pending["b1"]) == 2


def test_no_go_build_flushes_solos_as_losses(tmp_path):
    runner = _runner(tmp_path)
    runner._feed_tournament(_SPEC, _two_route_result(), "b1")
    runner._flush_tournament("b1", won=False)
    assert "b1" not in runner._tournament_pending  # buffer consumed
    for bucket, model in (("backend:codegen", "m1"), ("ui:codegen", "m2")):
        s = _stat(runner.settings, bucket, model)
        assert (s.wins, s.losses, s.plays) == (0, 1, 1)
        assert s.win_rate == 0.0


def test_go_build_flushes_solos_as_wins(tmp_path):
    runner = _runner(tmp_path)
    runner._feed_tournament(_SPEC, _two_route_result(), "b1")
    runner._flush_tournament("b1", won=True)
    assert "b1" not in runner._tournament_pending
    for bucket, model in (("backend:codegen", "m1"), ("ui:codegen", "m2")):
        s = _stat(runner.settings, bucket, model)
        assert (s.wins, s.losses, s.plays) == (1, 0, 1)
        assert s.win_rate == 1.0


def test_crashed_build_settles_buffered_solos_as_losses(tmp_path, monkeypatch):
    """Settle-point wiring: a build that never reaches "go" (here: the generic
    exception handler) flushes its buffered solos with won=False on disk."""
    import skyn3t.studio.runner as runner_mod

    runner = _runner(tmp_path)
    runner._feed_tournament(_SPEC, _two_route_result(), "fixed-b1")

    async def boom(**_kw):
        raise RuntimeError("intelligence exploded")

    monkeypatch.setattr(runner_mod, "prepare_build_intelligence", boom)
    outcome = asyncio.run(runner._start_build(
        "tiny python cli tool",
        extra={"build_id": "fixed-b1", "build_profile": "balanced",
               "app_type": "", "engine": ""},
    ))
    assert outcome.status == "failed"
    assert outcome.verdict == "no_go"
    assert "fixed-b1" not in runner._tournament_pending
    s = _stat(runner.settings, "backend:codegen", "m1")
    assert (s.wins, s.losses) == (0, 1)


def test_record_win_save_false_defers_write(tmp_path):
    p = tmp_path / "t.json"
    t = ModelTournament(p)
    t.record_win("backend:codegen", "m1", losers=[], save=False)
    assert not p.exists()  # write deferred
    t.save()
    assert p.exists()
