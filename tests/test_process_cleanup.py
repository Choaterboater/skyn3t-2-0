from __future__ import annotations


class _StubbornProc:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stdin = None

    def terminate(self) -> None:
        self.calls.append("terminate")

    def wait(self, timeout=None) -> None:
        self.calls.append(f"wait:{timeout}")
        if self.calls.count(f"wait:{timeout}") == 1:
            raise TimeoutError("still running")

    def kill(self) -> None:
        self.calls.append("kill")


def test_mcp_shutdown_reaps_after_kill():
    from skyn3t.studio.mcp_check import _shutdown

    proc = _StubbornProc()

    _shutdown(proc)

    assert proc.calls == ["terminate", "wait:2.0", "kill", "wait:2.0"]


def test_rag_shutdown_reaps_after_kill():
    from skyn3t.studio.rag_check import _shutdown

    proc = _StubbornProc()

    _shutdown(proc)

    assert proc.calls == ["terminate", "wait:2.0", "kill", "wait:2.0"]


def test_app_runner_windows_kills_the_preview_tree(monkeypatch):
    from skyn3t.studio import app_runner

    calls = []
    monkeypatch.setattr(app_runner.os, "name", "nt")
    monkeypatch.setattr(
        app_runner.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    app_runner._kill_group(1234)

    assert calls[0][0] == ["taskkill", "/PID", "1234", "/T", "/F"]
    assert calls[0][1]["check"] is False
