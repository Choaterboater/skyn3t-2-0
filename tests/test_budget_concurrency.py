from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path

import pytest

from skyn3t.adapters.llm import BudgetExceeded, BudgetTracker, LLMResult
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.observability.cost_tracker import CostTracker
from skyn3t.studio.runner import StudioRunner


def _result(cost: float, *, tokens: int = 3) -> LLMResult:
    return LLMResult(
        text="ok",
        model="test-model",
        backend="openrouter",
        prompt_tokens=tokens - 1,
        completion_tokens=1,
        cost_usd=cost,
    )


def _record_budget_in_process(
    data_dir: str,
    ready: multiprocessing.Queue,
    start: multiprocessing.Event,
    iterations: int,
) -> None:
    tracker = BudgetTracker(
        per_build_cap=100.0,
        daily_cap=100.0,
        token_cap=1_000_000,
        ledger_path=Path(data_dir) / "daily_usage.json",
    )
    ready.put(True)
    if not start.wait(15):
        raise RuntimeError("parent never released budget workers")
    for _ in range(iterations):
        tracker.record(_result(0.01))


@pytest.mark.parametrize("disabled_cap", [0, -1])
def test_nonpositive_daily_caps_disable_usd_and_token_guards(disabled_cap):
    budget = BudgetTracker(
        per_build_cap=0,
        daily_cap=float(disabled_cap),
        token_cap=disabled_cap,
    )
    budget.record(_result(999.0, tokens=9_000_000))

    budget.check()  # no exception: usage is tracked, both daily guards are disabled
    assert budget.spent_day == pytest.approx(999.0)
    assert budget.tokens_day == 9_000_000


def test_positive_daily_caps_still_trip():
    usd_budget = BudgetTracker(per_build_cap=0, daily_cap=0.5, token_cap=10_000)
    usd_budget.record(_result(0.6, tokens=10))
    with pytest.raises(BudgetExceeded, match="daily cap"):
        usd_budget.check()

    token_budget = BudgetTracker(per_build_cap=0, daily_cap=10, token_cap=5)
    token_budget.record(_result(0.1, tokens=6))
    with pytest.raises(BudgetExceeded, match="daily token cap"):
        token_budget.check()


def test_daily_ledger_updates_are_atomic_across_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    start = ctx.Event()
    process_count = 4
    iterations = 5
    workers = [
        ctx.Process(
            target=_record_budget_in_process,
            args=(str(tmp_path), ready, start, iterations),
        )
        for _ in range(process_count)
    ]
    for worker in workers:
        worker.start()
    for _ in workers:
        assert ready.get(timeout=20) is True
    start.set()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    restored = BudgetTracker(
        per_build_cap=100.0,
        daily_cap=100.0,
        token_cap=1_000_000,
        ledger_path=tmp_path / "daily_usage.json",
    )
    assert restored.spent_day == pytest.approx(process_count * iterations * 0.01)
    assert restored.tokens_day == process_count * iterations * 3


async def test_overlapping_builds_keep_caps_stages_and_calls_isolated():
    budget = BudgetTracker(per_build_cap=1.0, daily_cap=10.0, token_cap=10_000)
    tracker = CostTracker(budget=budget)
    a_recorded = asyncio.Event()
    b_recorded = asyncio.Event()
    a_finished = asyncio.Event()

    async def build_a() -> dict:
        tracker.start_build("build-a")
        tracker.start_stage("build-a", "code")
        budget.record(_result(0.10, tokens=10))
        a_recorded.set()
        await b_recorded.wait()
        stage = tracker.end_stage("build-a", "code")
        report = tracker.end_build("build-a")
        a_finished.set()
        return {"stage": stage, "report": report}

    async def build_b() -> dict:
        await a_recorded.wait()
        tracker.start_build("build-b")
        tracker.start_stage("build-b", "code")
        budget.record(_result(0.25, tokens=20))
        b_recorded.set()
        await a_finished.wait()
        stage = tracker.end_stage("build-b", "code")
        report = tracker.end_build("build-b")
        return {"stage": stage, "report": report}

    a, b = await asyncio.gather(build_a(), build_b())

    assert a["stage"]["cost_usd"] == pytest.approx(0.10)
    assert a["stage"]["tokens"] == 10
    assert a["report"]["cost_usd"] == pytest.approx(0.10)
    assert b["stage"]["cost_usd"] == pytest.approx(0.25)
    assert b["stage"]["tokens"] == 20
    assert b["report"]["cost_usd"] == pytest.approx(0.25)
    assert [(call.cost_usd, call.build_id) for call in budget.calls] == [
        (0.10, "build-a"),
        (0.25, "build-b"),
    ]
    assert tracker._builds == {}


