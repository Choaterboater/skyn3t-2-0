from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skyn3t.adapters.llm import LLMClient
from skyn3t.cli import main as cli
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.observability.activity import (
    ActivityLog,
    activity_enabled,
    bind_activity_bus,
    codegen_activity_scope,
    report_cli_event,
    report_codegen_model,
    report_tool_activity,
    run_with_activity,
)
from skyn3t.security.secrets import reset_mask_cache


def records(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("project", ["", "x" * 200])
def test_real_records_match_native_observer_contract(tmp_path, project):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the optional Copilot extension")
    path = tmp_path / "contract.jsonl"
    activity = ActivityLog(path, "build", project)
    activity.open()
    activity.emit("activity", "Reading project", path=".")
    activity.emit("activity", "\U0001f680" * 512,
                  path="/".join(["\U0001f680" * 40] * 12),
                  provider="p" * 128, tool="t" * 128)
    activity.finish({"status": "completed"})
    activity.close()
    result = subprocess.run(
        [node, "--input-type=module", "-e", """
import { RecordDecoder } from './.github/extensions/skyn3t-activity/records.mjs';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const rows = new RecordDecoder().push(Buffer.concat(chunks));
console.log(JSON.stringify(rows.map(row => row.error || row.record.event)));
process.exitCode = rows.some(row => row.error) ? 1 : 0;
"""],
        cwd=Path(__file__).resolve().parents[1],
        input=path.read_bytes(), capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
    assert json.loads(result.stdout) == ["started", "activity", "activity", "completed"]


async def test_activity_is_flushed_before_run_completion(tmp_path):
    path = tmp_path / "activity.jsonl"
    observed, release = asyncio.Event(), asyncio.Event()
    bus = EventBus()

    async def producer():
        bind_activity_bus(bus)
        await bus.emit(EventType.IMPROVE_STARTED, "improve", {
            "slug": "demo", "goal": "private task prompt",
            "routing_snapshot": {"codegen": {"effective_model": "gpt-6-astra", "effective_backend": "copilot_cli"}},
        })
        await bus.emit(EventType.IMPROVE_STAGE, "improve", {"stage": "generating"})
        with codegen_activity_scope(str(tmp_path)):
            await report_codegen_model("copilot", "gpt-6-astra")
            await report_cli_event({
                "type": "tool.execution_start",
                "data": {"toolName": "view", "toolCallId": "one",
                         "arguments": {"path": str(tmp_path / "src/main.py"), "content": "private code"}},
            }, "copilot")
            observed.set()
            await release.wait()
            await report_cli_event({
                "type": "tool.execution_complete",
                "data": {"toolCallId": "one", "result": {"content": "private output"}},
            }, "copilot")
        return {"status": "completed", "proof_passed": True, "files_changed": ["src/main.py"]}

    task = asyncio.create_task(run_with_activity(path, "improve", "demo", producer))
    await observed.wait()
    before = records(path)
    assert not task.done()
    assert before[0]["event"] == "started"
    assert any(row.get("path") == "src/main.py" for row in before)
    assert not any(row["event"] == "completed" for row in before)
    release.set()
    assert (await task)["status"] == "completed"
    after = records(path)
    assert after[-1]["event"] == "completed"
    assert after[-1]["proof_passed"] is True
    assert after[-1]["changed_files"] == 1
    assert [row["seq"] for row in after] == list(range(1, len(after) + 1))
    assert len({row["run_id"] for row in after}) == 1
    assert all(row["elapsed_s"] >= 0 for row in after)
    assert "private" not in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    assert not activity_enabled()


async def test_error_and_cancellation_are_terminal_and_preserve_exceptions(tmp_path):
    async def fail():
        raise ValueError("private credential in an exception")

    failed = tmp_path / "failed.jsonl"
    with pytest.raises(ValueError, match="credential"):
        await run_with_activity(failed, "build", "", fail)
    assert records(failed)[-1]["event"] == "failed"
    assert "credential" not in failed.read_text()

    started = asyncio.Event()

    async def wait():
        started.set()
        await asyncio.Event().wait()

    cancelled = tmp_path / "cancelled.jsonl"
    task = asyncio.create_task(run_with_activity(cancelled, "improve", "demo", wait))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert records(cancelled)[-1]["event"] == "interrupted"
    assert not activity_enabled()


async def test_existing_file_and_symlink_never_overwritten(tmp_path):
    path = tmp_path / "existing"
    path.write_text("preserve")
    called = False

    async def producer():
        nonlocal called
        called = True
        return {"status": "completed"}

    with pytest.raises(FileExistsError):
        await run_with_activity(path, "improve", "demo", producer)
    assert path.read_text() == "preserve"
    assert not called
    link = tmp_path / "link"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(OSError):
        await run_with_activity(link, "improve", "demo", producer)
    assert path.read_text() == "preserve"
    assert not called


async def test_only_safe_tool_metadata_is_published(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_TEST_TOKEN", "private-token-value-123456")
    reset_mask_cache()
    path = tmp_path / "safe.jsonl"

    async def producer():
        bus = EventBus()
        bind_activity_bus(bus)
        await bus.emit(EventType.CODEGEN_ACTIVITY, "untrusted", {
            "event": "activity", "tool": "view", "path": "../outside-secret.py",
            "message": "SECRET bus payload",
        })
        with codegen_activity_scope(str(tmp_path)):
            for kind in ("assistant.message", "assistant.reasoning", "tool.execution_partial_result"):
                await report_cli_event({"type": kind, "data": {"content": "SECRET PROSE"}}, "copilot")
            await report_cli_event({
                "type": "tool.execution_start",
                "data": {"toolName": "bash", "toolCallId": "cmd",
                         "arguments": {"command": "curl -H 'Authorization: Bearer SECRET' https://example.test",
                                       "description": "SECRET description"}},
            }, "copilot")
            await report_tool_activity("view", {"path": "../outside-secret.py"})
            await report_tool_activity("view", {"path": "src/private-token-value-123456.py"})
            await report_tool_activity("view", {"path": "src/name\nSECRET.py"})
            await report_tool_activity("apply_patch", {
                "input": "*** Begin Patch\n*** Update File: src/main.py\n+SECRET SOURCE\n*** End Patch",
            })
            await report_cli_event({
                "type": "tool.execution_complete",
                "data": {"toolCallId": "cmd", "success": False, "result": {"content": "SECRET OUTPUT"}},
            }, "copilot")
        return {"status": "failed", "error": "SECRET FAILURE"}

    try:
        await run_with_activity(path, "improve", "safe", producer)
        text = path.read_text()
        assert "SECRET" not in text
        assert "private-token-value-123456" not in text
        assert "outside-secret" not in text
        assert "src/main.py" in text
        assert any(row["event"] == "warning" and row.get("tool") == "bash" for row in records(path))
        assert all(set(row) <= {
            "schema_version", "run_id", "seq", "timestamp", "elapsed_s", "event",
            "message", "operation", "project", "stage", "model", "provider",
            "tool", "path", "changed_files", "proof_passed",
        } for row in records(path))
    finally:
        reset_mask_cache()


async def test_provider_metadata_and_parallel_runs_do_not_cross(tmp_path):
    async def run(name):
        async def producer():
            bind_activity_bus(EventBus())
            with codegen_activity_scope(str(tmp_path / name)):
                await report_cli_event({
                    "type": "assistant",
                    "message": {"content": [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py", "new_string": "private"}},
                    ]},
                }, "claude")
                await asyncio.sleep(0)
                await report_cli_event({
                    "type": "item.completed",
                    "item": {"type": "file_change", "changes": [{"path": "src/b.py"}], "text": "private"},
                }, "codex")
            return {"status": "completed"}
        await run_with_activity(tmp_path / f"{name}.jsonl", "build", name, producer)

    await asyncio.gather(run("one"), run("two"))
    for name in ("one", "two"):
        rows = records(tmp_path / f"{name}.jsonl")
        assert {row["project"] for row in rows} == {name}
        assert {row.get("path") for row in rows if row.get("path")} == {"src/a.py", "src/b.py"}
        assert "private" not in json.dumps(rows)
    assert records(tmp_path / "one.jsonl")[0]["run_id"] != records(tmp_path / "two.jsonl")[0]["run_id"]


def test_detail_limit_does_not_hide_final_outcome(tmp_path):
    activity = ActivityLog(tmp_path / "bounded.jsonl", "improve", "demo")
    activity.open()
    activity._records = 20_000
    activity.emit("activity", "not retained")
    activity.emit("activity", "also not retained")
    activity.finish({"status": "failed"})
    activity.close()
    rows = records(activity.path)
    assert [row["event"] for row in rows] == ["started", "warning", "failed"]
    assert "not retained" not in activity.path.read_text()


def test_io_failure_is_reported_without_leaking_exception_text(tmp_path, monkeypatch):
    import io

    from skyn3t.observability import activity as module

    warnings = []
    monkeypatch.setattr(module.log, "warning", lambda *args, **kwargs: warnings.append((args, kwargs)))

    class BrokenStream(io.StringIO):
        def write(self, text):
            raise OSError("private path or credential")

    activity = ActivityLog(tmp_path / "broken.jsonl", "improve", "demo")
    activity.open()
    activity._stream.close()
    activity._stream = BrokenStream()
    activity.emit("stage", "Generating", stage="generating")
    assert activity.closed
    assert warnings == [(("activity.write_failed",), {"error_type": "OSError"})]


async def test_fix_stage_names_and_retry_are_not_hidden(tmp_path):
    path = tmp_path / "stages.jsonl"

    async def producer():
        bus = EventBus()
        bind_activity_bus(bus)
        await bus.emit(EventType.BUILD_STAGE_STARTED, "studio", {"stage": "fix#1"})
        await bus.emit(EventType.TASK_RETRYING, "orchestrator", {
            "attempt": 1, "error": "private error body",
        })
        return {"status": "failed"}

    await run_with_activity(path, "build", "demo", producer)
    assert any(row.get("stage") == "fix#1" for row in records(path))
    assert any(row["event"] == "warning" for row in records(path))
    assert "private error body" not in path.read_text()


@pytest.mark.parametrize("operation", ["build", "improve"])
def test_cli_activity_flag_preserves_outcome_and_redacts_goal(tmp_path, monkeypatch, operation):
    async def fake(*args, **kwargs):
        bind_activity_bus(EventBus())
        await report_codegen_model("copilot", "gpt-6-astra")
        return {"status": "completed", "verdict": "go", "slug": "demo", "files": [],
                "proof_passed": True, "files_changed": [], "goal": "private goal"}

    monkeypatch.setattr(cli, f"_run_{operation}", fake)
    path = tmp_path / "cli.jsonl"
    args = ["studio", operation, "demo", "--activity-file", str(path)]
    if operation == "improve":
        args += ["--goal", "private goal"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert records(path)[-1]["event"] == "completed"
    assert "private goal" not in path.read_text()


class FakeProc:
    def __init__(self, lines):
        self.returncode = 0
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(json.dumps(line).encode() + b"\n")
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        return self.returncode

    async def communicate(self):
        return b"Plain response", b""


async def test_copilot_jsonl_is_opt_in_and_keeps_final_prose(tmp_path, monkeypatch):
    client = LLMClient(Settings(llm_backend="copilot_cli", data_dir=tmp_path / "data"))
    monkeypatch.setattr(client, "_cli_available", lambda provider: True)
    monkeypatch.setattr(client, "_cli_executable", lambda provider: "copilot")
    monkeypatch.setattr(client, "_cached_cli_version", lambda provider: "test")
    calls = []

    async def spawn(*argv, **kwargs):
        calls.append(argv)
        return FakeProc([
            {"type": "tool.execution_start", "data": {"toolName": "view", "arguments": {"path": "main.py"}}},
            {"type": "assistant.message", "data": {"content": "Final handoff"}},
        ])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    plain = await client.agentic_build("fixture", str(tmp_path), model="gpt-6-astra")
    assert plain["ok"] and plain["output_text"] == "Plain response"
    assert "--output-format" not in calls[-1]

    async def producer():
        bind_activity_bus(EventBus())
        result = await client.agentic_build("fixture", str(tmp_path), model="gpt-6-astra")
        assert result["ok"]
        assert result["output_text"] == "Final handoff"
        assert result["cli_execution"]["event_count"] == 2
        return {"status": "completed"}

    path = tmp_path / "stream.jsonl"
    await run_with_activity(path, "improve", "demo", producer)
    assert calls[-1][-4:] == ("--output-format", "json", "--stream", "on")
    assert any(row.get("path") == "main.py" for row in records(path))
    assert "Final handoff" not in path.read_text()


async def test_streamed_copilot_keeps_total_timeout_and_execution_evidence(tmp_path, monkeypatch):
    client = LLMClient(Settings(llm_backend="copilot_cli", data_dir=tmp_path / "data"))
    monkeypatch.setattr(client, "_cli_available", lambda provider: True)
    monkeypatch.setattr(client, "_cli_executable", lambda provider: "copilot")
    monkeypatch.setattr(client, "_cached_cli_version", lambda provider: "test")
    proc = FakeProc([])
    proc.returncode = None
    proc.stdout = asyncio.StreamReader()
    proc.stdout.feed_data(b'{"type":"tool.execution_start","data":{"toolName":"view"}}\n')
    spawned, terminated = [], []

    async def spawn(*args, **kwargs):
        spawned.append(args)
        return proc

    async def terminate(process):
        terminated.append(process)
        process.returncode = -9

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client, "_terminate", terminate)

    async def producer():
        bind_activity_bus(EventBus())
        result = await client.agentic_build("fixture", str(tmp_path), timeout=1, model="gpt-6-astra")
        assert not result["ok"]
        assert result["timed_out"]
        assert result["cli_execution"]["timeout_kind"] == "total"
        assert result["cli_execution"]["event_count"] == 1
        return {"status": "failed"}

    path = tmp_path / "timeout.jsonl"
    await run_with_activity(path, "improve", "demo", producer)
    assert len(spawned) == 1
    assert terminated == [proc]
    assert records(path)[-1]["event"] == "failed"


async def test_copilot_nonzero_exit_is_not_overridden_by_a_result_event():
    client = LLMClient(Settings(llm_backend="copilot_cli"))
    proc = FakeProc([{"type": "result", "is_error": False}])
    proc.returncode = 1
    assert not await client._consume_agentic_stream(proc, "copilot", 0)
