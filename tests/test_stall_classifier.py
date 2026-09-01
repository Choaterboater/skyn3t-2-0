"""Typed stall evidence + classifier for agentic CLI sessions (claw-code Phase 1/1.5 port).

A stalled coding CLI used to surface as a bare wall-clock timeout. Now the
stream consumer captures a StallEvidence bundle at detection time, classifies
it (deterministic, ordered rules), attaches {stall_kind, stall_evidence} to the
error result, and agentic_build auto-heals EXACTLY ONCE for the two
prompt-delivery kinds (misdelivery / acceptance timeout) before escalating.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from skyn3t.adapters import llm as llm_mod
from skyn3t.adapters.llm import LLMClient
from skyn3t.adapters.stall import (
    StallEvidence,
    classify_stall,
    stall_report,
)
from skyn3t.config.settings import Settings

# ---------------------------------------------------------------------------
# Unit: one fixture per kind -> correct classification + evidence string
# ---------------------------------------------------------------------------


def _ev(**kw):
    base = dict(
        provider="codex",
        attempt=0,
        lifecycle_state="streaming",
        bytes_received=0,
        events_received=0,
        content_events=0,
        seconds_since_last_output=0.0,
        prompt_sent_at=100.0,
        process_alive=True,
        exit_code=None,
        idle_timeout=60.0,
        output_tail="",
        stderr_tail="",
    )
    base.update(kw)
    return StallEvidence(**base)


def test_trust_prompt_tail_classifies_trust_required():
    ev = _ev(
        output_tail="Do you trust the files in this folder? [y/N]",
        bytes_received=53,
        events_received=1,
        seconds_since_last_output=75.0,
    )
    assert classify_stall(ev) == "trust_required"
    report = stall_report(ev)
    assert report["stall_kind"] == "trust_required"
    assert "trust" in report["stall_evidence"]


def test_zero_bytes_classifies_prompt_misdelivery():
    ev = _ev(bytes_received=0, events_received=0, seconds_since_last_output=75.0)
    assert classify_stall(ev) == "prompt_misdelivery"
    report = stall_report(ev)
    assert report["stall_kind"] == "prompt_misdelivery"
    assert "zero bytes" in report["stall_evidence"]


def test_misdelivery_requires_the_acceptance_window():
    # Zero bytes but only 5s quiet (window here is the 60s idle guard): too
    # early to call it a misdelivery -> unknown, not a misclassification.
    ev = _ev(bytes_received=0, seconds_since_last_output=5.0)
    assert classify_stall(ev) == "unknown"


def test_accepted_but_no_first_token_classifies_acceptance_timeout():
    ev = _ev(
        bytes_received=48,
        events_received=1,  # a lifecycle event only (e.g. thread.started)
        content_events=0,
        seconds_since_last_output=75.0,
    )
    assert classify_stall(ev) == "prompt_acceptance_timeout"
    report = stall_report(ev)
    assert report["stall_kind"] == "prompt_acceptance_timeout"
    assert "no first content token" in report["stall_evidence"]


def test_dead_transport_mid_stream_classifies_transport_dead():
    ev = _ev(
        process_alive=False,
        exit_code=0,  # clean exit, but the channel closed mid-stream
        bytes_received=4096,
        events_received=9,
        content_events=4,
        seconds_since_last_output=3.0,
    )
    assert classify_stall(ev) == "transport_dead"
    report = stall_report(ev)
    assert report["stall_kind"] == "transport_dead"
    assert "mid-stream" in report["stall_evidence"]


def test_crash_exit_classifies_worker_crashed():
    ev = _ev(
        process_alive=False,
        exit_code=1,
        bytes_received=128,
        events_received=2,
        content_events=1,
        stderr_tail="node: internal/errors: fatal error\n",
    )
    assert classify_stall(ev) == "worker_crashed"
    report = stall_report(ev)
    assert report["stall_kind"] == "worker_crashed"
    assert "exit_code=1" in report["stall_evidence"]


def test_crash_markers_in_stderr_classify_worker_crashed():
    ev = _ev(
        process_alive=False,
        exit_code=0,
        bytes_received=64,
        events_received=1,
        content_events=1,
        stderr_tail="Traceback (most recent call last):\n  ...",
    )
    assert classify_stall(ev) == "worker_crashed"


def test_heartbeat_quiet_classifies_heartbeat_stall():
    ev = _ev(
        bytes_received=8192,
        events_received=12,
        content_events=6,  # real output flowed, then silence
        seconds_since_last_output=610.0,
        idle_timeout=600.0,
    )
    assert classify_stall(ev) == "heartbeat_stall"
    report = stall_report(ev)
    assert report["stall_kind"] == "heartbeat_stall"
    assert "quiet" in report["stall_evidence"]


def test_rule_order_trust_beats_crash_and_crash_beats_transport():
    # A trust prompt in the tail wins even over a dead process: it is the most
    # actionable signal.
    trust = _ev(
        process_alive=False, exit_code=1, bytes_received=100,
        output_tail="Press Enter to continue",
    )
    assert classify_stall(trust) == "trust_required"
    # Nonzero exit + bytes flowed is a crash, not a dead transport.
    crash = _ev(process_alive=False, exit_code=2, bytes_received=100, content_events=1)
    assert classify_stall(crash) == "worker_crashed"


def test_classifier_never_raises_on_garbage():
    assert classify_stall(None) == "unknown"
    assert classify_stall(object()) == "unknown"
    assert classify_stall("not an evidence bundle") == "unknown"
    weird = _ev(
        exit_code="one",
        seconds_since_last_output=float("nan"),
        bytes_received="a lot",
    )
    assert classify_stall(weird) == "unknown"
    report = stall_report(None)
    assert report["stall_kind"] == "unknown"
    assert isinstance(report["stall_evidence"], str)


def test_classifier_failure_degrades_to_unknown_with_raw_evidence(monkeypatch):
    from skyn3t.adapters import stall as stall_mod

    def _boom(_ev):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(stall_mod, "_classify", _boom)
    ev = _ev(bytes_received=0, seconds_since_last_output=75.0)
    assert stall_mod.classify_stall(ev) == "unknown"
    report = stall_mod.stall_report(ev)
    assert report["stall_kind"] == "unknown"
    assert "StallEvidence" in report["stall_evidence"]  # raw evidence attached


# ---------------------------------------------------------------------------
# Stream level: the consumer attaches typed stall evidence to the receipt
# ---------------------------------------------------------------------------


class _EOFStream:
    async def readline(self):
        return b""


class _HangStream:
    async def readline(self):
        await asyncio.sleep(3600)  # never returns within the idle window
        return b""


class _ScriptedStream:
    def __init__(self, lines, *, then_hang=False):
        self._lines = list(lines)
        self._then_hang = then_hang

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._then_hang:
            await asyncio.sleep(3600)
        return b""


class _Proc:
    def __init__(self, stdout, *, returncode=0):
        self.stdout = stdout
        self.stderr = _EOFStream()
        self.returncode = returncode
        self.pid = 424242

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = 1


def _no_kill_terminate(monkeypatch):
    async def _fake_terminate(proc):
        if proc is not None and proc.returncode is None:
            proc.returncode = 1

    monkeypatch.setattr(LLMClient, "_terminate", staticmethod(_fake_terminate))


def _stream_client(**kw):
    return LLMClient(Settings(llm_backend="codex_cli", **kw))


def _line(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


async def test_idle_stall_zero_bytes_attaches_misdelivery(monkeypatch):
    _no_kill_terminate(monkeypatch)
    proc = _Proc(_HangStream(), returncode=None)

    ok = await _stream_client()._consume_agentic_stream(proc, "codex", 0.05)

    assert ok is False
    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["timed_out"] is True
    assert evidence["timeout_kind"] == "idle"
    assert evidence["stall_kind"] == "prompt_misdelivery"
    assert "zero bytes" in evidence["stall_evidence"]


async def test_accepted_but_silent_attaches_acceptance_timeout(monkeypatch):
    _no_kill_terminate(monkeypatch)
    proc = _Proc(
        _ScriptedStream([_line({"type": "thread.started", "thread_id": "t-1"})],
                        then_hang=True),
        returncode=None,
    )

    ok = await _stream_client()._consume_agentic_stream(proc, "codex", 0.05)

    assert ok is False
    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["stall_kind"] == "prompt_acceptance_timeout"


async def test_content_then_quiet_attaches_heartbeat_stall(monkeypatch):
    _no_kill_terminate(monkeypatch)
    proc = _Proc(
        _ScriptedStream([_line({"type": "item.completed", "item": {"text": "w"}})],
                        then_hang=True),
        returncode=None,
    )

    ok = await _stream_client()._consume_agentic_stream(proc, "codex", 0.05)

    assert ok is False
    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["stall_kind"] == "heartbeat_stall"


async def test_trust_prompt_on_stdout_attaches_trust_required(monkeypatch):
    _no_kill_terminate(monkeypatch)
    proc = _Proc(
        _ScriptedStream(
            [b"Do you trust the files in this folder? [y/N]\n"], then_hang=True
        ),
        returncode=None,
    )

    ok = await _stream_client()._consume_agentic_stream(proc, "claude", 0.05)

    assert ok is False
    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["stall_kind"] == "trust_required"


async def test_crash_exit_attaches_worker_crashed(monkeypatch):
    _no_kill_terminate(monkeypatch)
    proc = _Proc(_ScriptedStream([b"garbage\n"]), returncode=1)

    ok = await _stream_client()._consume_agentic_stream(proc, "codex", 0)

    assert ok is False
    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["stall_kind"] == "worker_crashed"


async def test_midstream_eof_attaches_transport_dead(monkeypatch):
    _no_kill_terminate(monkeypatch)
    proc = _Proc(
        _ScriptedStream([_line({"type": "assistant", "message": {"content": "hi"}})]),
        returncode=None,  # channel closed without a terminal result, never reaped
    )

    ok = await _stream_client()._consume_agentic_stream(proc, "claude", 0)

    assert ok is False
    evidence = llm_mod._AGENTIC_STREAM_EVIDENCE.get()
    assert evidence["stall_kind"] == "transport_dead"


# ---------------------------------------------------------------------------
# agentic_build: auto-heal ONCE for the delivery kinds, escalate the rest
# ---------------------------------------------------------------------------


class _Stdin:
    def write(self, _value):
        return None

    async def drain(self):
        return None

    def close(self):
        return None


class _BuildProc(_Proc):
    def __init__(self, stdout, *, returncode=0):
        super().__init__(stdout, returncode=returncode)
        self.stdin = _Stdin()


_OK_LINES = [
    _line({"type": "thread.started", "thread_id": "t-heal"}),
    _line({"type": "item.completed", "item": {"text": "working"}}),
    _line({"type": "result", "is_error": False}),
]


def _build_harness(monkeypatch, procs):
    """Fake the CLI transport under agentic_build; returns (client, spawns)."""
    client = _stream_client(agentic_idle_timeout=1)
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")
    monkeypatch.setattr(client, "_cli_executable", lambda _provider: "codex")
    _no_kill_terminate(monkeypatch)
    spawns = {"n": 0}

    async def _fake_exec(*_argv, **_kwargs):
        proc = procs[min(spawns["n"], len(procs) - 1)]
        spawns["n"] += 1
        return proc

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)
    return client, spawns


async def test_misdelivery_heals_once_and_recovers(monkeypatch, tmp_path):
    # First session: zero bytes ever (prompt never accepted). Resend delivers.
    client, spawns = _build_harness(
        monkeypatch,
        [
            _BuildProc(_HangStream(), returncode=None),
            _BuildProc(_ScriptedStream(_OK_LINES), returncode=0),
        ],
    )

    res = await client.agentic_build("build it", str(tmp_path))

    assert spawns["n"] == 2, "a prompt misdelivery is resent exactly once"
    assert res["ok"] is True
    assert res["cli_execution"]["stall_heal_attempted"] is True
    assert res["cli_execution"]["stall_heal_kind"] == "prompt_misdelivery"
    assert "stall_kind" not in res, "a healed build is not a stalled build"


async def test_misdelivery_heals_only_once_then_escalates(monkeypatch, tmp_path):
    # Both sessions deliver zero bytes -> one heal, then the classified error.
    client, spawns = _build_harness(
        monkeypatch,
        [
            _BuildProc(_HangStream(), returncode=None),
            _BuildProc(_HangStream(), returncode=None),
        ],
    )

    res = await client.agentic_build("build it", str(tmp_path))

    assert spawns["n"] == 2, "one heal and no more"
    assert res["ok"] is False
    assert res["timed_out"] is True
    assert res["stall_kind"] == "prompt_misdelivery"
    assert "zero bytes" in res["stall_evidence"]
    assert res["cli_execution"]["stall_heal_attempted"] is True


async def test_heartbeat_stall_does_not_heal(monkeypatch, tmp_path):
    # Real output flowed, then the session went quiet: escalate immediately.
    client, spawns = _build_harness(
        monkeypatch,
        [_BuildProc(
            _ScriptedStream(
                [_line({"type": "item.completed", "item": {"text": "w"}})],
                then_hang=True,
            ),
            returncode=None,
        )],
    )

    res = await client.agentic_build("build it", str(tmp_path))

    assert spawns["n"] == 1, "heartbeat_stall must NOT auto-heal"
    assert res["ok"] is False
    assert res["stall_kind"] == "heartbeat_stall"


# ---------------------------------------------------------------------------
# code_agent: stall_kind rides run metadata on the degraded path
# ---------------------------------------------------------------------------


async def test_run_metadata_carries_stall_kind_on_degraded_path(tmp_path):
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.agent import TaskRequest
    from skyn3t.core.events import EventBus

    agent = CodeAgent(event_bus=EventBus())
    await agent.start()
    calls = {"n": 0}

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        calls["n"] += 1
        return {
            "ok": False,
            "completed": False,
            "backend": "claude_cli",
            "timed_out": True,
            "error": "agentic CLI ended without a successful result",
            "stall_kind": "transport_dead",
            "stall_evidence": "transport died mid-stream: process exited (code 0)",
        }

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=lambda: property(
        lambda self: getattr(self, "_backend", "stub")
    )):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={"brief": "a react counter app", "slug": "counter",
                     "worktree_dir": str(tmp_path)},
            capabilities_required=("codegen",),
        ))

    assert calls["n"] == 2, "code_agent's own retry layer still applies on top"
    assert result.output.get("degraded") is True
    assert result.output["agentic"]["stall_kind"] == "transport_dead"
    assert "transport died mid-stream" in result.output["agentic"]["stall_evidence"]


async def test_stall_free_build_carries_no_stall_keys(tmp_path):
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.agent import TaskRequest
    from skyn3t.core.events import EventBus

    agent = CodeAgent(event_bus=EventBus())
    await agent.start()

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        import pathlib

        pathlib.Path(workdir, "App.jsx").write_text(
            "// a real multi-line app\n" + ("const item = { id: 1, name: 'x' };\n" * 400)
        )
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=lambda: property(
        lambda self: getattr(self, "_backend", "stub")
    )):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={"brief": "a react counter app", "slug": "counter",
                     "worktree_dir": str(tmp_path)},
            capabilities_required=("codegen",),
        ))

    assert "degraded" not in result.output
    agentic = result.output.get("agentic") or {}
    assert "stall_kind" not in agentic, "stall keys are additive-only"
    assert "stall_evidence" not in agentic


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
