# tests/test_serve_improve_routes.py
"""Service-layer tests for wiring `studio serve` + `studio improve` into the
dashboard (Spec 3 two-pane workspace). Mirrors test_projects_routes.py: the
async service functions are exercised directly with a SimpleNamespace state."""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.web import routes
from skyn3t.web.routes import improve_project, serve_project, serve_status, stop_serve


def _state(tmp_path, *, orchestrator=None):
    projects = tmp_path / "Projects"
    projects.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        settings=SimpleNamespace(projects_dir=projects),
        event_bus=EventBus(),
        orchestrator=orchestrator,
        memory=None,
        skills=None,
        builds={},
    )


def _static_project(state, slug, body="<title>served</title>"):
    d = Path(state.settings.projects_dir) / slug
    d.mkdir(parents=True)
    (d / "index.html").write_text(body)
    return d


def _port_refused(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------

def test_serve_static_then_stop_registers_and_cleans_up(tmp_path):
    state = _state(tmp_path)
    _static_project(state, "alpha")

    started = asyncio.run(serve_project(state, "alpha"))
    try:
        assert started["status"] == "running"
        assert started["url"].startswith("http://127.0.0.1:")
        assert started["slug"] == "alpha"
        # the running handle is held in a registry on state so a later stop finds it
        assert "alpha" in state.running_apps
        log_path = state.running_apps["alpha"].log_path
        assert log_path and Path(log_path).exists()
        port = started["port"]
    finally:
        stopped = asyncio.run(stop_serve(state, "alpha"))

    assert stopped["stopped"] is True
    assert "alpha" not in state.running_apps
    assert _port_refused(port)
    # temp logfile is unlinked on stop (the deferred Slice-2 teardown)
    assert not Path(log_path).exists()
    # a SERVE_STARTED and SERVE_STOPPED event were emitted for the cockpit
    kinds = [e.type for e in state.event_bus.history()]
    assert EventType.SERVE_STARTED in kinds and EventType.SERVE_STOPPED in kinds


def test_serve_no_preview_not_registered(tmp_path):
    state = _state(tmp_path)
    d = Path(state.settings.projects_dir) / "cli"
    d.mkdir(parents=True)
    (d / "main.py").write_text("print('cli')\n")  # no web entrypoint, no index.html
    out = asyncio.run(serve_project(state, "cli"))
    assert out["status"] == "no_preview"
    assert "cli" not in getattr(state, "running_apps", {})


def test_serve_unknown_slug_is_filenotfound(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(FileNotFoundError):
        asyncio.run(serve_project(state, "ghost"))


def test_serve_rejects_traversal(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(serve_project(state, "../etc"))


def test_serve_status_lists_running(tmp_path):
    state = _state(tmp_path)
    _static_project(state, "beta")
    asyncio.run(serve_project(state, "beta"))
    try:
        status = asyncio.run(serve_status(state))
        slugs = {r["slug"] for r in status["running"]}
        assert "beta" in slugs
    finally:
        asyncio.run(stop_serve(state, "beta"))


def test_serve_same_slug_replaces_previous(tmp_path):
    state = _state(tmp_path)
    _static_project(state, "gamma")
    first = asyncio.run(serve_project(state, "gamma"))
    try:
        second = asyncio.run(serve_project(state, "gamma"))
        # the second start supersedes the first: only one handle, old port freed
        assert len([k for k in state.running_apps if k == "gamma"]) == 1
        assert _port_refused(first["port"]) or first["port"] == second["port"]
    finally:
        asyncio.run(stop_serve(state, "gamma"))


def test_stop_unknown_slug_is_noop(tmp_path):
    state = _state(tmp_path)
    out = asyncio.run(stop_serve(state, "nobody"))
    assert out["stopped"] is False


# --------------------------------------------------------------------------
# improve
# --------------------------------------------------------------------------

class _FakeEngine:
    """Stand-in for ImproveEngine: records construction + emits IMPROVE_* events."""
    last_kwargs: dict = {}

    def __init__(self, event_bus, orchestrator, **kwargs):
        self.event_bus = event_bus
        self.orchestrator = orchestrator
        _FakeEngine.last_kwargs = kwargs

    async def improve(self, project, goal, *, correlation_id=None):
        await self.event_bus.emit(EventType.IMPROVE_STARTED, "improve",
                                  {"slug": project, "goal": goal}, correlation_id)
        await self.event_bus.emit(EventType.IMPROVE_COMPLETED, "improve",
                                  {"slug": project, "status": "completed"}, correlation_id)
        return SimpleNamespace(to_dict=lambda: {"slug": project, "status": "completed"})


def test_improve_missing_goal_raises(tmp_path):
    state = _state(tmp_path, orchestrator=SimpleNamespace())
    _static_project(state, "alpha")
    with pytest.raises(ValueError):
        asyncio.run(improve_project(state, "alpha", "   "))


def test_improve_unknown_slug_is_filenotfound(tmp_path):
    state = _state(tmp_path, orchestrator=SimpleNamespace())
    with pytest.raises(FileNotFoundError):
        asyncio.run(improve_project(state, "ghost", "add a footer"))


def test_improve_without_orchestrator_is_unavailable(tmp_path):
    state = _state(tmp_path, orchestrator=None)
    _static_project(state, "alpha")
    out = asyncio.run(improve_project(state, "alpha", "add a footer"))
    assert out["accepted"] is False


def test_improve_dispatches_background_task(tmp_path, monkeypatch):
    monkeypatch.setattr("skyn3t.studio.improve.ImproveEngine", _FakeEngine)
    state = _state(tmp_path, orchestrator=SimpleNamespace())
    _static_project(state, "alpha")

    async def _run():
        out = await improve_project(state, "alpha", "add a footer")
        # drain the dispatched background task(s)
        if routes._IMPROVE_TASKS:
            await asyncio.gather(*list(routes._IMPROVE_TASKS))
        return out

    out = asyncio.run(_run())
    assert out["accepted"] is True
    assert out["slug"] == "alpha" and out["goal"] == "add a footer"
    assert out["correlation_id"]
    # the engine was constructed with the state's collaborators
    assert "settings" in _FakeEngine.last_kwargs
    kinds = [e.type for e in state.event_bus.history()]
    assert EventType.IMPROVE_STARTED in kinds and EventType.IMPROVE_COMPLETED in kinds
