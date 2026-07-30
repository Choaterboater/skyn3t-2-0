"""A long agentic build must prove it is alive.

Measured on a real build: 24 minutes elapsed between consecutive log lines
while the agent was working normally. With agentic_build_timeout at 1800s and
best_of_n above 1, a single app can spend well over an hour inside codegen, and
nothing in the log distinguishes that from a hang. The stream events are the
only progress evidence a CLI agent produces, and they were consumed silently.

The heartbeat must not change stream semantics: it is emitted from the existing
event loop, never resets the idle-timeout wait (that is driven by real events),
and is disabled by setting the interval to 0.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from skyn3t.adapters.llm import LLMClient


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStderr:
    async def readline(self):
        return b""


class _FakeProc:
    returncode = 0

    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()

    async def wait(self):
        return 0


def _events(n: int) -> list[bytes]:
    out = [json.dumps({"type": "assistant", "i": i}).encode() + b"\n" for i in range(n)]
    out.append(json.dumps({"type": "result", "is_error": False}).encode() + b"\n")
    return out


def _client(**kw):
    settings = SimpleNamespace(
        agentic_heartbeat_seconds=kw.pop("heartbeat", 60),
        **kw,
    )
    client = LLMClient.__new__(LLMClient)
    client.settings = settings
    return client


def _run(client, proc, idle=0):
    return asyncio.run(client._consume_agentic_stream(proc, "codex", idle))


def test_heartbeat_fires_while_a_long_stream_runs(monkeypatch):
    from skyn3t.adapters import llm as llm_mod

    beats: list[dict] = []
    monkeypatch.setattr(
        llm_mod.log, "info",
        lambda evt, **kw: beats.append({"event": evt, **kw}) if evt == "llm.agentic_progress" else None,
    )
    # Advance the clock a minute per event so every event crosses the interval.
    ticks = iter(range(0, 100_000, 60))
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: float(next(ticks)))

    ok = _run(_client(heartbeat=60), _FakeProc(_events(4)))

    assert ok is True
    assert beats, "a long stream must emit at least one progress line"
    assert beats[0]["provider"] == "codex"
    assert beats[0]["events"] >= 1
    assert "elapsed_s" in beats[0]


def test_heartbeat_is_silent_for_a_short_stream(monkeypatch):
    """A fast build must not gain new log noise."""
    from skyn3t.adapters import llm as llm_mod

    beats: list[str] = []
    monkeypatch.setattr(
        llm_mod.log, "info",
        lambda evt, **kw: beats.append(evt) if evt == "llm.agentic_progress" else None,
    )
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: 0.0)  # no time passes

    ok = _run(_client(heartbeat=60), _FakeProc(_events(5)))

    assert ok is True
    assert beats == []


def test_zero_disables_the_heartbeat(monkeypatch):
    from skyn3t.adapters import llm as llm_mod

    beats: list[str] = []
    monkeypatch.setattr(
        llm_mod.log, "info",
        lambda evt, **kw: beats.append(evt) if evt == "llm.agentic_progress" else None,
    )
    ticks = iter(range(0, 100_000, 600))
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: float(next(ticks)))

    _run(_client(heartbeat=0), _FakeProc(_events(4)))

    assert beats == []


def test_the_stream_result_contract_is_unchanged(monkeypatch):
    """The heartbeat must not alter success/failure reporting."""
    from skyn3t.adapters import llm as llm_mod

    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: 0.0)

    failing = [json.dumps({"type": "result", "is_error": True}).encode() + b"\n"]
    assert _run(_client(heartbeat=60), _FakeProc(failing)) is False

    passing = [json.dumps({"type": "result", "is_error": False}).encode() + b"\n"]
    assert _run(_client(heartbeat=60), _FakeProc(passing)) is True
