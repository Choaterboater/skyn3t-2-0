"""Offline tests for the platform package (security/observability/persistence/
registry/self_healing). No network, no heavy deps required."""

from __future__ import annotations

import asyncio

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.core.events import EventBus, EventType


# ---- security: secrets ---------------------------------------------------
def test_filter_env_strips_secrets():
    from skyn3t.security.secrets import filter_env

    env = {"PATH": "/bin", "OPENAI_API_KEY": "sk-secret", "MY_TOKEN": "abc", "FOO": "bar"}
    clean = filter_env(env)
    assert "PATH" in clean and clean["FOO"] == "bar"
    assert "OPENAI_API_KEY" not in clean
    assert "MY_TOKEN" not in clean


def test_secrets_redaction():
    from skyn3t.security.secrets import SecretsStore, scrub_text

    store = SecretsStore()
    store.put("MY_KEY", "supersecretvalue123")
    assert store.redact("token is supersecretvalue123 here") == "token is ***REDACTED*** here"
    # pattern-based scrub even without the store knowing it
    assert "***REDACTED***" in scrub_text("leaked sk-abcdefghijklmnopqrstuvwx")


# ---- security: audit -----------------------------------------------------
def test_audit_chain(tmp_path):
    from skyn3t.security.audit import AuditLog

    log = AuditLog(path=tmp_path / "audit.jsonl")
    log.record("sandbox_exec", actor="agent", detail={"cmd": "pytest"})
    log.record("permission", actor="cortex", outcome="deny")
    assert log.verify() is True
    records = log.read()
    assert len(records) == 2
    # Tamper -> chain breaks
    (tmp_path / "audit.jsonl").write_text(
        (tmp_path / "audit.jsonl").read_text().replace("sandbox_exec", "evil")
    )
    log2 = AuditLog(path=tmp_path / "audit.jsonl")
    assert log2.verify() is False


# ---- security: permissions ----------------------------------------------
def test_permissions_classify():
    from skyn3t.security.permissions import Decision, PermissionManager

    pm = PermissionManager()
    assert pm.classify("delete_path") is Decision.NEEDS_APPROVAL
    # safe action auto-approved when cortex_auto_approve_safe is default True
    assert pm.classify("read_file") in (Decision.ALLOW, Decision.NEEDS_APPROVAL)


def test_permissions_check_fail_closed():
    from skyn3t.security.permissions import PermissionManager

    pm = PermissionManager()
    # dangerous action with default deny approver -> denied
    assert asyncio.run(pm.check("delete_path")) is False
    pm.grant("agent", "delete_path")
    assert asyncio.run(pm.check("delete_path", actor="agent")) is True


# ---- security: sandbox ---------------------------------------------------
def test_sandbox_runs_offline():
    from skyn3t.security.sandbox import SandboxRunner, available_stacks

    runner = SandboxRunner()
    assert "python" in available_stacks()
    # warm_start returns None gracefully if docker absent
    res = runner.warm_start_image("python")
    assert res is None or isinstance(res, str)
    # docker_available never raises
    assert isinstance(runner.docker_available(), bool)


def test_sandbox_subprocess_fallback_warns():
    from skyn3t.security.sandbox import SandboxRunner

    runner = SandboxRunner()
    if runner.docker_available():
        pytest.skip("docker present; fallback path not exercised")
    with pytest.warns(RuntimeWarning):
        result = asyncio.run(runner.run(["echo", "hi"], timeout=10))
    assert "hi" in result.stdout
    assert result.warning is not None


# ---- observability: metrics ---------------------------------------------
def test_metrics_noop_or_real():
    from skyn3t.observability.metrics import get_metrics

    m = get_metrics()
    m.tasks_total.labels("codegen", "ok").inc()
    m.budget_remaining.set(1.23)
    assert isinstance(m.render(), bytes)


# ---- observability: cost tracker ----------------------------------------
def test_cost_tracker_with_fake_budget():
    from skyn3t.observability.cost_tracker import CostTracker

    class FakeCall:
        backend = "stub"; model = "x"; prompt_tokens = 10
        completion_tokens = 5; cost_usd = 0.001

    class FakeBudget:
        spent_build = 0.001; spent_day = 0.001; tokens_day = 15
        calls = [FakeCall()]

    ct = CostTracker(budget=FakeBudget())
    ct.start_build("b1")
    rep = ct.report()
    assert rep["spent_day_usd"] >= 0.0
    out = ct.end_build("b1")
    assert out["build_id"] == "b1"


# ---- observability: traces ----------------------------------------------
def test_tracer_spans(tmp_path):
    from skyn3t.observability.traces import Tracer

    tr = Tracer(path=tmp_path / "traces.jsonl")
    with tr.span("stage", stage="code"):
        pass
    traj = tr.trajectory()
    assert traj and traj[0]["name"] == "stage"
    assert (tmp_path / "traces.jsonl").exists()


