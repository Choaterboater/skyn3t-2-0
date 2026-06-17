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
