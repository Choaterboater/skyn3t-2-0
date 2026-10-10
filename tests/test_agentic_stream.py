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


# ── Productive-session grace (live-build regression) ─────────────────────────
# A real build logged: ``llm.agentic_stalled idle_s=120 provider=claude
# stall_evidence='had output (502749B/202 events, 100 content) then quiet 120s
# ...; process_alive=True' stall_kind=heartbeat_stall`` — a HEALTHY claude -p
# writing one large file in a single tool call (stream-json is silent until the
# tool call completes) was killed by a short user-configured idle window. A
# live session that already produced substantial content gets a grace floor;
# a session that never produced content keeps the configured window.


class _BurstThenQuiet:
    """Yields ``burst`` lines immediately, then the next ``tail`` lines only
    after ``quiet_s`` of wall-clock silence (cancellation-safe: the ready time
    is absolute, so a wait_for timeout + re-read keeps the same deadline)."""

    def __init__(self, burst: list[bytes], tail: list[bytes], quiet_s: float):
        self._burst = list(burst)
        self._tail = list(tail)
        self._quiet_s = quiet_s
        self._ready_at: float | None = None

    async def readline(self) -> bytes:
        loop = asyncio.get_running_loop()
        if self._burst:
            return self._burst.pop(0)
        if self._ready_at is None:
            self._ready_at = loop.time() + self._quiet_s
        delay = self._ready_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._tail:
            return self._tail.pop(0)
        return b""


_CONTENT = [b'{"type":"assistant","message":{"content":[]}}\n'] * 10
_RESULT = [b'{"type":"result","subtype":"success","is_error":false}\n']


def _grace_client(floor: float, build_timeout: int = 1800) -> LLMClient:
    s = Settings(llm_backend="claude_cli")
    s.agentic_productive_idle_floor = floor
    s.agentic_build_timeout = build_timeout
    return LLMClient(s)


async def test_productive_alive_session_survives_short_idle_window(monkeypatch):
    killed = {"n": 0}

    async def _fake_terminate(proc):  # noqa: ANN001
        killed["n"] += 1

    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))
    # Alive (returncode None), 10 content events, then quiet 0.25s (> the 0.05s
    # configured idle, < the 1.0s grace floor), then the tool call completes.
    stream = _BurstThenQuiet(_CONTENT, _RESULT, quiet_s=0.25)
    proc = _FakeProc(stream, returncode=None)
    ok = await _grace_client(floor=1.0)._consume_agentic_stream(proc, "claude", 0.05)
    assert ok is True
    assert killed["n"] == 0
    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert not evidence.get("timed_out")
    assert evidence.get("idle_grace_extensions", 0) >= 1


async def test_productive_alive_session_still_killed_past_grace_floor(monkeypatch):
    killed = {"n": 0}

    async def _fake_terminate(proc):  # noqa: ANN001
        killed["n"] += 1

    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))
    stream = _BurstThenQuiet(_CONTENT, _RESULT, quiet_s=3600)
    proc = _FakeProc(stream, returncode=None)
    ok = await _grace_client(floor=0.2)._consume_agentic_stream(proc, "claude", 0.05)
    assert ok is False
    assert killed["n"] == 1
    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["timed_out"] is True
    assert evidence["stall_kind"] == "heartbeat_stall"


async def test_no_content_session_keeps_configured_window(monkeypatch):
    async def _fake_terminate(proc):  # noqa: ANN001
        return None

    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))
    # Alive but only lifecycle events: no grace — killed at the configured window
    # even though the quiet gap (0.25s) is inside the grace floor.
    stream = _BurstThenQuiet(
        [b'{"type":"system","subtype":"init"}\n'], _RESULT, quiet_s=0.25
    )
    proc = _FakeProc(stream, returncode=None)
    ok = await _grace_client(floor=1.0)._consume_agentic_stream(proc, "claude", 0.05)
    assert ok is False
    assert llm_mod._AGENTIC_STREAM_EVIDENCE.get()["timed_out"] is True


async def test_dead_process_gets_no_grace(monkeypatch):
    async def _fake_terminate(proc):  # noqa: ANN001
        return None

    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))
    stream = _BurstThenQuiet(_CONTENT, _RESULT, quiet_s=0.25)
    proc = _FakeProc(stream, returncode=1)  # process already exited
    ok = await _grace_client(floor=1.0)._consume_agentic_stream(proc, "claude", 0.05)
    assert ok is False
    assert llm_mod._AGENTIC_STREAM_EVIDENCE.get()["timed_out"] is True


async def test_grace_never_exceeds_build_wall_clock(monkeypatch):
    async def _fake_terminate(proc):  # noqa: ANN001
        return None

    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))
    # Grace floor 600s but the build budget is 1s: the extension is capped by
    # the build wall-clock, so the hung-but-alive session dies near 1s.
    stream = _BurstThenQuiet(_CONTENT, _RESULT, quiet_s=3600)
    proc = _FakeProc(stream, returncode=None)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    ok = await asyncio.wait_for(
        _grace_client(floor=600, build_timeout=1)._consume_agentic_stream(
            proc, "claude", 0.05
        ),
        timeout=10,
    )
    assert ok is False
    assert loop.time() - t0 < 5
