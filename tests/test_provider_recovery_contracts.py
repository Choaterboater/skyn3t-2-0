"""Provider contract regressions through the real HTTP and agentic call paths."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import skyn3t.adapters.llm as llm
from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import Tier

_MISSING = object()
_WRITE = {
    "id": "write-1",
    "type": "function",
    "function": {
        "name": "write_file",
        "arguments": '{"path":"app.py","content":"VALUE = 1\\n"}',
    },
}


def _response(message, finish_reason=_MISSING):
    choice = {"message": message}
    if finish_reason is not _MISSING:
        choice["finish_reason"] = finish_reason
    return {
        "choices": [choice],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "cost": 0.001},
    }


def _scripted_http(monkeypatch, responses, *, bodies=None):
    requests = []
    client_type = httpx.AsyncClient

    def handle(request):
        requests.append(request.url.path)
        if bodies is not None:
            bodies.append(json.loads(request.content))
        assert len(requests) <= 12, "provider recovery exceeded the probe safety bound"
        return httpx.Response(
            200, json=responses[min(len(requests) - 1, len(responses) - 1)],
        )

    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )
    return requests


def _client(**overrides):
    options = {
        "llm_backend": "openrouter",
        "openrouter_api_key": "test-only",
        "llm_fallback_enabled": False,
    }
    options.update(overrides)
    return LLMClient(Settings(**options))


@pytest.mark.parametrize("finish_reason", [_MISSING, "stop"])
async def test_valid_legacy_and_explicit_text_completion_remain_supported(
    tmp_path, monkeypatch, finish_reason,
):
    _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE]}, "tool_calls"),
        _response({"content": "Implemented the requested file."}, finish_reason),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is True
    assert result["completed"] is True
    assert (tmp_path / "app.py").read_text() == "VALUE = 1\n"


async def test_object_arguments_remain_valid_through_history_and_loop_guards(
    tmp_path, monkeypatch,
):
    read = {
        "id": "read-1", "type": "function",
        "function": {"name": "read_file", "arguments": {"path": "app.py"}},
    }
    bodies = []
    _scripted_http(monkeypatch, [
        _response({"tool_calls": [_WRITE]}, "tool_calls"),
        _response({"tool_calls": [read]}, "tool_calls"),
        _response({"tool_calls": [{**read, "id": "read-2"}]}, "tool_calls"),
        _response({"tool_calls": [{
            "id": "finish-1", "type": "function",
            "function": {"name": "finish", "arguments": {}},
        }]}, "tool_calls"),
    ], bodies=bodies)
    result = await _client()._openrouter_agentic(
        "write and inspect a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is True
    assert result["completed"] is True
    assert len(bodies) == 4
    for request in bodies:
        for message in request["messages"]:
            for call in message.get("tool_calls", []):
                assert isinstance(call["function"]["arguments"], str)


@pytest.mark.parametrize("name,args", [
    ("write_file", {"path": "empty.txt", "content": ""}),
    ("write_files", {"files": [{"path": "empty.txt", "content": ""}]}),
])
async def test_explicit_empty_write_content_is_still_valid(tmp_path, monkeypatch, name, args):
    _scripted_http(monkeypatch, [
        _response({"tool_calls": [{
            "id": "write-1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}, "tool_calls"),
        _response({"tool_calls": [{
            "id": "finish-1", "type": "function",
            "function": {"name": "finish", "arguments": "{}"},
        }]}),
    ])
    result = await _client()._openrouter_agentic(
        "create an empty file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is True
    assert result["completed"] is True
    assert (tmp_path / "empty.txt").read_bytes() == b""


@pytest.mark.parametrize("invalid", [
    {"id": "other", "function": None},
    {"id": "other", "function": ["finish"]},
    {"id": True, "function": {"name": "finish", "arguments": "{}"}},
    {"id": 1, "function": {"name": "finish", "arguments": "{}"}},
    {"id": " ", "function": {"name": "finish", "arguments": "{}"}},
    {"id": "write-1", "function": {"name": "finish", "arguments": "{}"}},
])
async def test_invalid_function_or_identity_rejects_all_sibling_tools(
    tmp_path, monkeypatch, invalid,
):
    _scripted_http(monkeypatch, [
        _response({"tool_calls": [_WRITE, invalid]}, "tool_calls"),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is False
    assert result["completed"] is False
    assert not (tmp_path / "app.py").exists()


async def test_malformed_recovery_does_not_resend_invalid_tool_messages(
    tmp_path, monkeypatch,
):
    bodies = []
    _scripted_http(monkeypatch, [
        _response({"tool_calls": [_WRITE, {"id": False, "function": None}]}, "tool_calls"),
        _response({"tool_calls": [_WRITE]}, "tool_calls"),
        _response({"content": "done"}, "stop"),
    ], bodies=bodies)
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is True
    assert len(bodies) == 3
    for request in bodies:
        for message in request["messages"]:
            for call in message.get("tool_calls", []):
                assert isinstance(call["id"], str) and call["id"]
                assert isinstance(call["function"], dict)
            if message["role"] == "tool":
                assert message["tool_call_id"]


async def test_repeated_length_truncated_text_never_becomes_completion(tmp_path, monkeypatch):
    requests = _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE]}, "tool_calls"),
        _response({"content": "The remaining implementation is"}, "length"),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["completed"] is False
    assert result["ok"] is False
    assert len(requests) <= 4
    assert "truncat" in str(result.get("error", "")).lower()


@pytest.mark.parametrize("arguments", ["{", "null", "[]", "false", '"done"'])
async def test_malformed_finish_rejects_the_whole_tool_batch_before_writing(
    tmp_path, monkeypatch, arguments,
):
    invalid_finish = {
        "id": "finish-1",
        "type": "function",
        "function": {"name": "finish", "arguments": arguments},
    }
    _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE, invalid_finish]}, "tool_calls"),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert not (tmp_path / "app.py").exists(), "an invalid batch partially wrote the workspace"
    assert result["completed"] is False
    assert result["ok"] is False


async def test_explicit_refusal_is_terminal_without_recovery(tmp_path, monkeypatch):
    requests = _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE]}, "tool_calls"),
        _response({
            "content": "I cannot complete the requested operation.",
            "refusal": "The requested operation is refused.",
        }, "stop"),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["completed"] is False
    assert result["ok"] is False
    assert len(requests) == 2
    assert "refus" in str(result.get("error", "")).lower()


def _rate_limit(seconds):
    request = httpx.Request("POST", llm.OPENROUTER_URL)
    response = httpx.Response(429, headers={"Retry-After": seconds}, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


async def test_retry_after_minimum_is_observed_before_another_attempt(monkeypatch):
    clock = [0.0]
    attempts = []

    async def sleep(delay):
        clock[0] += delay

    async def attempt(model):
        attempts.append(clock[0])
        if len(attempts) == 1:
            raise _rate_limit("10")
        return "ok"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = await _client(
        llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=30,
    )._resilient_call("test/model", Tier.CHEAP, attempt)
    assert result == "ok"
    assert len(attempts) == 2
    assert 10 <= attempts[1] - attempts[0] <= 30


async def test_oversized_retry_after_stops_without_an_early_attempt(monkeypatch):
    attempts = []

    async def sleep(delay):
        pass

    async def attempt(model):
        attempts.append(model)
        if len(attempts) == 1:
            raise _rate_limit("3600")
        return "unexpected early retry"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(httpx.HTTPStatusError):
        await _client(
            llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=30,
        )._resilient_call("test/model", Tier.CHEAP, attempt)
    assert len(attempts) == 1


@pytest.mark.parametrize("reason", ["provider_error", "stop", 42, ["stop"]])
async def test_invalid_tool_finish_state_has_no_dispatch(tmp_path, monkeypatch, reason):
    finish = {
        "id": "finish-1", "type": "function",
        "function": {"name": "finish", "arguments": "{}"},
    }
    _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE, finish]}, reason),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is False
    assert result["completed"] is False
    assert not (tmp_path / "app.py").exists()


@pytest.mark.parametrize("calls", [{}, {"function": {}}, "invalid", False])
async def test_non_list_tool_calls_cannot_become_a_text_finish(tmp_path, monkeypatch, calls):
    _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE]}, "tool_calls"),
        _response({"content": "done", "tool_calls": calls}, "stop"),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is False
    assert result["completed"] is False


@pytest.mark.parametrize("name,args", [
    ("write_file", {"path": "victim.py"}),
    ("write_files", {"files": [{"path": "victim.py"}]}),
])
async def test_missing_content_does_not_erase_files_or_dispatch_siblings(
    tmp_path, monkeypatch, name, args,
):
    victim = tmp_path / "victim.py"
    victim.write_text("KEEP = True\n")
    incomplete = {
        "id": "bad-write", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
    _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE, incomplete]}, "tool_calls"),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is False
    assert victim.read_text() == "KEEP = True\n"
    assert not (tmp_path / "app.py").exists()


def _fake_retry_clock(monkeypatch, *, oversleep=0.0):
    now, sleeps = [100.0], []
    monkeypatch.setattr(
        llm, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time),
    )

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay + oversleep

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return now, sleeps


async def test_latest_retry_after_is_respected_before_fallback(monkeypatch):
    now, sleeps = _fake_retry_clock(monkeypatch)
    client = _client(
        llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=30,
    )
    monkeypatch.setattr(client, "_healthy_fallback_models", lambda *args: ["test/fallback"])
    attempts = []

    async def attempt(model):
        attempts.append((model, now[0]))
        if model == "test/primary":
            raise _rate_limit("10")
        return "ok"

    assert await client._resilient_call("test/primary", Tier.CHEAP, attempt) == "ok"
    assert attempts == [
        ("test/primary", 100.0), ("test/primary", 110.0), ("test/fallback", 120.0),
    ]
    assert sleeps == [10.0, 10.0]


async def test_late_backoff_wakeup_never_starts_an_expired_attempt(monkeypatch):
    now, _ = _fake_retry_clock(monkeypatch, oversleep=5)
    client = _client(
        llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=30,
        llm_call_deadline_seconds=5,
    )
    attempts = []
    error = _rate_limit("1")

    async def attempt(model):
        attempts.append(now[0])
        if len(attempts) == 1:
            raise error
        return "late request"

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await client._resilient_call("test/model", Tier.CHEAP, attempt)
    assert raised.value is error
    assert attempts == [100.0]


@pytest.mark.parametrize("header,cap", [
    ("9" * 400, 30), ("1e" + "9" * 400, 30), ("5", 0),
])
async def test_unhonorable_numeric_wait_never_becomes_local_retry(monkeypatch, header, cap):
    _, sleeps = _fake_retry_clock(monkeypatch)
    client = _client(
        llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=cap,
    )
    attempts = []
    error = _rate_limit(header)

    async def attempt(model):
        attempts.append(model)
        if len(attempts) == 1:
            raise error
        return "early request"

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await client._resilient_call("test/model", Tier.CHEAP, attempt)
    assert raised.value is error
    assert len(attempts) == 1
    assert sleeps == []


async def test_oversized_fallback_wait_preserves_the_primary_error(monkeypatch):
    _fake_retry_clock(monkeypatch)
    client = _client(llm_max_retries=0, llm_retry_max_delay=30)
    monkeypatch.setattr(client, "_healthy_fallback_models", lambda *args: ["test/fallback"])
    request = httpx.Request("POST", llm.OPENROUTER_URL)
    primary_error = httpx.HTTPStatusError(
        "missing model", request=request, response=httpx.Response(404, request=request),
    )
    fallback_error = _rate_limit("3600")

    async def attempt(model):
        raise primary_error if model == "test/primary" else fallback_error

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await client._resilient_call("test/primary", Tier.CHEAP, attempt)
    assert raised.value is primary_error
    assert raised.value.__cause__ is fallback_error


@contextmanager
def _native_activity(path):
    from skyn3t.core.events import EventBus
    from skyn3t.observability.activity import _CURRENT, ActivityLog

    activity = ActivityLog(path, "improve", "provider-contract")
    activity.open()
    activity.bind(EventBus())
    token = _CURRENT.set(activity)
    try:
        yield activity
    finally:
        _CURRENT.reset(token)
        activity.close()


@pytest.mark.parametrize("missing", [True, False])
async def test_tool_activity_distinguishes_errors_from_error_prefixed_file_data(
    tmp_path, monkeypatch, missing,
):
    if not missing:
        (tmp_path / "readme.txt").write_text("ERROR: this is ordinary file content\n")
    read = {
        "id": "read-1", "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"readme.txt"}'},
    }
    finish = {
        "id": "finish-1", "type": "function",
        "function": {"name": "finish", "arguments": "{}"},
    }
    _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE]}, "tool_calls"),
        _response({"content": "", "tool_calls": [read]}, "tool_calls"),
        _response({"content": "", "tool_calls": [finish]}, "tool_calls"),
    ])
    activity_path = tmp_path / "tools.jsonl"
    with _native_activity(activity_path):
        await _client()._openrouter_agentic(
            "write a file", str(tmp_path), "test/model",
            enforce_antistub=False, verify_on_stop=False,
        )
    rows = [
        json.loads(line) for line in activity_path.read_text().splitlines()
        if json.loads(line).get("tool") == "read_file"
    ]
    assert rows[-1]["event"] == ("warning" if missing else "activity")
    assert ("Tool failed" if missing else "Finished") in rows[-1]["message"]


async def test_retry_activity_uses_actual_overridden_provider(tmp_path, monkeypatch):
    _, sleeps = _fake_retry_clock(monkeypatch)
    fake_sleep = asyncio.sleep
    client_type = httpx.AsyncClient
    count = 0
    private = "PRIVATE_PROVIDER_BODY_6d8b6e"
    activity_path = tmp_path / "override.jsonl"

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "5", "X-Provider-Diagnostic": private},
                json={"error": {"message": private}},
            )
        return httpx.Response(200, json=_response({"content": "ok"}, "stop"))

    async def observe_before_sleep(delay):
        content = activity_path.read_text()
        assert "Retry decision" in content
        assert "outcome waiting" in content
        assert private not in content
        assert "test-only" not in content
        await fake_sleep(delay)

    monkeypatch.setattr(
        llm.httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(asyncio, "sleep", observe_before_sleep)
    with _native_activity(activity_path):
        await _client(
            llm_backend="stub", llm_max_retries=1, llm_retry_base_delay=0,
            llm_retry_max_delay=30,
        ).complete(
            private, provider_override="openrouter", model_override="test/model:free",
        )
    rows = [json.loads(line) for line in activity_path.read_text().splitlines()]
    retries = [row for row in rows if "Retry decision" in row["message"]]
    assert retries
    assert all(row["provider"] == "openrouter" for row in retries)
    assert count == 2
    assert sleeps == [5.0]

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the optional native observer contract")
    decoded = subprocess.run(
        [node, "--input-type=module", "-e", """
