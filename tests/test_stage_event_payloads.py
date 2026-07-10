"""Build events carry the keys their consumers read (capability/gaps/stack/brief)."""

from __future__ import annotations

import asyncio

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest, StageRecord
from skyn3t.studio.planner import Planner
from skyn3t.studio.runner import StudioRunner


def _runner():
    bus = EventBus()
    return bus, StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def test_stage_completed_carries_capability_and_gaps():
    async def go():
        bus, runner = _runner()
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.BUILD_STAGE_COMPLETED, cap)
        rec = StageRecord(name="review", agent_type="reviewer", capability="review")
        rec.output_summary = {"gaps": ["no recognizable entrypoint", "no dependency manifest"]}
        await runner._emit_stage_done("b1", rec, "cid")
        assert seen, "a BUILD_STAGE_COMPLETED event should be emitted"
        p = seen[-1].payload
        assert p["capability"] == "review"  # dashboard stage-axis match (HIGH-3)
        assert p["gaps"] == ["no recognizable entrypoint", "no dependency manifest"]  # FeatureSuggester (HIGH-4)

    asyncio.run(go())


def test_stage_completed_without_gaps_omits_key():
    async def go():
        bus, runner = _runner()
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.BUILD_STAGE_COMPLETED, cap)
        rec = StageRecord(name="code", agent_type="code", capability="codegen")
        await runner._emit_stage_done("b1", rec, "cid")
        p = seen[-1].payload
        assert p["capability"] == "codegen"
        assert "gaps" not in p  # additive: no junk key when there are no gaps

    asyncio.run(go())


def test_stage_completed_carries_agent_name_and_duration():
    # "Which agent ran each stage, and how long" is recorded on the StageRecord
    # but was only reachable post-build via the manifest — surface it live so the
    # dashboard's stage ledger is legible during the build.
    async def go():
        bus, runner = _runner()
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.BUILD_STAGE_COMPLETED, cap)
        rec = StageRecord(name="code", agent_type="code", capability="codegen",
                          agent_name="deepseek-coder", duration_ms=1234.0)
        rec.status = "completed"
        await runner._emit_stage_done("b1", rec, "cid")
        p = seen[-1].payload
        assert p["agent_name"] == "deepseek-coder"
        assert p["duration_ms"] == 1234.0

    asyncio.run(go())


def test_stage_completed_persists_backend_and_cost_truth_on_record():
    async def go():
        bus, runner = _runner()
        seen = []

        class Tracker:
            def end_stage(self, _build_id, _stage):
                return {
                    "stage": "code",
                    "cost_usd": 0.0,
                    "tokens": 123,
                    "call_count": 1,
                    "backend": "codex_cli",
                    "backends": {"codex_cli": 1},
                    "models": ["gpt-5.6-codex"],
                    "cost_source_counts": {"not_reported_by_cli": 1},
                    "cost_usd_known": False,
                    "cost_classification": "unknown",
                    "failed_attempts": 0,
                    "max_unconfirmed_exposure_usd": 0.0,
                }

        runner.cost_tracker = Tracker()

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.BUILD_STAGE_COMPLETED, cap)
        rec = StageRecord(name="code", agent_type="code", capability="codegen")
        rec.output_summary = {
            "execution": {
                "backend": "codex_cli",
                "task": {
                    "build_id": "build-1",
                    "worktree_role": "main",
                },
            }
        }
        await runner._emit_stage_done("build-1", rec, "cid")

        truth = rec.output_summary["cost_truth"]
        assert truth["backend"] == "codex_cli"
        assert truth["cost_usd_known"] is False
        assert truth["cost_classification"] == "unknown"
        assert seen[-1].payload["cost_truth"] == truth
        assert seen[-1].payload["backend"] == "codex_cli"

    asyncio.run(go())


def test_stage_completed_omits_agent_and_duration_when_absent():
    async def go():
        bus, runner = _runner()
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.BUILD_STAGE_COMPLETED, cap)
        # No agent_name; duration stays 0.0 (e.g. a failed stage) -> both omitted,
        # so the payload never carries null junk keys.
        rec = StageRecord(name="code", agent_type="code", capability="codegen")
        await runner._emit_stage_done("b1", rec, "cid")
        p = seen[-1].payload
        assert "agent_name" not in p
        assert "duration_ms" not in p

    asyncio.run(go())


def test_build_completed_carries_stack_and_brief(tmp_path):
    async def go():
        bus, runner = _runner()
        seen = []

        async def cap(ev):
            seen.append(ev)

        bus.subscribe(EventType.BUILD_COMPLETED, cap)
        m = BuildManifest(slug="s", brief="a fastapi service with items", stack="fastapi")
        m.artifact_dir = str(tmp_path)
        plan = Planner(Settings()).plan("a fastapi service with items", "s")
        await runner._finalize(m, plan, "cid", 88.0)
        assert seen, "a BUILD_COMPLETED event should be emitted"
        p = seen[-1].payload
        assert p["stack"] == "fastapi"  # ExperienceIngestor tags + recall key (HIGH-5)
        assert p["brief"] == "a fastapi service with items"

    asyncio.run(go())
