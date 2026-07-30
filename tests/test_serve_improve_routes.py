# tests/test_serve_improve_routes.py
"""Service-layer tests for wiring `studio serve` + `studio improve` into the
dashboard (Spec 3 two-pane workspace). Mirrors test_projects_routes.py: the
async service functions are exercised directly with a SimpleNamespace state."""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.studio.app_runner import RunningApp
from skyn3t.studio.manifest import BuildManifest
from skyn3t.web import routes
from skyn3t.web.routes import (
    fanout_project,
    improve_project,
    serve_project,
    serve_status,
    stop_serve,
)


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
    BuildManifest(
        slug=slug, brief=slug, stack="static", status="completed", verdict="go"
    ).save(d)
    return d


def _port_refused(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


class _FailRunner:
    """Stub AppRunner whose start() always reports a failed launch carrying a
    real (existing) logfile, to exercise the failed-start cleanup path."""

    def __init__(self, log_path):
        self.log_path = log_path
        self.stopped = []

    async def start(self, pdir, stack="", **kw):
        return RunningApp(url="", port=0, pid=None, kind="static",
                          project_dir=str(pdir), log_path=self.log_path,
                          status="failed", detail={"log_tail": "boom"})

    def stop(self, app):
        self.stopped.append(app)


class _AsyncStopRunner:
    """PreviewSupervisor-shaped runner whose stop must be awaited."""

    def __init__(self):
        self.stopped = []

    async def stop(self, app):
        await asyncio.sleep(0)
        self.stopped.append(app)


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------

@pytest.mark.requires_loopback
@pytest.mark.requires_docker
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
        assert state.running_apps["alpha"].detail["engine"] == "docker"
        assert state.running_apps["alpha"].detail["isolation"]["runtime_egress"] == "blocked"
        port = started["port"]
    finally:
        stopped = asyncio.run(stop_serve(state, "alpha"))

    assert stopped["stopped"] is True
    assert "alpha" not in state.running_apps
    assert _port_refused(port)
    # a SERVE_STARTED and SERVE_STOPPED event were emitted for the cockpit
    kinds = [e.type for e in state.event_bus.history()]
    assert EventType.SERVE_STARTED in kinds and EventType.SERVE_STOPPED in kinds


def test_serve_no_preview_not_registered(tmp_path):
    state = _state(tmp_path)
    d = Path(state.settings.projects_dir) / "cli"
    d.mkdir(parents=True)
    (d / "main.py").write_text("print('cli')\n")  # no web entrypoint, no index.html
    BuildManifest(
        slug="cli", brief="cli", stack="python", status="completed", verdict="go"
    ).save(d)
    out = asyncio.run(serve_project(state, "cli"))
    assert out["status"] == "no_preview"
    assert "cli" not in getattr(state, "running_apps", {})


def test_default_serve_runner_is_docker_supervisor_when_proof_ladder_is_disabled(
    tmp_path,
):
    from skyn3t.studio.preview_supervisor import PreviewSupervisor

    state = _state(tmp_path)
    state.settings.proof_ladder_required = False

    runner = routes._app_runner(state)

    assert isinstance(runner, PreviewSupervisor)


def test_serve_rejects_manifestless_incomplete_project_before_runner_start(tmp_path):
    state = _state(tmp_path)
    project = Path(state.settings.projects_dir) / "partial"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<h1>not delivered</h1>")
    state.app_runner = _FailRunner(None)

    with pytest.raises(routes.ProjectNotDeliveredError):
        asyncio.run(serve_project(state, "partial"))

    assert not state.app_runner.stopped
    assert "partial" not in getattr(state, "running_apps", {})


def test_serve_unknown_slug_is_filenotfound(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(FileNotFoundError):
        asyncio.run(serve_project(state, "ghost"))


def test_serve_rejects_traversal(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(serve_project(state, "../etc"))


@pytest.mark.requires_loopback
@pytest.mark.requires_docker
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


@pytest.mark.requires_loopback
@pytest.mark.requires_docker
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


@pytest.mark.requires_loopback
@pytest.mark.requires_docker
def test_concurrent_serves_same_slug_no_leak(tmp_path):
    # Two serves of one slug racing across the `await start` suspension must not
    # leak a started process: exactly one survivor registered, the loser's port
    # torn down (review finding #1, TOCTOU).
    state = _state(tmp_path)
    _static_project(state, "race")

    async def _run():
        return await asyncio.gather(serve_project(state, "race"),
                                    serve_project(state, "race"))

    r1, r2 = asyncio.run(_run())
    try:
        assert set(state.running_apps.keys()) == {"race"}
        survivor_port = state.running_apps["race"].port
        assert not _port_refused(survivor_port)  # survivor is alive
        for r in (r1, r2):
            if r.get("status") == "running" and r["port"] != survivor_port:
                assert _port_refused(r["port"])  # loser was torn down
    finally:
        asyncio.run(stop_serve(state, "race"))


@pytest.mark.requires_loopback
@pytest.mark.requires_docker
def test_serve_then_concurrent_stop_is_consistent(tmp_path):
    # A stop racing an in-flight start must not be silently lost: the end state
    # is either cleanly stopped or cleanly running — never an orphan port that
    # isn't tracked in the registry (review finding #2).
    state = _state(tmp_path)
    _static_project(state, "tug")

    async def _run():
        return await asyncio.gather(serve_project(state, "tug"),
                                    stop_serve(state, "tug"))

    serve_res, _stop_res = asyncio.run(_run())
    try:
        if "tug" in state.running_apps:
            assert not _port_refused(state.running_apps["tug"].port)
        else:
            # stop won the race: the started server must not be left orphaned
            if serve_res.get("status") == "running":
                assert _port_refused(serve_res["port"])
    finally:
        asyncio.run(stop_serve(state, "tug"))


def test_failed_serve_cleans_up_logfile(tmp_path):
    # A failed start is never registered, so its temp logfile must be unlinked
    # inline or it leaks forever (review finding #7).
    import tempfile
    fd, log_path = tempfile.mkstemp(prefix="skyn3t-serve-test-", suffix=".log")
    import os
    os.close(fd)
    state = _state(tmp_path)
    _static_project(state, "boom")
    state.app_runner = _FailRunner(log_path)

    out = asyncio.run(serve_project(state, "boom"))
    assert out["status"] == "failed"
    assert "boom" not in state.running_apps
    assert not Path(log_path).exists()  # logfile cleaned up


def test_stop_all_serves_tears_down_registered(tmp_path):
    # Shutdown teardown stops every registered preview and clears the registry
    # (locks the contract atexit/on_event depend on — review finding #4).
    from skyn3t.web.deps import AppState

    state = AppState(settings=SimpleNamespace(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path, app_name="x",
        version="0", free_only=True, no_claude=True, autonomous_builds=False,
        approval_gates=True, has_any_llm=False, claude_available=False))
    runner = _FailRunner(None)
    app = RunningApp(url="http://127.0.0.1:1", port=1, pid=None, kind="static",
                     project_dir=str(tmp_path), status="running")
    state.app_runner = runner
    state.running_apps["x"] = app

    state.stop_all_serves()
    assert state.running_apps == {}
    assert runner.stopped == [app]


def test_close_awaits_async_preview_cleanup(tmp_path):
    from skyn3t.web.deps import AppState

    state = AppState(settings=SimpleNamespace(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path, app_name="x",
        version="0", free_only=True, no_claude=True, autonomous_builds=False,
        approval_gates=True, has_any_llm=False, claude_available=False))
    runner = _AsyncStopRunner()
    apps = [
        RunningApp(
            url=f"http://127.0.0.1:{port}",
            port=port,
            pid=None,
            kind="static",
            project_dir=str(tmp_path),
            status="running",
        )
        for port in (1, 2)
    ]
    state.app_runner = runner
    state.running_apps.update({"one": apps[0], "two": apps[1]})

    asyncio.run(state.close())

    assert state.running_apps == {}
    assert runner.stopped == apps


async def test_stop_all_serves_schedules_async_cleanup_on_running_loop(tmp_path):
    from skyn3t.web.deps import AppState

    state = AppState(settings=SimpleNamespace(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path, app_name="x",
        version="0", free_only=True, no_claude=True, autonomous_builds=False,
        approval_gates=True, has_any_llm=False, claude_available=False))
    runner = _AsyncStopRunner()
    app = RunningApp(
        url="http://127.0.0.1:1",
        port=1,
        pid=None,
        kind="static",
        project_dir=str(tmp_path),
        status="running",
    )
    state.app_runner = runner
    state.running_apps["x"] = app

    state.stop_all_serves()
    await state.stop_all_serves_async()

    assert state.running_apps == {}
    assert runner.stopped == [app]


def _appstate(tmp_path):
    from skyn3t.web.deps import AppState
    return AppState(settings=SimpleNamespace(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path, app_name="x",
        version="0", free_only=True, no_claude=True, autonomous_builds=False,
        approval_gates=True, has_any_llm=False, claude_available=False))


class _FanStudio:
    """Stub StudioRunner.start that returns a canned go outcome per candidate."""
    def __init__(self):
        self.started = []
        self.extras = []

    async def start(self, brief, slug=None, extra=None):
        self.started.append(slug)
        self.extras.append(dict(extra or {}))
        stack = (extra or {}).get("stack", "")
        return SimpleNamespace(verdict="go", score=80.0, status="completed",
                               stack=stack, slug=slug,
                               manifest={"extra": {"proof": {"passed": True}}})


def _fan_state(tmp_path, studio=None):
    return SimpleNamespace(
        studio=studio, event_bus=EventBus(),
        settings=SimpleNamespace(projects_dir=tmp_path / "Projects"))


def test_fanout_dispatches_distinct_slugs_and_emits(tmp_path):
    studio = _FanStudio()
    state = _fan_state(tmp_path, studio)

    async def _run():
        out = await fanout_project(state, "a todo app", ["react", "static"])
        if routes._FANOUT_TASKS:
            await asyncio.gather(*list(routes._FANOUT_TASKS))
        return out

    out = asyncio.run(_run())
    assert out["accepted"] is True and out["correlation_id"]
    # each candidate built to a DISTINCT slug (no clobbering)
    assert set(studio.started) == {"a-todo-app-react", "a-todo-app-static"}
    kinds = [e.type for e in state.event_bus.history()]
    assert EventType.FANOUT_STARTED in kinds and EventType.FANOUT_COMPLETED in kinds


def test_fanout_rejects_missing_explicit_backend_before_dispatch(tmp_path):
    studio = _FanStudio()
    state = _fan_state(tmp_path, studio)
    state.settings.llm_backend = "openrouter"
    state.settings.openrouter_api_key = ""

    with pytest.raises(ValueError, match="OpenRouter was explicitly selected"):
        asyncio.run(fanout_project(state, "a todo app", ["react", "static"]))

    assert studio.started == []


def test_fanout_rejects_unavailable_codegen_cli_before_dispatch(tmp_path):
    studio = _FanStudio()
    state = _fan_state(tmp_path, studio)
    # This endpoint test is about the explicit codegen-provider lock, not the
    # automatic executor policy.
    state.settings.llm_backend = "stub"
    state.settings.codegen_cli_provider = "codex"
    state.llm_client = SimpleNamespace(_cli_available=lambda _provider: False)

    with pytest.raises(ValueError, match="codegen_cli_provider='codex'.*unavailable"):
        asyncio.run(fanout_project(state, "a todo app", ["react", "static"]))

    assert studio.started == []


def test_fanout_preserves_full_build_contract_for_every_candidate(tmp_path):
    studio = _FanStudio()
    state = _fan_state(tmp_path, studio)
    state.settings.data_dir = tmp_path / "data"
    image = "data:image/png;base64,iVBORw0KGgo="

    async def _run():
        out = await fanout_project(
            state,
            "a complete portal",
            ["react", "fastapi"],
            build_profile="fast",
            model_override="vendor/model",
            full_app=True,
            reference_image=image,
        )
        if routes._FANOUT_TASKS:
            await asyncio.gather(*list(routes._FANOUT_TASKS))
        return out

    out = asyncio.run(_run())

    assert out["build_profile"] == "fast"
    assert out["model_override"] == "vendor/model"
    assert out["full_app"] is True
    assert out["reference_images"] == 1
    assert {extra["stack"] for extra in studio.extras} == {"react", "fastapi"}
    for extra in studio.extras:
        assert extra["build_profile"] == "fast"
        assert extra["full_app_contract"] is True
        assert extra["asset_gen"] is False
        assert extra["parallel_code_slices"] is True
        assert extra["parallel_code_slices_min_files"] == 4
        assert extra["model_override"] == "vendor/model"
        assert Path(extra["reference_image"]).is_file()


@pytest.mark.parametrize("profile", ["cheap_learned", "balanced"])
def test_fanout_profile_policy_never_weakens_full_app_contract(tmp_path, profile):
    studio = _FanStudio()
    state = _fan_state(tmp_path, studio)

    async def _run():
        await fanout_project(
            state,
            "a complete service website",
            ["react", "static"],
            build_profile=profile,
            full_app=True,
        )
        if routes._FANOUT_TASKS:
            await asyncio.gather(*list(routes._FANOUT_TASKS))

    asyncio.run(_run())

    for extra in studio.extras:
        assert extra["full_app_contract"] is True
        assert extra["asset_gen"] is False
        assert extra["visual_self_heal"] is True
        if profile == "cheap_learned":
            assert extra["best_of_n"] == 1
            assert extra["best_of_n_across_models"] is False
            assert extra["parallel_code_slices"] is True
            assert extra["parallel_code_slices_min_files"] == 4


def test_fanout_requires_two_stacks(tmp_path):
    state = _fan_state(tmp_path, _FanStudio())
    with pytest.raises(ValueError):
        asyncio.run(fanout_project(state, "x", ["react"]))


def test_fanout_without_studio_is_unavailable(tmp_path):
    state = _fan_state(tmp_path, studio=None)
    out = asyncio.run(fanout_project(state, "x", ["react", "static"]))
    assert out["accepted"] is False


def test_create_app_registers_atexit_teardown(tmp_path, monkeypatch):
    # create_app must register a process-exit backstop so previews are stopped
    # even when the graceful ASGI shutdown hook doesn't fire (review finding #4).
    pytest.importorskip("fastapi")
    import atexit as _atexit

    from skyn3t.web.app import create_app

    registered = []
    monkeypatch.setattr(_atexit, "register",
                        lambda fn, *a, **k: (registered.append(fn), fn)[1])

    state = _appstate(tmp_path)
    runner = _AsyncStopRunner()
    app = RunningApp(url="http://127.0.0.1:1", port=1, pid=None, kind="static",
                     project_dir=str(tmp_path), status="running")
    state.app_runner = runner
    state.running_apps["x"] = app

    create_app(state=state)
    assert registered, "create_app must register an atexit teardown"
    for fn in registered:
        fn()
    assert state.running_apps == {} and runner.stopped == [app]


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
    state.settings.llm_backend = "stub"
    state.settings.codegen_cli_provider = "codex"
    state.settings.codegen_cli_model = "submission-model"
    state.llm_client = SimpleNamespace(
        _cli_available=lambda _provider: True,
        build_routing_snapshot=lambda _model="": {
            "requested_backend": "stub",
            "effective_backend": "stub",
            "codegen": {
                "source": "codegen_cli_pin",
                "requested_backend": "codex_cli",
                "effective_backend": "codex_cli",
                "requested_model": "submission-model",
                "effective_model": "submission-model",
            },
        },
    )

    async def _run():
        out = await improve_project(state, "alpha", "add a footer")
        # The task has not needed to start for the route to be immutable: the
        # service captured it synchronously with the GUI request.
        state.settings.codegen_cli_provider = "claude"
        state.settings.codegen_cli_model = "live-model"
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
    assert _FakeEngine.last_kwargs["llm_client"] is state.llm_client
    frozen = _FakeEngine.last_kwargs["routing_snapshot"]["codegen"]
    assert frozen["requested_backend"] == "codex_cli"
    assert frozen["requested_model"] == "submission-model"
    kinds = [e.type for e in state.event_bus.history()]
    assert EventType.IMPROVE_STARTED in kinds and EventType.IMPROVE_COMPLETED in kinds