import { RecordDecoder } from './.github/extensions/skyn3t-activity/records.mjs';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const rows = new RecordDecoder().push(Buffer.concat(chunks));
console.log(JSON.stringify(rows.map(row => row.error || row.record.event)));
process.exitCode = rows.some(row => row.error) ? 1 : 0;
"""],
        cwd=Path(__file__).resolve().parents[1],
        input=activity_path.read_bytes(), capture_output=True, check=False, timeout=10,
    )
    assert decoded.returncode == 0, decoded.stdout.decode() + decoded.stderr.decode()
    assert len(json.loads(decoded.stdout)) == len(rows)


async def test_slow_activity_cannot_spend_the_deadline_and_start_another_request(
    tmp_path, monkeypatch,
):
    from skyn3t.core.events import EventType

    now, sleeps = _fake_retry_clock(monkeypatch)
    error = _rate_limit("10")
    attempts = []

    async def slow_listener(event):
        if event.payload.get("event") == "retry" and event.payload.get("outcome") == "waiting":
            now[0] += 10

    async def attempt(model):
        attempts.append(now[0])
        raise error

    with _native_activity(tmp_path / "slow-activity.jsonl") as activity:
        activity.bus.subscribe(EventType.CODEGEN_ACTIVITY, slow_listener)
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await _client(
                llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=30,
                llm_call_deadline_seconds=15,
            )._resilient_call("test/model", Tier.CHEAP, attempt)
    assert raised.value is error
    assert attempts == [100.0]
    assert sleeps == []


async def test_bounded_successful_truncation_recovery(tmp_path, monkeypatch):
    requests = _scripted_http(monkeypatch, [
        _response({"content": "", "tool_calls": [_WRITE]}, "tool_calls"),
        _response({"content": "The implementation is starting..."}, "length"),
        _response({"content": "Implementation complete."}, "stop"),
    ])
    result = await _client()._openrouter_agentic(
        "write a file", str(tmp_path), "test/model",
        enforce_antistub=False, verify_on_stop=False,
    )
    assert result["ok"] is True
    assert result["completed"] is True
    assert len(requests) == 3


@pytest.mark.parametrize("outcome,count", [
    ("refused", 1), ("truncated", 3), ("malformed_response", 1), ("malformed_tools", 3),
])
async def test_rejected_responses_retain_exact_billing_without_body_in_activity(
    tmp_path, monkeypatch, outcome, count,
):
    client = _client()
    private = "PRIVATE_PROVIDER_BODY_6d8b6e"
    responses = {
        "refused": _response({"content": private, "refusal": private}, "stop"),
        "truncated": _response({"content": private, "tool_calls": [_WRITE]}, "length"),
        "malformed_response": _response({"content": [private], "tool_calls": [_WRITE]}, "tool_calls"),
        "malformed_tools": _response({"content": private, "tool_calls": [_WRITE, {
            "id": "finish-1", "function": {"name": "finish", "arguments": "{"},
        }]}, "tool_calls"),
    }
    requests = _scripted_http(monkeypatch, [responses[outcome]])
    activity_path = tmp_path / "rejected.jsonl"
    with _native_activity(activity_path):
        result = await client._openrouter_agentic(
            private, str(tmp_path), "test/model",
            enforce_antistub=False, verify_on_stop=False,
        )
    assert result["completed"] is False
    assert result["ok"] is False
    assert len(requests) == count
    assert client.budget.spent_day == pytest.approx(0.001 * count)
    assert not (tmp_path / "app.py").exists()
    assert private not in activity_path.read_text()
    assert private not in str(result.get("error"))


async def test_retry_after_http_date_format(monkeypatch):
    from datetime import UTC, datetime, timedelta
    future = datetime.now(UTC) + timedelta(seconds=15)
    date_str = future.strftime("%a, %d %b %Y %H:%M:%S GMT")

    clock = [0.0]
    attempts = []

    async def sleep(delay):
        clock[0] += delay

    async def attempt(model):
        attempts.append(clock[0])
        if len(attempts) == 1:
            req = httpx.Request("POST", llm.OPENROUTER_URL)
            resp = httpx.Response(429, headers={"Retry-After": date_str}, request=req)
            raise httpx.HTTPStatusError("rate limited", request=req, response=resp)
        return "ok"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = await _client(
        llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=30,
    )._resilient_call("test/model", Tier.CHEAP, attempt)
    assert result == "ok"
    assert len(attempts) == 2
    assert 10 <= attempts[1] - attempts[0] <= 30


@pytest.mark.parametrize("retry_after", ["0", "-5", "invalid", "NaN", "inf"])
async def test_invalid_and_zero_retry_after_falls_back_to_local_backoff(monkeypatch, retry_after):
    clock = [0.0]
    attempts = []

    async def sleep(delay):
        clock[0] += delay

    async def attempt(model):
        attempts.append(clock[0])
        if len(attempts) == 1:
            req = httpx.Request("POST", llm.OPENROUTER_URL)
            resp = httpx.Response(429, headers={"Retry-After": retry_after}, request=req)
            raise httpx.HTTPStatusError("rate limited", request=req, response=resp)
        return "ok"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = await _client(
        llm_max_retries=1, llm_retry_base_delay=2.0, llm_retry_max_delay=30,
    )._resilient_call("test/model", Tier.CHEAP, attempt)
    assert result == "ok"
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] > 0.0


async def test_deadline_rejection_stops_before_sleeping(monkeypatch):
    attempts = []

    async def sleep(delay):
        pass

    async def attempt(model):
        attempts.append(model)
        if len(attempts) == 1:
            raise _rate_limit("20")
        return "unexpected"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(httpx.HTTPStatusError):
        await _client(
            llm_max_retries=1,
            llm_retry_base_delay=0,
            llm_retry_max_delay=30,
            llm_call_deadline_seconds=5,
        )._resilient_call("test/model", Tier.CHEAP, attempt)
    assert len(attempts) == 1


async def test_cancellation_during_backoff_propagates(monkeypatch):
    attempts = []

    async def sleep(delay):
        raise asyncio.CancelledError("cancelled during sleep")

    async def attempt(model):
        attempts.append(model)
        if len(attempts) == 1:
            raise _rate_limit("10")
        return "unexpected"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await _client(
            llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=30,
        )._resilient_call("test/model", Tier.CHEAP, attempt)
    assert len(attempts) == 1


async def test_retry_activity_emitted_before_waiting_secret_safe(monkeypatch, tmp_path):
    from skyn3t.observability.activity import run_with_activity

    events_emitted = []

    async def attempt(model):
        if len(events_emitted) == 0:
            events_emitted.append("attempt1")
            raise _rate_limit("5")
        events_emitted.append("attempt2")
        return {"status": "completed", "result": "ok"}

    async def producer():
        return await _client(
            llm_max_retries=1, llm_retry_base_delay=0, llm_retry_max_delay=30,
        )._resilient_call("test/model", Tier.CHEAP, attempt)

    real_sleep = asyncio.sleep

    async def mock_sleep(delay):
        await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)
    act_file = tmp_path / "activity.jsonl"
    res = await run_with_activity(act_file, "build", "test_proj", producer)
    assert res == {"status": "completed", "result": "ok"}
    content = act_file.read_text()
    assert "Retry decision" in content
    assert "test-only" not in content  # API key secret-safe
