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
from skyn3t.web.websockets import ConnectionHub, _channel_match, _ws_authorized


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


# ---- websocket auth (token via subprotocol, never query string) -----------
class _FakeWS:
    def __init__(self, *, headers=None, subprotocols=None, client_host="127.0.0.1"):
        self.headers = headers or {}
        self.scope = {"subprotocols": list(subprotocols or [])}
        self.client = type("_C", (), {"host": client_host})()


def test_ws_authorized_via_subprotocol():
    st = _state()
    st.settings.auth_token = "secret"
    # token carried as the skyn3t-bearer subprotocol -> authorized (even off-loopback)
    assert _ws_authorized(st, _FakeWS(subprotocols=["skyn3t-bearer", "secret"], client_host="8.8.8.8")) is True
    # wrong token rejected
    assert _ws_authorized(st, _FakeWS(subprotocols=["skyn3t-bearer", "nope"], client_host="8.8.8.8")) is False


def test_ws_authorized_via_header_bearer():
    st = _state()
    st.settings.auth_token = "secret"
    assert _ws_authorized(st, _FakeWS(headers={"authorization": "Bearer secret"}, client_host="8.8.8.8")) is True


def test_ws_authorized_ignores_query_token():
    st = _state()
    st.settings.auth_token = "secret"
    # A ?token= query param must NOT authorize anymore (it leaked into logs).
    ws = _FakeWS(client_host="8.8.8.8")
    ws.query_params = {"token": "secret"}
    assert _ws_authorized(st, ws) is False


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


async def test_settings_payload_surfaces_learned_router_flags():
    st = _state()
    st.settings.auto_route = True
    st.settings.model_evolution = True
    st.settings.visual_self_heal = True
    payload = await routes.settings_payload(st)
    assert payload["auto_route"] is True
    assert payload["model_evolution"] is True
    assert payload["visual_self_heal"] is True
    assert payload["visual_self_heal_max_rounds"] >= 1


async def test_visual_self_heal_setting_toggle_does_not_persist_in_tests():
    st = _state()
    res = await routes.set_visual_self_heal(st, True, persist=False)
    assert res["visual_self_heal"] is True
    assert st.settings.visual_self_heal is True


async def test_settings_payload_surfaces_improve_agentic():
    st = _state()
    st.settings.improve_agentic = False
    payload = await routes.settings_payload(st)
    assert payload["improve_agentic"] is False


async def test_improve_agentic_setting_toggle_does_not_persist_in_tests():
    st = _state()
    res = await routes.set_improve_agentic(st, False, persist=False)
    assert res["improve_agentic"] is False
    assert res["improve_agentic_timeout"] >= 1
    assert st.settings.improve_agentic is False
    # And back on — the default posture.
    res = await routes.set_improve_agentic(st, True, persist=False)
    assert res["improve_agentic"] is True
    assert st.settings.improve_agentic is True


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
    assert "patterns" in res


async def test_skills_payload_surfaces_build_patterns():
    class _Skill:
        slug = "react"
        title = "React app"
        body = "Build a reliable React app."
        stack = "react"
        tags = ["react"]
        score = 0.9
        source = "test"

    class _Skills:
        def all(self):
            return [_Skill()]

    class _Patterns:
        def scoreboard(self):
            return [{
                "fp": "abc",
                "stack": "react",
                "uses": 4,
                "win_rate": 0.75,
                "mean_score": 88.5,
                "shape": {"stages": 5},
            }]

    st = _state(skills=_Skills(), patterns=_Patterns())
    res = await routes.list_skills(st)

    assert res["skills"][0]["slug"] == "react"
    assert res["patterns"][0]["fp"] == "abc"
    assert res["patterns"][0]["shape"] == {"stages": 5}


async def test_agent_catalog_preview_and_import(tmp_path):
    from skyn3t.intelligence.skill_library import SkillLibrary

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "frontend-agent.md").write_text(
        "---\n"
        "name: Frontend Builder\n"
        "description: Builds responsive React interfaces.\n"
        "---\n"
        "# Frontend Builder\n\n- Build accessible components.\n",
        encoding="utf-8",
    )
    skills = SkillLibrary(tmp_path / "skills")
    st = _state(skills=skills)

    preview = await routes.agent_catalog_preview(st, str(catalog), limit=20)
    assert preview["summary"]["entries"] == 1
    assert preview["entries"][0]["title"] == "Frontend Builder"
    assert "react" in preview["entries"][0]["stacks"]

    imported = await routes.import_agent_catalog(st, str(catalog), limit=20)
    assert imported["imported"] == 1
    skills_payload = await routes.list_skills(st)
    assert skills_payload["skills"][0]["title"] == "Frontend Builder"


async def test_agent_catalog_import_degrades_without_skill_library(tmp_path):
    st = _state()
    with pytest.raises(ValueError, match="skill library"):
        await routes.import_agent_catalog(st, str(tmp_path))


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
