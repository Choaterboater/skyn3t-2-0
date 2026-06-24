"""Streaming agentic-build consumer — accurate success + stall detection.

`_consume_agentic_stream` drives a `claude -p --output-format stream-json`
session: it reads the NDJSON event log to EOF, trusts the terminal `result`
event's `is_error` flag for success, falls back to the process returncode when
no result event appears, and kills a session that goes silent past the idle
guard. No real subprocess — a fake proc feeds canned event lines.
"""

from __future__ import annotations

import asyncio

from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings


class _FakeStream:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""  # EOF


class _HangStream:
    async def readline(self) -> bytes:
        await asyncio.sleep(3600)  # never returns within the idle window
        return b""


class _OverrunStream:
    """readline() raises ValueError once (a line past the buffer limit), like
    asyncio's StreamReader does on an oversized line, then yields EOF."""

    def __init__(self):
        self._raised = False

    async def readline(self) -> bytes:
        if not self._raised:
            self._raised = True
            raise ValueError("Separator is found, but chunk is longer than limit")
        return b""


class _FakeProc:
    def __init__(self, stdout, *, returncode: int = 0, stderr=None):
        self.stdout = stdout
        self.stderr = _FakeStream(stderr or [])
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


def _client() -> LLMClient:
    return LLMClient(Settings(llm_backend="claude_cli"))


async def test_result_event_success():
    proc = _FakeProc(_FakeStream([
        b'{"type":"system","subtype":"init"}\n',
        b'{"type":"assistant"}\n',
        b'{"type":"result","subtype":"success","is_error":false}\n',
    ]))
    assert await _client()._consume_agentic_stream(proc, "claude", 0) is True


async def test_result_event_error_flag_fails():
    # claude -p can exit 0 yet report is_error=true — we must trust the event.
    proc = _FakeProc(_FakeStream([
        b'{"type":"result","subtype":"error_max_turns","is_error":true}\n',
    ]), returncode=0)
    assert await _client()._consume_agentic_stream(proc, "claude", 0) is False


async def test_no_result_event_falls_back_to_returncode():
    ok_proc = _FakeProc(_FakeStream([b'{"type":"assistant"}\n']), returncode=0)
    assert await _client()._consume_agentic_stream(ok_proc, "claude", 0) is True
    bad_proc = _FakeProc(_FakeStream([b'not even json\n']), returncode=1)
    assert await _client()._consume_agentic_stream(bad_proc, "claude", 0) is False


async def test_idle_guard_kills_stalled_session(monkeypatch):
    killed = {"n": 0}

    async def _fake_terminate(proc):  # noqa: ANN001
        killed["n"] += 1

    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))
    proc = _FakeProc(_HangStream())
    # idle_timeout=0.05s: no event ever arrives, so the guard fires fast.
    ok = await _client()._consume_agentic_stream(proc, "claude", 0.05)
    assert ok is False
    assert killed["n"] == 1


async def test_oversized_line_does_not_crash_build():
    # A line past even the 64MB buffer makes readline() raise ValueError; the
    # consumer must NOT propagate it — it stops streaming and falls back to the
    # process returncode (success here).
    proc = _FakeProc(_OverrunStream(), returncode=0)
    assert await _client()._consume_agentic_stream(proc, "claude", 0) is True
