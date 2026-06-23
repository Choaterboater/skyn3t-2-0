"""Offline tests for the cortex autonomy layer.

No network, no heavy deps. Verifies proposal triage (auto-apply / gate /
dedupe), tuning apply, GEPA prompt evolution, repo-scout offline degrade,
the P2 CI/PR rework hooks, and the autonomous-loop guardrails (daily cap,
budget, heartbeat stall abort).
"""

from __future__ import annotations

import asyncio

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.cortex.autonomous_loop import AutonomousLoop, BuildHeartbeat, GuardrailState
from skyn3t.cortex.bootstrap import Cortex
from skyn3t.cortex.components import ReviewWatcher
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import (
    Proposal,
    ProposalStatus,
    ProposalStore,
    ProposalType,
)
from skyn3t.cortex.prompt_evolver import PromptEvolver
from skyn3t.cortex.repo_scout import RepoScout


def _settings(**over) -> Settings:
    base = dict(
        approval_gates=True,
        cortex_auto_approve_safe=True,
        autonomous_builds=False,
        autonomous_learning=True,
        autonomous_daily_build_cap=2,
        daily_usd_cap=5.0,
    )
    base.update(over)
    return Settings(**base)


# ---- proposal store --------------------------------------------------------
def test_proposal_store_dedup():
    store = ProposalStore()
    p1, ok1 = store.add(Proposal(type=ProposalType.TUNING, title="bump n"))
    p2, ok2 = store.add(Proposal(type=ProposalType.TUNING, title="bump n"))
    assert ok1 is True
    assert ok2 is False
    assert p2.status == ProposalStatus.REJECTED
    assert p2.decision_reason == "duplicate"


def test_proposal_roundtrip():
    p = Proposal(type=ProposalType.INGEST, title="x", confidence=2.0)
    assert p.confidence == 1.0  # clamped
    back = Proposal.from_dict(p.to_dict())
    assert back.id == p.id
    assert back.type == ProposalType.INGEST


# ---- triage ----------------------------------------------------------------
async def test_safe_tuning_auto_applies():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.TUNING,
            title="set best_of_n",
            payload={"setting": "best_of_n", "value": 3},
            confidence=0.9,
            safe=True,
        )
    )
    assert prop.status == ProposalStatus.APPLIED
    assert cortex.handlers.overrides["best_of_n"] == 3


async def test_apply_is_idempotent_no_double_apply():
    # Re-approving an already-APPLIED proposal must not re-run its handler
    # (the handler could duplicate a tuning change or emit conflicting events).
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    calls = {"n": 0}

    async def counting_handler(prop):
        calls["n"] += 1
        return {"applied": True}

    cortex.handlers.register(ProposalType.TUNING, counting_handler)
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.TUNING,
            title="x",
            payload={"setting": "a", "value": 1},
            confidence=0.9,
            safe=True,
        )
    )
    assert prop.status == ProposalStatus.APPLIED
    assert calls["n"] == 1
    again = await cortex.approve(prop.id)
    assert again.status == ProposalStatus.APPLIED
    assert calls["n"] == 1  # NOT re-applied


def test_prompt_text_hash_is_stable_across_processes():
    import os
    import subprocess
    import sys

    from skyn3t.cortex.prompt_evolver import _text_hash

    h1 = _text_hash("evolve the planner prompt")
    # A fresh interpreter with a different hash seed must produce the same key —
    # builtin hash() would differ (PYTHONHASHSEED randomization).
    env = {**os.environ, "PYTHONHASHSEED": "98765"}
    out = subprocess.run(
        [sys.executable, "-c",
         "from skyn3t.cortex.prompt_evolver import _text_hash;"
         "print(_text_hash('evolve the planner prompt'))"],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == h1


async def test_feature_is_gated():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    prop = await cortex.submit(
        Proposal(type=ProposalType.FEATURE, title="add x", confidence=0.99, safe=True)
    )
    # Feature type is always gated regardless of safe/confidence.
    assert prop.status == ProposalStatus.GATED


async def test_unsafe_tuning_is_gated():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    prop = await cortex.submit(
        Proposal(type=ProposalType.TUNING, title="risky", confidence=0.99, safe=False)
    )
    assert prop.status == ProposalStatus.GATED


async def test_low_confidence_safe_is_gated():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    prop = await cortex.submit(
        Proposal(type=ProposalType.TUNING, title="lowconf", confidence=0.2, safe=True)
    )
    assert prop.status == ProposalStatus.GATED


async def test_human_approve_applies():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.TUNING,
            title="manual",
            payload={"overrides": {"debate_enabled": True}},
            confidence=0.2,
            safe=True,
        )
    )
    assert prop.status == ProposalStatus.GATED
    applied = await cortex.approve(prop.id)
    assert applied.status == ProposalStatus.APPLIED
    assert cortex.handlers.overrides["debate_enabled"] is True