def test_tracer_event_bus(tmp_path):
    from skyn3t.observability.traces import Tracer

    bus = EventBus()
    tr = Tracer(path=tmp_path / "t.jsonl")
    tr.attach_event_bus(bus)
    asyncio.run(bus.emit(EventType.SYSTEM, "test", {"k": "v"}))
    assert any(s["name"] == "event:system" for s in tr.trajectory())


# ---- observability: health ----------------------------------------------
def test_doctor_runs():
    from skyn3t.observability.health import doctor

    report = asyncio.run(doctor())
    assert report["status"] in ("ok", "degraded", "fail")
    names = {c["name"] for c in report["checks"]}
    assert {"docker", "llm", "dirs", "optional_deps"} <= names


# ---- persistence: checkpoint + recovery (3-mode) ------------------------
def test_checkpoint_save_load(tmp_path):
    from skyn3t.persistence.checkpoint import CheckpointManager

    s = get_settings()
    mgr = CheckpointManager(settings=s)
    mgr._dir = tmp_path / "cp"
    bus = EventBus()
    asyncio.run(bus.emit(EventType.SYSTEM, "boot", {}))
    mgr.save("boot", event_bus=bus, state={"files": ["a.py"], "task": {"stage": "code"}})
    cp = mgr.load("latest")
    assert cp is not None
    assert cp.state["files"] == ["a.py"]
    assert "boot" in mgr.list_checkpoints()


def test_recovery_three_modes(tmp_path):
    from skyn3t.persistence.checkpoint import CheckpointManager
    from skyn3t.persistence.recovery import RecoveryManager, RestoreMode

    mgr = CheckpointManager()
    mgr._dir = tmp_path / "cp"
    bus = EventBus()
    asyncio.run(bus.emit(EventType.SYSTEM, "x", {}))
    mgr.save(
        "latest", event_bus=bus,
        state={"files": ["a.py"], "worktree_dir": "/w", "stage": "code", "plan": ["a"]},
    )
    rec = RecoveryManager(mgr)

    r_files = rec.restore(event_bus=EventBus(), mode=RestoreMode.FILES)
    assert r_files.files_state and not r_files.task_state

    r_task = rec.restore(event_bus=EventBus(), mode=RestoreMode.TASK)
    assert r_task.task_state and not r_task.files_state

    r_both = rec.restore(event_bus=EventBus(), mode="files+task")
    assert r_both.files_state and r_both.task_state
    assert r_both.events_restored >= 1


def test_recovery_missing_checkpoint(tmp_path):
    from skyn3t.persistence.checkpoint import CheckpointManager
    from skyn3t.persistence.recovery import RecoveryManager

    mgr = CheckpointManager()
    mgr._dir = tmp_path / "empty"
    res = RecoveryManager(mgr).restore(event_bus=EventBus())
    assert res.restored is False


# ---- registry: connector catalog ----------------------------------------
def test_catalog_match_brief():
    from skyn3t.registry.catalog import CATALOG

    plan = CATALOG.wiring_plan("build a Stripe store with Postgres and an API")
    assert "stripe" in plan["connectors"]
    assert "postgres" in plan["connectors"]
    assert "fastapi" in plan["connectors"]
    assert "STRIPE_API_KEY" in plan["env_vars"]
    assert any("stripe" in p for p in plan["packages"])


def test_catalog_lookup():
    from skyn3t.registry.catalog import ConnectorCatalog

    cat = ConnectorCatalog()
    assert cat.get("redis").category == "database"
    assert "database" in cat.categories()
    assert cat.match_brief("totally unrelated text") == []


# ---- self_healing: budget guard -----------------------------------------
def test_budget_guard_loop_trip():
    from skyn3t.self_healing.budget import BudgetGuard, GuardTripped

    guard = BudgetGuard(max_loops=2)
    guard._loops = 5
    with pytest.raises(GuardTripped) as ei:
        guard.check()
    assert ei.value.kind == "loop"


def test_budget_guard_stall():
    from skyn3t.self_healing.budget import BudgetGuard, GuardTripped

    guard = BudgetGuard(stall_timeout=0.0)
    # last heartbeat is "now" but timeout 0 => immediately stalled
    import time as _t
    guard._last_heartbeat = _t.time() - 1
    assert guard.is_stalled()
    with pytest.raises(GuardTripped) as ei:
        guard.check()
    assert ei.value.kind in ("stall", "loop", "budget")


def test_budget_guard_event_attach():
    from skyn3t.self_healing.budget import BudgetGuard

    async def run():
        bus = EventBus()
        guard = BudgetGuard(max_loops=1000, stall_timeout=10_000)
        guard.attach(bus)
        await bus.emit(EventType.BUILD_STAGE_STARTED, "studio", {})
        await bus.emit(EventType.AGENT_HEARTBEAT, "agent", {})
        st = guard.status()
        guard.detach()
        return st

    status = asyncio.run(run())
    assert status["loops"] == 1
    assert status["state"] in ("ok", "warn")
