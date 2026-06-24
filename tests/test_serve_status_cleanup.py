"""serve_status must reap + unlink a dead preview server, not just drop it.

Bug: when a live-preview child has died, serve_status popped it from the
registry but never called cleanup_serve — leaking the zombie child (never
waitpid-reaped) and its temp /tmp/skyn3t-serve-*.log file. Every other removal
path (start_serve, stop_serve) cleans up; the prune path didn't.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import skyn3t.web.routes as routes
from skyn3t.studio.app_runner import RunningApp


def test_serve_status_unlinks_dead_servers_logfile(tmp_path, monkeypatch):
    log = tmp_path / "skyn3t-serve-dead.log"
    log.write_text("vite crashed\n")
    app = RunningApp(
        url="http://127.0.0.1:5173", port=5173, pid=4_000_000, kind="node",
        project_dir=str(tmp_path), log_path=str(log), status="running",
    )
    state = SimpleNamespace(running_apps={"dead": app})
    # Force the "process died" branch deterministically (the pid handling itself
    # is exercised elsewhere; here we test the cleanup on prune).
    monkeypatch.setattr(routes, "_pid_alive", lambda pid: False)

    out = asyncio.run(routes.serve_status(state))

    assert "dead" not in state.running_apps      # pruned from the registry
    assert not log.exists()                       # temp logfile unlinked (no leak)
    assert out["running"] == []


def test_serve_status_keeps_live_server(tmp_path, monkeypatch):
    app = RunningApp(
        url="http://127.0.0.1:5173", port=5173, pid=4_000_000, kind="node",
        project_dir=str(tmp_path), log_path=str(tmp_path / "live.log"), status="running",
    )
    state = SimpleNamespace(running_apps={"live": app})
    monkeypatch.setattr(routes, "_pid_alive", lambda pid: True)  # still alive
    out = asyncio.run(routes.serve_status(state))
    assert "live" in state.running_apps           # kept
    assert out["running"] and out["running"][0]["slug"] == "live"