async def test_proposal_events_emitted():
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(EventType.PROPOSAL_CREATED, lambda e: seen.append(e) or _noop())
    cortex = Cortex(bus, settings=_settings())
    await cortex.submit(Proposal(type=ProposalType.TUNING, title="evt", safe=True, confidence=0.9))
    assert any(e.type == EventType.PROPOSAL_CREATED for e in seen)


async def _noop():
    return None


# ---- handlers --------------------------------------------------------------
async def test_handler_stage_memory():
    reg = HandlerRegistry()  # no stage_dir -> in-memory
    res = await reg.apply(Proposal(type=ProposalType.INGEST, title="t"))
    assert res["applied"] is True
    assert res["staged"] == "memory"


# ---- prompt evolver --------------------------------------------------------
async def test_prompt_evolver_improves_and_gates():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    ev = PromptEvolver(cortex=cortex)
    best = await ev.evolve("Write code.", prompt_key="codegen")
    assert best.score >= 0.0
    # Evolved prompt should beat a bare baseline.
    assert best.text != "Write code."
    gated = cortex.store.gated()
    assert any(p.type == ProposalType.TUNING for p in gated)


# ---- repo scout ------------------------------------------------------------
async def test_repo_scout_offline():
    scout = RepoScout(settings=_settings())
    props = await scout.scout("web framework")
    assert props
    assert all(p.type == ProposalType.INGEST for p in props)
    assert all(p.safe is False for p in props)


# ---- P2 review watcher -----------------------------------------------------
async def test_ci_failure_creates_gated_patch():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    rw = ReviewWatcher(cortex, bus, settings=_settings())
    prop = await rw.ingest_ci_failure({"run_id": "42", "log": "boom", "repo": "a/b"})
    assert prop.type == ProposalType.CODE_PATCH
    assert prop.status == ProposalStatus.GATED
    # Duplicate run id is ignored.
    dup = await rw.ingest_ci_failure({"run_id": "42", "log": "boom"})
    assert dup is None


async def test_pr_comment_creates_patch():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    rw = ReviewWatcher(cortex, bus, settings=_settings())
    prop = await rw.ingest_pr_comment({"comment_id": "9", "body": "rename this"})
    assert prop.status == ProposalStatus.GATED


# ---- autonomous loop guardrails -------------------------------------------
def test_guardrail_daily_cap():
    st = GuardrailState()
    st.record_build_start()
    st.record_build_start()
    assert st.builds_today() == 2


async def test_loop_blocks_when_disabled():
    bus = EventBus()
    s = _settings(autonomous_builds=False)
    loop = AutonomousLoop(Cortex(bus, settings=s), bus, settings=s)
    allowed, reason = loop.can_start_build()
    assert allowed is False
    assert "disabled" in reason


async def test_loop_blocks_at_daily_cap():
    bus = EventBus()
    s = _settings(autonomous_builds=True, autonomous_daily_build_cap=1)
    loop = AutonomousLoop(Cortex(bus, settings=s), bus, settings=s)
    loop.state.record_build_start()
    allowed, reason = loop.can_start_build()
    assert allowed is False
    assert "daily build cap" in reason


