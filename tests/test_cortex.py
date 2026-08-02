"""Offline tests for the cortex autonomy layer.

No network, no heavy deps. Verifies proposal triage (auto-apply / gate /
dedupe), tuning apply, GEPA prompt evolution, repo-scout offline degrade,
the P2 CI/PR rework hooks, and the autonomous-loop guardrails (daily cap,
budget, heartbeat stall abort).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.cortex.autonomous_loop import AutonomousLoop, BuildHeartbeat, GuardrailState
from skyn3t.cortex.bootstrap import Cortex, build_cortex
from skyn3t.cortex.components import ReviewWatcher
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.prompt_evolver import PromptEvolver
from skyn3t.cortex.proposal_store import (
    Proposal,
    ProposalStatus,
    ProposalStore,
    ProposalType,
)
from skyn3t.cortex.repo_scout import RepoScout


def _settings(**over) -> Settings:
    base = dict(
        approval_gates=True,
        cortex_auto_approve_safe=True,
        autonomous_builds=False,
        autonomous_learning=True,
        autonomous_daily_build_cap=2,
        daily_usd_cap=5.0,
        # Isolate each Cortex's persisted proposal store (cortex now mirrors
        # proposals to data_dir/cortex/proposals.jsonl) so tests don't share
        # dedup state or write into the real ./data.
        data_dir=Path(tempfile.mkdtemp(prefix="skyn3t_cortex_test_")),
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


def test_applied_proposal_blocks_regeneration():
    # The "approved 50 times" bug: once a proposal is APPLIED, a recurring
    # generator that re-submits the SAME dedupe_key must be suppressed — re-
    # proposing something already enacted is pure approval noise.
    store = ProposalStore()
    p1, ok1 = store.add(Proposal(type=ProposalType.TUNING, title="bump n"))
    assert ok1 is True
    store.set_status(p1.id, ProposalStatus.APPLIED)
    p2, ok2 = store.add(Proposal(type=ProposalType.TUNING, title="bump n"))
    assert ok2 is False  # already applied -> not re-surfaced
    assert p2.status == ProposalStatus.REJECTED


def test_rejected_proposal_does_not_block_re_proposal():
    # A human REJECT (or a duplicate-reject) must not permanently block the key —
    # else nothing could ever be re-proposed after a no.
    store = ProposalStore()
    p1, _ = store.add(Proposal(type=ProposalType.TUNING, title="bump n"))
    store.set_status(p1.id, ProposalStatus.REJECTED)
    _, ok2 = store.add(Proposal(type=ProposalType.TUNING, title="bump n"))
    assert ok2 is True  # a prior rejection doesn't freeze the topic forever


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


async def test_safe_tuning_uses_ratchet_when_enabled():
    bus = EventBus()
    calls = []

    async def ratchet(prop):
        calls.append(prop.id)
        return {"kept": False, "reasons": ["go-rate regressed"]}

    cortex = Cortex(
        bus,
        settings=_settings(reliability_ratchet_enabled=True),
        ratchet_evaluator=ratchet,
    )
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.TUNING,
            title="set best_of_n",
            payload={"setting": "best_of_n", "value": 3},
            confidence=0.9,
            safe=True,
        )
    )

    assert calls == [prop.id]
    assert prop.status == ProposalStatus.FAILED
    assert prop.result["ratchet"]["kept"] is False
    assert cortex.handlers.overrides == {}


async def test_build_cortex_wires_default_ratchet_when_enabled(monkeypatch):
    from skyn3t.cortex.tuning_store import load_overrides
    from skyn3t.studio.bench import capture_regression_case

    bus = EventBus()
    settings = _settings(reliability_ratchet_enabled=True, best_of_n=1)
    capture_regression_case(
        settings.data_dir,
        "prior-failed-build",
        "a captured failure that must stay green",
        "python",
    )
    calls = []

    class FakeOrchestrator:
        @property
        def agents(self):
            return {}

    async def fake_evaluate_change(**kwargs):
        calls.append(kwargs["label"])
        assert any(c.id == "prior-failed-build" for c in kwargs["cases"])
        kwargs["apply_change"]()
        return {
            "kept": True,
            "reasons": [],
            "before": {"go_rate": 0.5},
            "after": {"go_rate": 1.0},
            "go_rate_delta": 0.5,
        }

    monkeypatch.setattr("skyn3t.cortex.ratchet.evaluate_change", fake_evaluate_change)

    cortex = build_cortex(bus, settings=settings, orchestrator=FakeOrchestrator())
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.TUNING,
            title="ratchet best_of_n",
            payload={"setting": "best_of_n", "value": 2},
            confidence=0.9,
            safe=True,
        )
    )

    assert calls == [f"ratchet-{prop.id[:8]}"]
    assert prop.status == ProposalStatus.APPLIED
    assert prop.result["ratchet"]["kept"] is True
    assert settings.best_of_n == 2
    assert load_overrides(settings.data_dir)["best_of_n"] == 2


async def test_default_ratchet_skips_bench_when_budget_exhausted(monkeypatch):
    """An already-exhausted budget would produce two equally-dead bench runs
    (every case dies on BudgetExceeded) that gate_change could accept on
    garbage evidence — the evaluator must preflight and skip the 2x bench."""
    from types import SimpleNamespace

    from skyn3t.adapters.llm import BudgetExceeded

    bus = EventBus()
    settings = _settings(reliability_ratchet_enabled=True)

    class FakeOrchestrator:
        @property
        def agents(self):
            return {}

    class ExhaustedBudget:
        def check(self):
            raise BudgetExceeded("daily usd cap 5.0 exceeded (5.2)")

    bench_calls = []

    async def fake_evaluate_change(**kwargs):
        bench_calls.append(kwargs["label"])
        return {"kept": True, "reasons": []}

    monkeypatch.setattr("skyn3t.cortex.ratchet.evaluate_change", fake_evaluate_change)

    cortex = build_cortex(
        bus, settings=settings, orchestrator=FakeOrchestrator(),
        llm=SimpleNamespace(budget=ExhaustedBudget()),
    )
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.TUNING,
            title="ratchet best_of_n",
            payload={"setting": "best_of_n", "value": 2},
            confidence=0.9,
            safe=True,
        )
    )

    assert bench_calls == []  # the 2x bench never ran
    assert prop.status == ProposalStatus.FAILED
    assert any(
        "insufficient budget" in str(r) for r in prop.result["ratchet"]["reasons"]
    )


async def test_default_ratchet_bounds_cases_and_wires_budget_scoping(monkeypatch):
    """The auto-ratchet bench must (1) cap the suite at DEFAULT_CASES plus only
    the MOST RECENT app regressions (all_cases could pull in up to 200 — one
    auto-approved proposal then meant 2x(20+200) unguarded real builds), and
    (2) build runners with the same cost/budget wiring the CLI bench uses, so
    per-build spend is scoped per case instead of accumulating across the
    whole before+after bench."""
    from types import SimpleNamespace

    from skyn3t.studio.bench import DEFAULT_CASES, capture_regression_case

    bus = EventBus()
    settings = _settings(reliability_ratchet_enabled=True, best_of_n=1)
    for i in range(12):
        capture_regression_case(
            settings.data_dir, f"regression-{i:02d}", f"a captured failure number {i}", "python"
        )

    class FakeOrchestrator:
        @property
        def agents(self):
            return {}

    resets = []

    class FakeBudget:
        def check(self):
            return None

        def reset_build(self):
            resets.append(True)

    captured = {}

    async def fake_evaluate_change(**kwargs):
        captured["cases"] = kwargs["cases"]
        captured["make_build_fn"] = kwargs["make_build_fn"]
        kwargs["apply_change"]()
        return {
            "kept": True, "reasons": [],
            "before": {"go_rate": 0.5}, "after": {"go_rate": 1.0}, "go_rate_delta": 0.5,
        }

    monkeypatch.setattr("skyn3t.cortex.ratchet.evaluate_change", fake_evaluate_change)

    llm = SimpleNamespace(budget=FakeBudget())
    cortex = build_cortex(bus, settings=settings, orchestrator=FakeOrchestrator(), llm=llm)
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.TUNING,
            title="ratchet best_of_n",
            payload={"setting": "best_of_n", "value": 2},
            confidence=0.9,
            safe=True,
        )
    )
    assert prop.status == ProposalStatus.APPLIED

    # Bounded: the built-in exam + the 10 most recent captured regressions.
    case_ids = [c.id for c in captured["cases"]]
    assert len(captured["cases"]) == len(DEFAULT_CASES) + 10
    assert "regression-11" in case_ids  # most recent kept
    assert "regression-00" not in case_ids  # oldest dropped

    # The build_fn wires cost/budget observability and scopes spend per case.
    runner_kwargs = {}

    class FakeRunner:
        def __init__(self, _bus, _orch, **kwargs):
            runner_kwargs.update(kwargs)

        async def start(self, _brief, slug=None, extra=None):
            return SimpleNamespace(verdict="go", score=90.0)

    monkeypatch.setattr("skyn3t.studio.runner.StudioRunner", FakeRunner)
    build_fn = captured["make_build_fn"]()
    await build_fn(captured["cases"][0])
    assert runner_kwargs["cost_tracker"] is not None
    assert runner_kwargs["budget_guard"] is not None
    assert resets, "per-case budget reset did not run"


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


async def test_proven_safe_gated_feature_auto_applies_only_when_ratchet_keeps():
    bus = EventBus()
    calls = []

    async def ratchet(prop):
        calls.append(prop.id)
        return {"kept": True, "reasons": ["bench improved"]}

    cortex = Cortex(
        bus,
        settings=_settings(reliability_ratchet_enabled=True),
        ratchet_evaluator=ratchet,
    )
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.FEATURE,
            title="enable safe proven feature",
            payload={"proven_safe": True},
            confidence=0.95,
            safe=True,
        )
    )

    assert calls == [prop.id]
    assert prop.status == ProposalStatus.APPLIED
    assert prop.result["ratchet"]["kept"] is True


async def test_proven_safe_gated_feature_fails_when_ratchet_rejects():
    bus = EventBus()

    async def ratchet(prop):
        return {"kept": False, "reasons": ["go-rate regressed"]}

    cortex = Cortex(
        bus,
        settings=_settings(reliability_ratchet_enabled=True),
        ratchet_evaluator=ratchet,
    )
    prop = await cortex.submit(
        Proposal(
            type=ProposalType.FEATURE,
            title="reject unsafe regression",
            payload={"proven_safe": True},
            confidence=0.95,
            safe=True,
        )
    )

    assert prop.status == ProposalStatus.FAILED
    assert prop.result["ratchet"]["kept"] is False


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


async def test_prompt_evolver_uses_completed_build_manifest_prompts():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    ev = PromptEvolver(cortex=cortex)
    manifest = {
        "slug": "demo",
        "brief": "Build a polished dashboard",
        "stack": "react_vite",
        "score": 58.0,
        "verdict": "no_go",
        "extra": {
            "prompts": [{"stage": "codegen", "text": "Write code."}],
        },
        "stages": [
            {
                "name": "review",
                "status": "completed",
                "output_summary": {"gaps": ["missing empty states", "no tests"]},
            }
        ],
    }

    best = await ev.evolve_from_manifest(manifest)

    assert best is not None
    assert best.text != "Write code."
    gated = cortex.store.gated()
    prop = next(p for p in gated if p.source == "prompt_evolver")
    assert prop.payload["prompt_key"] == "demo:codegen"
    assert prop.payload["tasks"][0]["gaps"] == ["missing empty states", "no tests"]


async def test_prompt_evolver_ignores_malformed_manifest_gaps_string():
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings())
    ev = PromptEvolver(cortex=cortex)
    manifest = {
        "slug": "demo",
        "extra": {"prompts": [{"stage": "codegen", "text": "Write code."}]},
        "stages": [{"output_summary": {"gaps": "missing tests"}}],
    }

    best = await ev.evolve_from_manifest(manifest)

    assert best is not None
    prop = next(p for p in cortex.store.gated() if p.source == "prompt_evolver")
    assert prop.payload["tasks"][0]["gaps"] == []


# ---- repo scout ------------------------------------------------------------
async def test_repo_scout_offline():
    scout = RepoScout(settings=_settings())
    props = await scout.scout("web framework")
    assert props
    assert all(p.type == ProposalType.INGEST for p in props)
    assert all(p.safe is False for p in props)


async def test_repo_scout_run_warns_honestly_when_unauthenticated():
    # The token note fires only when the scout actually runs (cortex.start
    # path), and says what the token really does: raise the rate limit.
    from structlog.testing import capture_logs

    scout = RepoScout(settings=_settings())  # autonomous_learning=True, no token
    assert scout.github_token is None
    with capture_logs() as logs:
        task = asyncio.create_task(scout.run())
        await asyncio.sleep(0.05)  # let run() reach its pre-loop warning
        task.cancel()
        await task  # run() swallows CancelledError and returns
    warns = [e for e in logs if e.get("event") == "cortex.scout_unauthenticated"]
    assert len(warns) == 1
    assert "rate limit" in warns[0]["hint"]
    assert "works without a token" in warns[0]["hint"]


async def test_repo_scout_run_silent_when_learning_off():
    from structlog.testing import capture_logs

    scout = RepoScout(settings=_settings(autonomous_learning=False))
    with capture_logs() as logs:
        await scout.run()  # gated off -> returns before any scout logging
    assert not [e for e in logs if "scout" in str(e.get("event", "")).lower()]


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


@pytest.mark.parametrize("disabled_cap", [0, -1])
async def test_loop_allows_builds_when_daily_build_cap_is_disabled(disabled_cap):
    bus = EventBus()
    s = _settings(
        autonomous_builds=True,
        autonomous_daily_build_cap=disabled_cap,
        daily_usd_cap=0,
    )
    loop = AutonomousLoop(Cortex(bus, settings=s), bus, settings=s)
    for _ in range(25):
        loop.state.record_build_start()

    allowed, reason = loop.can_start_build()

    assert allowed is True
    assert reason == ""


async def test_loop_blocks_when_budget_exhausted():
    bus = EventBus()
    s = _settings(autonomous_builds=True, daily_usd_cap=1.0)
    loop = AutonomousLoop(Cortex(bus, settings=s), bus, settings=s)
    loop.state.record_spend(1.5)
    allowed, reason = loop.can_start_build()
    assert allowed is False
    assert "USD cap" in reason


async def test_loop_allows_spend_when_daily_usd_cap_is_disabled():
    bus = EventBus()
    s = _settings(
        autonomous_builds=True,
        autonomous_daily_build_cap=10,
        daily_usd_cap=0,
        per_build_usd_cap=100,
    )
    loop = AutonomousLoop(Cortex(bus, settings=s), bus, settings=s)
    loop.state.record_spend(500)
    loop.state.reserve("inflight", 100)

    allowed, reason = loop.can_start_build()

    assert allowed is True
    assert reason == ""


async def test_loop_blocks_on_in_flight_reservation_before_spend_recorded():
    """Pre-charge guard: an in-flight build's escrow must block a concurrent
    launch even before any real spend is recorded — otherwise two builds each
    see spend==0 and collectively overshoot the daily cap."""
    bus = EventBus()
    s = _settings(autonomous_builds=True, daily_usd_cap=1.0, per_build_usd_cap=0.6)
    loop = AutonomousLoop(Cortex(bus, settings=s), bus, settings=s)
    # Nothing has actually been spent yet...
    assert loop.state.spend_usd == 0.0
    # ...but a build is in flight with an escrowed estimate.
    loop.state.reserve("inflight", loop._next_build_estimate())  # 0.6 reserved
    allowed, reason = loop.can_start_build()
    # Projected = reserved 0.6 + next est 0.6 = 1.2 > 1.0 cap → blocked.
    assert allowed is False
    assert "would be exceeded" in reason
    # Releasing the in-flight reservation frees the next launch again.
    loop.state.release("inflight")
    allowed2, _ = loop.can_start_build()
    assert allowed2 is True


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


async def test_gated_tuner_escalates_best_of_n_past_an_applied_bump():
    """The dedupe key is value-bearing: an APPLIED bump to 3 must not block the
    next rung (value 4) of the escalation ladder as a 'duplicate' — the
    title-derived constant key made the designed low-score -> more-samples
    ladder one-shot per installation. Identical values still dedupe."""
    from skyn3t.cortex.components import GatedTuner

    bus = EventBus()
    settings = _settings(best_of_n=2)
    cortex = Cortex(bus, settings=settings)
    tuner = GatedTuner(cortex, bus, settings=settings)
    await tuner.run()

    def applied():
        return [p for p in cortex.store.all() if p.status is ProposalStatus.APPLIED]

    # First trigger: bump 2 -> 3, auto-applied.
    for _ in range(3):
        await bus.emit(EventType.BUILD_STAGE_COMPLETED, "studio", {"stage": "review", "score": 40.0})
    assert [p.payload["value"] for p in applied()] == [3]
    assert settings.best_of_n == 3

    # Scores stay low: the next trigger must escalate to 4, not be rejected
    # as a duplicate of the already-applied value-3 proposal.
    for _ in range(3):
        await bus.emit(EventType.BUILD_STAGE_COMPLETED, "studio", {"stage": "review", "score": 40.0})
    assert sorted(p.payload["value"] for p in applied()) == [3, 4]
    assert settings.best_of_n == 4


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
