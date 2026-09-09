"""Opt-in, bounded activity records for operators, never a raw agent transcript."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

import structlog

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.security.secrets import mask_secrets

log = structlog.get_logger(__name__)
_CURRENT: contextvars.ContextVar[ActivityLog | None] = contextvars.ContextVar(
    "skyn3t_operator_activity", default=None
)
_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "skyn3t_activity_workdir", default=None
)
_CALLS: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "skyn3t_activity_tools", default=None
)
_TOKEN = re.compile(r"^[a-zA-Z0-9_.:/+-]{1,128}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CREDENTIAL = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+"
    r"|(?:gh[pousr]_|github_pat_|sk-)[a-zA-Z0-9_-]{16,})"
)
_EVENTS = {
    "started", "stage", "model", "activity", "heartbeat", "warning",
    "completed", "failed", "interrupted",
}
_TEXT_FIELDS = {"operation", "project", "stage", "model", "provider", "tool", "path"}
_STAGES = {
    "localize": "Reading project context",
    "generating": "Generating changes",
    "prepare_dependencies": "Preparing dependencies",
    "proof": "Verifying the candidate",
    "verifying": "Verifying the candidate",
    "repairing": "Repairing the candidate",
    "finalizing": "Finalizing generated changes",
    "delivering": "Delivering verified files",
}
_TOOLS = {
    "view": "Reading", "read": "Reading", "read_file": "Reading",
    "edit": "Editing", "edit_file": "Editing", "apply_patch": "Applying a patch",
    "write": "Writing", "write_file": "Writing", "write_files": "Writing files",
    "create": "Creating", "create_file": "Creating", "file_change": "Changed a file",
    "bash": "Running a shell command", "shell": "Running a shell command",
    "run_command": "Running a command", "command_execution": "Running a command",
    "glob": "Finding files", "rg": "Searching files", "grep": "Searching files",
    "list_files": "Listing files", "task": "Running a delegated task",
}


def _text(value: object, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _CONTROL.sub(" ", value)
    cleaned = _CREDENTIAL.sub("<redacted>", cleaned)
    return mask_secrets(cleaned)[:limit]


def _token(value: object) -> str:
    return _text(value, 128) if isinstance(value, str) and _TOKEN.fullmatch(value) else ""


def _relative_path(value: object, root: Path | None) -> str:
    if root is None or not isinstance(value, str) or len(value) > 2048:
        return ""
    if _CONTROL.search(value) or "://" in value or "?" in value or "\x00" in value:
        return ""
    try:
        path = Path(value)
        relative = (path if path.is_absolute() else root / path).resolve().relative_to(root)
    except (OSError, ValueError):
        return ""
    return _path_hint(_text(relative.as_posix(), 512))


def _path_hint(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512 or _CONTROL.search(value):
        return ""
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        return ""
    return _text(value, 512)


class ActivityLog:
    """One exclusive JSONL file; only explicitly selected metadata may enter it."""

    def __init__(self, path: Path, operation: str, project: str = "") -> None:
        self.path = path
        self.run_id = uuid.uuid4().hex
        self.operation = operation
        self.project = _text(Path(project).name if project else "", 128)
        self.started = time.monotonic()
        self.last_activity = self.started
        self.seq = 0
        self.stage = "initializing"
        self.bus: EventBus | None = None
        self._stream: TextIO | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._model: tuple[str, str] | None = None
        self._records = 0
        self._bytes = 0
        self._limited = False
        self.closed = False

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        self._stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        self.emit("started", "Starting SkyN3t", stage=self.stage)

    def emit(self, event: str, message: str, **fields: object) -> None:
        if self._stream is None or self.closed:
            return
        if event not in _EVENTS:
            raise ValueError("Unknown activity event")
        # Bound noisy providers without dropping the authoritative final outcome.
        if (self._records >= 20_000 or self._bytes >= 8 * 1024 * 1024) and event in {"activity", "heartbeat"}:
            if not self._limited:
                self._limited = True
                self.emit("warning", "Detailed activity limit reached; stage and outcome updates continue.")
            return
        self.seq += 1
        self._records += 1
        now = time.monotonic()
        if event != "heartbeat":
            self.last_activity = now
        record: dict[str, object] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "seq": self.seq,
            "timestamp": time.time(),
            "elapsed_s": round(now - self.started, 3),
            "event": event,
            "message": _text(message, 512),
            "operation": self.operation,
        }
        if self.project:
            record["project"] = self.project
        for key in _TEXT_FIELDS:
            value = fields.get(key)
            if key == "path":
                value = _path_hint(value)
            if isinstance(value, str) and value:
                record[key] = _text(value, 512 if key == "path" else 128)
        count = fields.get("changed_files")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            record["changed_files"] = count
        passed = fields.get("proof_passed")
        if isinstance(passed, bool):
            record["proof_passed"] = passed
        try:
            line = json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n"
            self._stream.write(line)
            self._stream.flush()
            self._bytes += len(line)
        except OSError as error:
            log.warning("activity.write_failed", error_type=type(error).__name__)
            self.close()

    def bind(self, bus: EventBus) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self.bus = bus
        self._unsubscribe = bus.subscribe(EventType.ALL, self.on_event)

    async def on_event(self, event: Event) -> None:
        payload = event.payload
        if event.type == EventType.CODEGEN_ACTIVITY:
            kind = payload.get("event")
            if kind == "model":
                model, provider = _token(payload.get("model")), _token(payload.get("provider"))
                if self._model == (model, provider):
                    return
                self._model = model, provider
                self.emit("model", f"Codegen model: {model or 'provider default'}",
                          model=model, provider=provider)
            elif kind == "activity":
                tool = _token(payload.get("tool")) or "tool"
                short = tool.rsplit(".", 1)[-1].lower()
                failed = payload.get("failed") is True
                if failed:
                    action = f"Tool failed: {short}"
                elif payload.get("finished") is True:
                    action = f"Finished {short}"
                else:
                    action = _TOOLS.get(short, f"Using {short}")
                path = _path_hint(payload.get("path"))
                self.emit("warning" if failed else "activity",
                          f"{action}{': ' + path if path else ''}", tool=tool, path=path)
            return
        if event.type in {EventType.BUILD_STARTED, EventType.IMPROVE_STARTED}:
            slug = _text(payload.get("slug"), 128)
            if slug:
                self.project = slug
            snapshot = payload.get("routing_snapshot")
            codegen = snapshot.get("codegen") if isinstance(snapshot, dict) else None
            if isinstance(codegen, dict):
                self.emit("model", "Codegen route selected",
                          model=_token(codegen.get("effective_model")),
                          provider=_token(codegen.get("effective_backend")))
        elif event.type in {EventType.IMPROVE_STAGE, EventType.BUILD_STAGE_STARTED}:
            stage = _text(payload.get("stage") or payload.get("agent_type"), 128)
            if stage:
                self.stage = stage
                self.emit("stage", _STAGES.get(stage, f"Stage: {stage}"), stage=stage)
        elif event.type == EventType.TASK_STARTED:
            agent = _token(payload.get("agent") or event.source)
            self.emit("activity", f"Agent started: {agent or 'worker'}")
        elif event.type == EventType.TASK_RETRYING:
            attempt = payload.get("attempt")
            label = str(attempt) if isinstance(attempt, int) and not isinstance(attempt, bool) else ""
            self.emit("warning", f"Task retry scheduled{': ' + label if label else ''}", stage=self.stage)
        elif event.type == EventType.APPROVAL_REQUESTED:
            self.emit("warning", "Waiting for operator approval", stage=self.stage)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(15)
            quiet = max(0, int(time.monotonic() - self.last_activity))
            self.emit("heartbeat", f"No new reported activity for {quiet}s", stage=self.stage)

    def finish(self, outcome: Mapping[str, Any] | None) -> None:
        completed = outcome is not None and outcome.get("status") == "completed"
        interrupted = outcome is not None and bool(outcome.get("aborted"))
        event = "completed" if completed else "interrupted" if interrupted else "failed"
        message = "SkyN3t run completed" if completed else (
            "Run stopped before completion" if interrupted else "SkyN3t run failed; see the CLI outcome"
        )
        fields: dict[str, object] = {"stage": self.stage}
        if outcome:
            changed = outcome.get("files_changed")
            if isinstance(changed, list):
                fields["changed_files"] = len(changed)
            fields["proof_passed"] = outcome.get("proof_passed")
        self.emit(event, message, **fields)

    def close(self) -> None:
        self.closed = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._stream is not None:
            stream, self._stream = self._stream, None
            try:
                stream.close()
            except OSError as error:
                log.warning("activity.close_failed", error_type=type(error).__name__)


def activity_enabled() -> bool:
    current = _CURRENT.get()
    return current is not None and not current.closed


def bind_activity_bus(bus: EventBus) -> None:
    current = _CURRENT.get()
    if current is not None:
        current.bind(bus)


async def run_with_activity(
    path: Path | None,
    operation: str,
    project: str,
    producer: Callable[[], Awaitable[dict[str, Any] | None]],
) -> dict[str, Any] | None:
    if path is None:
        return await producer()
    activity = ActivityLog(path, operation, project)
    activity.open()
    token = _CURRENT.set(activity)
    heartbeat = asyncio.create_task(activity.heartbeat())
    try:
        outcome = await producer()
        activity.finish(outcome)
        return outcome
    except asyncio.CancelledError:
        activity.emit("interrupted", "Run interrupted; inspect the CLI outcome and project state")
        raise
    except BaseException:
        activity.emit("failed", "Run ended with an error; see the CLI output", stage=activity.stage)
        raise
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        activity.close()
        _CURRENT.reset(token)


@contextmanager
def codegen_activity_scope(workdir: str) -> Iterator[None]:
    root_token = _ROOT.set(Path(workdir).resolve())
    calls_token = _CALLS.set({})
    try:
        yield
    finally:
        _CALLS.reset(calls_token)
        _ROOT.reset(root_token)


async def _publish(payload: dict[str, object]) -> None:
    current = _CURRENT.get()
    if current is not None and current.bus is not None and not current.closed:
        await current.bus.emit(EventType.CODEGEN_ACTIVITY, "codegen", payload)


async def report_codegen_model(provider: str, model: str | None) -> None:
    if activity_enabled():
        await _publish({"event": "model", "provider": _token(provider),
                        "model": _token(model) if model else f"{provider}-default"})


async def report_tool_activity(
    name: object, arguments: object = None, *, root: Path | None = None,
    finished: bool = False, failed: bool = False,
) -> None:
    if not activity_enabled():
        return
    tool = _token(name) or "tool"
    short = tool.rsplit(".", 1)[-1].lower()
    path = ""
    if isinstance(arguments, dict):
        value = arguments.get("path") or arguments.get("file_path") or arguments.get("filePath")
        path = _relative_path(value, root or _ROOT.get())
        if not path and short == "apply_patch":
            patch = arguments.get("input") or arguments.get("patch")
            if isinstance(patch, str):
                match = re.search(r"^\*\*\* (?:Add|Update|Delete) File: ([^\r\n]+)", patch[:16_384], re.M)
                if match:
                    path = _relative_path(match.group(1), root or _ROOT.get())
    elif short == "apply_patch" and isinstance(arguments, str):
        match = re.search(r"^\*\*\* (?:Add|Update|Delete) File: ([^\r\n]+)", arguments[:16_384], re.M)
        if match:
            path = _relative_path(match.group(1), root or _ROOT.get())
    await _publish({"event": "activity", "tool": tool, "path": path,
                    "finished": finished, "failed": failed})


async def report_cli_event(event: Mapping[str, object], provider: str) -> None:
    """Interpret only tool envelopes; prose, reasoning, arguments and results stay private."""
    if not activity_enabled():
        return
    kind = event.get("type")
    data = event.get("data")
    data = data if isinstance(data, dict) else {}
    if provider == "copilot":
        calls = _CALLS.get()
        call_id = data.get("toolCallId")
        if kind == "tool.execution_start":
            tool = _token(data.get("toolName")) or "tool"
            if calls is not None and isinstance(call_id, str) and len(call_id) <= 128:
                if len(calls) >= 256:
                    calls.pop(next(iter(calls)))
                calls[call_id] = tool
            await report_tool_activity(tool, data.get("arguments"))
        elif kind == "tool.execution_complete":
            tool = calls.pop(call_id, "tool") if calls is not None and isinstance(call_id, str) else "tool"
            await report_tool_activity(tool, finished=True, failed=data.get("success") is False)
    elif kind == "assistant":
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if isinstance(blocks, list):
            for block in blocks[:64]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    await report_tool_activity(block.get("name"), block.get("input"))
    elif provider == "codex" and kind in {"item.started", "item.completed"}:
        item = event.get("item")
        if not isinstance(item, dict):
            return
        if item.get("type") == "command_execution":
            exit_code = item.get("exit_code")
            await report_tool_activity(
                "command_execution", finished=kind == "item.completed",
                failed=isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0,
            )
        elif item.get("type") == "file_change" and kind == "item.completed":
            changes = item.get("changes")
            if isinstance(changes, list):
                for change in changes[:64]:
                    if isinstance(change, dict):
                        await report_tool_activity("file_change", {"path": change.get("path")})