async def test_loop_blocks_when_budget_exhausted():
    bus = EventBus()
    s = _settings(autonomous_builds=True, daily_usd_cap=1.0)
    loop = AutonomousLoop(Cortex(bus, settings=s), bus, settings=s)
    loop.state.record_spend(1.5)
    allowed, reason = loop.can_start_build()
    assert allowed is False
    assert "USD cap" in reason


async def test_heartbeat_stall_aborts():
    bus = EventBus()
    s = _settings(autonomous_builds=True)
    loop = AutonomousLoop(
        Cortex(bus, settings=s), bus, settings=s, stall_idle_seconds=0.0
    )
    cid = "build-1"
    loop.heartbeat.start(cid)

    async def never_finishes():
        await asyncio.sleep(60)

    task = asyncio.create_task(never_finishes())
    loop._active[cid] = task
    # idle threshold is 0 -> immediately considered stalled.
    await asyncio.sleep(0.01)
    aborted = await loop.check_stalls()
    assert cid in aborted
    # Let the cancellation propagate to the cancelled task.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


def test_build_heartbeat_basic():
    hb = BuildHeartbeat()
    hb.start("x")
    assert hb.stalled("x", 1000) is False
    assert hb.stalled("missing", 0) is False


async def test_gated_tuner_counts_low_scores_per_stage():
    """A global counter conflated stages; now N consecutive low scores must be
    on the SAME stage before GatedTuner proposes bumping best_of_n."""
    from skyn3t.cortex.components import GatedTuner

    class _CaptureCortex:
        def __init__(self) -> None:
            self.submitted: list = []

        async def submit(self, proposal):
            self.submitted.append(proposal)
            return proposal

    bus = EventBus()
    cortex = _CaptureCortex()
    tuner = GatedTuner(cortex, bus, settings=_settings(best_of_n=1))
    await tuner.run()

    # Low scores spread across DIFFERENT stages must NOT trigger.
    for stage in ("architect", "code", "design"):
        await bus.emit(EventType.BUILD_STAGE_COMPLETED, "studio", {"stage": stage, "score": 40.0})
    assert cortex.submitted == []

    # Three consecutive low scores on the SAME stage trigger exactly one bump.
    for _ in range(3):
        await bus.emit(EventType.BUILD_STAGE_COMPLETED, "studio", {"stage": "review", "score": 40.0})
    assert len(cortex.submitted) == 1
    assert cortex.submitted[0].payload == {"setting": "best_of_n", "value": 2}

    # A passing score resets that stage's counter, so 2 more lows don't re-fire.
    await bus.emit(EventType.BUILD_STAGE_COMPLETED, "studio", {"stage": "review", "score": 85.0})
    for _ in range(2):
        await bus.emit(EventType.BUILD_STAGE_COMPLETED, "studio", {"stage": "review", "score": 40.0})
    assert len(cortex.submitted) == 1


async def test_reflection_loop_closes_tuning_loop():
    """A finished no_go build flows reflection -> KNOWLEDGE_UPDATED suggestions
    -> SelfTuningEngine, enabling reflective_retry on a live agent."""
    from skyn3t.cortex.components import ReflectionLoop
    from skyn3t.memory.tuner import SelfTuningEngine

    class _FakeAgent:
        def __init__(self) -> None:
            self.config = {"reflective_retry": False}

    class _NullCortex:
        async def submit(self, proposal):
            return proposal

    bus = EventBus()
    agent = _FakeAgent()
    tuner = SelfTuningEngine(bus, {"coder": agent})
    tuner.start()
    loop = ReflectionLoop(_NullCortex(), bus, settings=_settings())
    await loop.run()  # subscribe to BUILD_COMPLETED

    await bus.emit(
        EventType.BUILD_COMPLETED, "studio",
        {"slug": "app", "stack": "fastapi", "score": 30.0, "verdict": "no_go"},
    )
    assert agent.config["reflective_retry"] is True

    # A successful build must NOT churn configs (no suggestions emitted).
    agent.config["reflective_retry"] = False
    await bus.emit(
        EventType.BUILD_COMPLETED, "studio",
        {"slug": "app2", "stack": "fastapi", "score": 95.0, "verdict": "go"},
    )
    assert agent.config["reflective_retry"] is False
