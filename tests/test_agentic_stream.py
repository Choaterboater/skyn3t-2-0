"""Streaming agentic-build consumer — accurate success + stall detection.

`_consume_agentic_stream` drives a `claude -p --output-format stream-json`
session: it reads the NDJSON event log to EOF, trusts the terminal `result`
event's `is_error` flag for success, falls back to the process returncode when
no result event appears, and kills a session that goes silent past the idle
guard. No real subprocess — a fake proc feeds canned event lines.
"""

from __future__ import annotations

import asyncio

from skyn3t.adapters import llm as llm_mod
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


async def test_codex_stream_retains_bounded_resume_evidence_only():
    proc = _FakeProc(_FakeStream([
        b'{"type":"thread.started","thread_id":"thread-01:abc"}\n',
        b'{"type":"item.completed","item":{"text":"private source body"}}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":999}}\n',
        b'{"type":"thread.started","thread_id":"bad id with whitespace"}\n',
    ]))

    assert await _client()._consume_agentic_stream(proc, "codex", 0) is True

    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["provider"] == "codex"
    assert evidence["session_persistence"] == "ephemeral"
    assert evidence["event_count"] == 4
    assert evidence["parsed_event_count"] == 4
    assert evidence["event_type_counts"] == {
        "thread.started": 2,
        "item.completed": 1,
        "turn.completed": 1,
    }
    assert evidence["thread_id"] == "thread-01:abc"
    assert evidence["terminal_event_type"] == "turn.completed"
    assert evidence["exit_code"] == 0
    assert evidence["exit_status"] == "exited"
    assert "private source body" not in str(evidence)


async def test_stream_evidence_marks_idle_timeout(monkeypatch):
    async def _fake_terminate(proc):  # noqa: ANN001
        return None

    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))
    proc = _FakeProc(_HangStream())

    assert await _client()._consume_agentic_stream(proc, "codex", 0.05) is False

    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["timed_out"] is True
    assert evidence["timeout_kind"] == "idle"
    assert evidence["termination_reason"] == "idle_timeout"
    assert evidence["exit_status"] == "terminated_after_idle_timeout"
