"""Offline tests for the web_api package.

These require no network and no heavy deps. The FastAPI-dependent path is
exercised only when FastAPI happens to be installed (skipped otherwise); the
framework-agnostic core (state, auth, handlers, websocket hub, trajectory
hooks) is always tested.
"""

from __future__ import annotations

import pytest

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.web import app as web_app
from skyn3t.web import routes
from skyn3t.web.deps import (
    AppState,
    check_auth,
    extract_bearer,
    is_loopback,
)
from skyn3t.web.websockets import ConnectionHub, _channel_match


def _state(**kw) -> AppState:
    return AppState(event_bus=EventBus(), **kw)


# ---- import-without-fastapi guarantees ------------------------------------
def test_modules_import_without_side_effects():
    # Importing the package must never require fastapi.
    assert hasattr(web_app, "create_app")
    assert hasattr(web_app, "get_app")


def test_create_app_raises_clearly_when_fastapi_absent():
    if web_app.fastapi_available():
        pytest.skip("fastapi installed; cannot test the absent-dep error path")
    with pytest.raises(RuntimeError) as exc:
        web_app.create_app()
    assert "FastAPI" in str(exc.value)


# ---- auth ------------------------------------------------------------------
def test_extract_bearer():
    assert extract_bearer("Bearer abc123") == "abc123"
    assert extract_bearer("bearer  spaced ") == "spaced"
    assert extract_bearer("Basic xyz") is None
    assert extract_bearer(None) is None


def test_is_loopback():
    assert is_loopback("127.0.0.1") is True
    assert is_loopback("::1") is True
    assert is_loopback("localhost") is True
    assert is_loopback("10.0.0.5") is False
    assert is_loopback(None) is False


def test_check_auth_with_token():
    s = _state().settings
    s.auth_token = "secret"
    assert check_auth(s, authorization="Bearer secret", client_host="8.8.8.8") is True
    assert check_auth(s, authorization="Bearer wrong", client_host="127.0.0.1") is False
    assert check_auth(s, authorization=None, client_host="127.0.0.1") is False
    s.auth_token = ""


def test_check_auth_loopback_only_when_no_token():
    s = _state().settings
    s.auth_token = ""
    assert check_auth(s, authorization=None, client_host="127.0.0.1") is True
    assert check_auth(s, authorization=None, client_host="8.8.8.8") is False


# ---- state snapshots -------------------------------------------------------
def test_status_and_budget_snapshots():
    st = _state()
    status = st.status()
    assert status["ok"] is True
    assert "backends" in status and "policy" in status
    budget = st.budget_snapshot()
    assert "daily_usd_cap" in budget
    backends = st.llm_backends()
    assert "tiers" in backends and "budget" in backends


# ---- handlers (framework-agnostic) ----------------------------------------
async def test_submit_and_list_builds():
    st = _state()
    res = await routes.submit_build(st, brief="a todo app", stack="python")
    assert res["build_id"]
    assert st.event_bus.published_count >= 1
    listed = await routes.list_builds(st)
    assert any(b["build_id"] == res["build_id"] for b in listed["builds"])


async def test_submit_build_requires_brief():
    st = _state()
    with pytest.raises(ValueError):
        await routes.submit_build(st, brief="   ")


async def test_proposal_capture_and_decide():
    st = _state()
    # Emitting a proposal event populates the cache via the bus subscription.
    await st.event_bus.emit(
        EventType.PROPOSAL_CREATED,
        source="cortex",
        payload={"proposal_id": "p1", "kind": "build", "summary": "ship it"},
    )
    proposals = await routes.list_proposals(st)
    assert any(p["proposal_id"] == "p1" for p in proposals["proposals"])
    decided = await routes.decide_proposal(st, proposal_id="p1", approved=True, reason="ok")
    assert decided["status"] == "approved"


async def test_metrics_and_prometheus_render():
    st = _state()
    await st.event_bus.emit(EventType.SYSTEM, source="t", payload={})
    data = await routes.metrics_payload(st)
    assert data["events_published"] >= 1
    text = routes.render_prometheus(data)
    assert "skyn3t_events_published_total" in text


async def test_knowledge_search_degraded():
    st = _state()
    res = await routes.knowledge_search(st, q="anything")
    assert res["query"] == "anything"
    assert res.get("degraded") is True


async def test_skills_degraded():
    st = _state()
    res = await routes.list_skills(st)
    assert "skills" in res


# ---- trajectory replay / time-travel hooks (P2) ---------------------------
async def test_trajectory_filtering_and_correlation():
    st = _state()
    await st.event_bus.emit(EventType.SYSTEM, source="a", payload={}, correlation_id="c1")
    await st.event_bus.emit(EventType.BUILD_STARTED, source="b", payload={}, correlation_id="c2")
    all_events = st.trajectory()
    assert len(all_events) >= 2
    only_system = st.trajectory(event_type=EventType.SYSTEM)
    assert all(e["type"] == "system" for e in only_system)
    scoped = st.trajectory(correlation_id="c2")
    assert len(scoped) == 1 and scoped[0]["correlation_id"] == "c2"
    snap = st.trajectory_snapshot()
    assert "history" in snap


# ---- websocket hub (framework-agnostic) -----------------------------------
class _FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, msg: str) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(msg)


def test_channel_match():
    swarm_ev = Event(type=EventType.AGENT_HEARTBEAT, source="x")
    prop_ev = Event(type=EventType.PROPOSAL_CREATED, source="x")
    sys_ev = Event(type=EventType.SYSTEM, source="x")
    assert _channel_match("all", sys_ev) is True
    assert _channel_match("swarm", swarm_ev) is True
    assert _channel_match("swarm", prop_ev) is False
    assert _channel_match("proposals", prop_ev) is True


async def test_hub_fanout_and_dead_socket_drop():
    bus = EventBus()
    hub = ConnectionHub(bus)
    good = _FakeSocket()
    bad = _FakeSocket(fail=True)
    await hub.add("all", good)
    await hub.add("all", bad)
    await bus.emit(EventType.SYSTEM, source="t", payload={"n": 1})
    assert len(good.sent) == 1
    # The failing socket is evicted on the failed send.
    assert hub.count("all") == 1
    hub.close()


async def test_hub_channel_isolation():
    bus = EventBus()
    hub = ConnectionHub(bus)
    swarm = _FakeSocket()
    props = _FakeSocket()
    await hub.add("swarm", swarm)
    await hub.add("proposals", props)
    await bus.emit(EventType.AGENT_HEARTBEAT, source="agent")
    assert len(swarm.sent) == 1
    assert len(props.sent) == 0
    await bus.emit(EventType.PROPOSAL_CREATED, source="cortex", payload={"proposal_id": "x"})
    assert len(props.sent) == 1
    hub.close()