async def test_per_build_cap_is_independent_and_shared_with_child_tasks():
    budget = BudgetTracker(per_build_cap=0.15, daily_cap=10.0, token_cap=10_000)
    tracker = CostTracker(budget=budget)
    tracker.start_build("parent")

    async def record_in_child() -> None:
        budget.record(_result(0.08))

    await asyncio.gather(record_in_child(), record_in_child())
    with pytest.raises(BudgetExceeded, match="per-build cap"):
        budget.check()
    report = tracker.end_build("parent")
    assert report["cost_usd"] == pytest.approx(0.16)


async def test_overlapping_build_cap_checks_do_not_share_spend():
    budget = BudgetTracker(per_build_cap=0.15, daily_cap=10.0, token_cap=10_000)
    tracker = CostTracker(budget=budget)
    a_recorded = asyncio.Event()
    b_checked = asyncio.Event()

    async def build_a() -> None:
        tracker.start_build("a")
        budget.record(_result(0.10))
        a_recorded.set()
        await b_checked.wait()
        budget.check()
        budget.record(_result(0.06))
        with pytest.raises(BudgetExceeded, match="per-build cap"):
            budget.check()
        tracker.end_build("a")

    async def build_b() -> None:
        await a_recorded.wait()
        tracker.start_build("b")
        budget.record(_result(0.14))
        budget.check()
        b_checked.set()
        await asyncio.sleep(0)
        budget.check()
        tracker.end_build("b")

    await asyncio.gather(build_a(), build_b())


def test_close_build_releases_an_abandoned_context():
    budget = BudgetTracker(per_build_cap=1.0, daily_cap=10.0, token_cap=10_000)
    tracker = CostTracker(budget=budget)
    tracker.start_build("failed")
    budget.record(_result(0.1))

    tracker.close_build("failed")

    assert tracker._builds == {}


async def test_runner_closes_budget_context_when_early_delegate_fails(monkeypatch, tmp_path):
    budget = BudgetTracker(per_build_cap=1.0, daily_cap=10.0, token_cap=10_000)
    settings = Settings(
        _env_file=None,
        llm_backend="stub",
        data_dir=tmp_path,
        projects_dir=tmp_path / "projects",
    )

    class TrackedLLM:
        pass

    tracked_llm = TrackedLLM()
    tracked_llm.budget = budget
    tracker = CostTracker.from_llm(tracked_llm, settings)
    bus = EventBus()
    runner = StudioRunner(
        bus,
        Orchestrator(bus),
        settings=settings,
        cost_tracker=tracker,
    )

    async def fail_in_selector(brief, pin=None, llm=None, attended=False):
        del brief, pin, attended
        assert llm is tracked_llm
        budget.record(_result(0.1))
        raise RuntimeError("selector failed")

    monkeypatch.setattr("skyn3t.studio.stack_selector.select_stack", fail_in_selector)
    supplied_extra: dict[str, str] = {}
    with pytest.raises(RuntimeError, match="selector failed"):
        await runner.start("build an app", extra=supplied_extra)

    assert supplied_extra == {}
    assert budget.calls[-1].build_id
    assert tracker._builds == {}
