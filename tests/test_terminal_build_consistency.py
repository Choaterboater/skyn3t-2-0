"""Terminal build cost/verdict must agree across manifest, memory, and live API."""

from __future__ import annotations

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner
from skyn3t.web.deps import AppState


class _RejectingGuard:
    def reset(self):
        return None

    def attach(self, event_bus):
        return None

    def detach(self):
        return None

    def heartbeat(self):
        return None

    def check(self):
        raise RuntimeError("daily token cap exceeded")


class _TerminalCostTracker:
    llm = None

    def __init__(self):
        self.active = False
        self.closed = 0

    def start_build(self, build_id):
        self.active = True

    def end_build(self, build_id):
        assert self.active is True
        self.active = False
        self.closed += 1
        return {
            "build_id": build_id,
            "cost_usd": 0.522486,
            "tokens": 1_044_973,
            "duration_s": 12.0,
            "stages": [{"stage": "code", "cost_usd": 0.522486, "tokens": 1_044_973}],
        }

    def close_build(self, build_id):
        if self.active:
            self.end_build(build_id)


class _Memory:
    def __init__(self):
        self.saved = []

    async def relevant_lessons(self, *args, **kwargs):
        return []

    async def save_build(self, **fields):
        self.saved.append(fields)


async def test_failed_runner_settles_cost_before_all_terminal_persistence(tmp_path):
    settings = Settings(
        _env_file=None,
        llm_backend="stub",
        projects_dir=tmp_path / "projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        asset_gen=False,
        approval_gates=False,
    )
    bus = EventBus()
    state = AppState(
        settings=settings,
        event_bus=bus,
        llm_client=object(),
        router=object(),
    )
    memory = _Memory()
    costs = _TerminalCostTracker()
    runner = StudioRunner(
        bus,
        Orchestrator(bus),
        settings=settings,
        memory=memory,
        cost_tracker=costs,
        budget_guard=_RejectingGuard(),
    )

    outcome = await runner.start(
        "Build a Python command line utility",
        slug="terminal-consistency",
        extra={"stack": "python", "build_id": "terminal-build"},
    )

    project_dir = settings.projects_dir / outcome.slug
    disk_manifest = BuildManifest.load(project_dir)
    assert disk_manifest is not None
    assert outcome.status == disk_manifest.status == "failed"
    assert outcome.verdict == disk_manifest.verdict == "no_go"
    assert outcome.cost_usd == disk_manifest.cost_usd == pytest.approx(0.522486)
    assert disk_manifest.extra["build_cost_usd"] == pytest.approx(0.522486)
    assert disk_manifest.extra["wasted_usd"] == pytest.approx(0.522486)
    assert costs.closed == 1

    persisted = memory.saved[-1]
    assert persisted["status"] == "failed"
    assert persisted["verdict"] == "no_go"
    assert persisted["cost_usd"] == pytest.approx(0.522486)
    assert persisted["manifest"]["cost_usd"] == pytest.approx(0.522486)

    live = state.builds["terminal-build"]
    assert live.status == "failed"
    assert live.verdict == "no_go"
    assert live.cost_usd == pytest.approx(0.522486)
