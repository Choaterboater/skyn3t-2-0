"""Regression evidence for uncapped, cost-efficient full-app code generation."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import skyn3t.adapters.llm as llm_module
from skyn3t.adapters.llm import LLMClient
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import ModelRouter, Tier
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.runner import StudioRunner


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _RecordingClient:
    def __init__(self, turns: list[dict]):
        self.turns = list(turns)
        self.bodies: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, _url, json=None, headers=None, timeout=None):
        del headers, timeout
        self.bodies.append(copy.deepcopy(json))
        index = len(self.bodies) - 1
        return _Response(self.turns[index])


def _tool_turn(name: str, args: dict, call_id: str, *, cached=0, cache_write=0):
    return {
        "id": f"generation-{call_id}",
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            }
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {
                "cached_tokens": cached,
                "cache_write_tokens": cache_write,
            },
        },
    }


def test_large_unbounded_batches_do_not_resend_persisted_file_bodies(
    tmp_path, monkeypatch
):
    batches: list[list[dict[str, str]]] = []
    for batch_index in range(3):
        batches.append([
            {
                "path": f"src/b{batch_index}/module_{file_index}.js",
                "content": (
                    f"export const value = {batch_index * 32 + file_index};\n/*"
                    + "x" * 4096
                    + "*/\n"
                ),
            }
            for file_index in range(32)
        ])
    turns = [
        _tool_turn(
            "write_files",
            {"files": files},
            f"batch-{index}",
            cached=index * 100,
            cache_write=1000 if index == 0 else 0,
        )
        for index, files in enumerate(batches)
    ]
    turns.append(_tool_turn("finish", {}, "finish", cached=300))
    fake = _RecordingClient(turns)
    monkeypatch.setattr(
        llm_module.httpx, "AsyncClient", lambda *_args, **_kwargs: fake
    )
    client = LLMClient(Settings(
        llm_backend="openrouter",
        openrouter_api_key="test-key",
        agentic_verify_on_stop=False,
    ))

    result = asyncio.run(client._openrouter_agentic(
        "Build the complete app",
        str(tmp_path),
        "qwen/qwen3-coder:free",
        stack="fastapi",
        enforce_antistub=False,
        verify_on_stop=False,
    ))

    all_files = [item for batch in batches for item in batch]
    assert result["ok"] is True
    assert result["files_written"] == 96
    assert all((tmp_path / item["path"]).is_file() for item in all_files)

    # Prompt-cache routing is stable for every turn and retry in this session.
    session_ids = {body["session_id"] for body in fake.bodies}
    assert len(session_ids) == 1
    assert len(next(iter(session_ids))) <= 256
    assert all("max_tokens" not in body for body in fake.bodies)
    assert all("max_completion_tokens" not in body for body in fake.bodies)
    assert result["provider_requests"] == 4

    final_history = fake.bodies[-1]["messages"]
    serialized_history = json.dumps(final_history)
    assert "x" * 1024 not in serialized_history
    receipt_paths = []
    for message in final_history:
        for call in message.get("tool_calls", []):
            if call["function"]["name"] != "write_files":
                continue
            args = json.loads(call["function"]["arguments"])
            assert llm_module._PERSISTED_WRITE_RECEIPT_KEY in args
            assert all(
                llm_module._PERSISTED_WRITE_RECEIPT_KEY in item
                for item in args["files"]
            )
            receipt_paths.extend(item["path"] for item in args["files"])
    assert receipt_paths == [item["path"] for item in all_files]

    original_content_bytes = sum(
        len(item["content"].encode("utf-8")) for item in all_files
    )
    assert result["write_argument_bytes_compacted"] > original_content_bytes * 0.9
    assert result["context_bytes_sent"] < original_content_bytes // 2
    assert result["batch_write_calls"] == 3
    assert result["single_write_calls"] == 0
    assert result["cached_tokens"] == 600
    assert result["cache_write_tokens"] == 1000


def test_failed_or_partially_rejected_write_keeps_diagnostic_arguments():
    arguments = {
        "files": [
            {"path": "src/ok.js", "content": "ok"},
            {"path": "../outside.js", "content": "outside"},
        ]
    }
    tool_call = {
        "function": {
            "name": "write_files",
            "arguments": json.dumps(arguments),
        }
    }
    original = tool_call["function"]["arguments"]

    removed = LLMClient._compact_persisted_write_call(
        tool_call,
        arguments,
        "OK wrote batch (1 changed); rejected out-of-scope: ../outside.js",
    )

    assert removed == 0
    assert tool_call["function"]["arguments"] == original


def test_replayed_persisted_write_receipts_cannot_overwrite_real_file(
    tmp_path, monkeypatch
):
    original_args = {
        "path": "src/app.js",
        "content": "export const product = 'complete';\n/*" + "x" * 2048 + "*/\n",
    }
    historical_call = {
        "function": {
            "name": "write_file",
            "arguments": json.dumps(original_args),
        }
    }
    removed = LLMClient._compact_persisted_write_call(
        historical_call,
        original_args,
        "OK wrote src/app.js (35 bytes)",
    )
    current_receipt = json.loads(historical_call["function"]["arguments"])
    future_receipt = copy.deepcopy(current_receipt)
    future_receipt[llm_module._PERSISTED_WRITE_RECEIPT_KEY]["version"] = 999
    future_receipt["content"] = "opaque future receipt body"
    assert removed > 0

    turns = [
        _tool_turn("write_file", original_args, "original"),
        _tool_turn("write_file", current_receipt, "current-receipt"),
        _tool_turn("write_file", future_receipt, "future-receipt"),
        _tool_turn("finish", {}, "finish"),
    ]
    fake = _RecordingClient(turns)
    monkeypatch.setattr(
        llm_module.httpx, "AsyncClient", lambda *_args, **_kwargs: fake
    )
    client = LLMClient(Settings(
        llm_backend="openrouter",
        openrouter_api_key="test-key",
        agentic_verify_on_stop=False,
    ))

    result = asyncio.run(client._openrouter_agentic(
        "Build the complete app",
        str(tmp_path),
        "test/model",
        stack="fastapi",
        enforce_antistub=False,
        verify_on_stop=False,
    ))

    assert result["ok"] is True
    assert result["files_written"] == 1
    assert result["write_tool_calls"] == 3
    assert (tmp_path / "src" / "app.js").read_text(encoding="utf-8") == (
        original_args["content"]
    )
    tool_results = [
        str(message.get("content") or "")
        for message in fake.bodies[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert sum("persisted write receipt" in result for result in tool_results) == 2


def test_write_files_rejects_legacy_receipt_member_atomically(tmp_path, monkeypatch):
    original = "export const keep = 'real';\n"
    mixed_batch = {
        "files": [
            {"path": "src/new.js", "content": "export const added = true;\n"},
            {
                "path": "src/keep.js",
                "content": (
                    "[persisted to workspace: 28 UTF-8 bytes; "
                    "use read_file to inspect]"
                ),
            },
        ]
    }
    turns = [
        _tool_turn(
            "write_file",
            {"path": "src/keep.js", "content": original},
            "original",
        ),
        _tool_turn("write_files", mixed_batch, "legacy-receipt-batch"),
        _tool_turn("finish", {}, "finish"),
    ]
    fake = _RecordingClient(turns)
    monkeypatch.setattr(
        llm_module.httpx, "AsyncClient", lambda *_args, **_kwargs: fake
    )
    client = LLMClient(Settings(
        llm_backend="openrouter",
        openrouter_api_key="test-key",
        agentic_verify_on_stop=False,
    ))

    result = asyncio.run(client._openrouter_agentic(
        "Build the complete app",
        str(tmp_path),
        "test/model",
        stack="fastapi",
        enforce_antistub=False,
        verify_on_stop=False,
    ))

    assert result["ok"] is True
    assert result["files_written"] == 1
    assert result["batch_write_calls"] == 1
    assert (tmp_path / "src" / "keep.js").read_text(encoding="utf-8") == original
    assert not (tmp_path / "src" / "new.js").exists()
    tool_results = [
        str(message.get("content") or "")
        for message in fake.bodies[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert any("rejected atomically" in result for result in tool_results)


def test_preexisting_receipt_only_planned_files_cannot_pass_finish(
    tmp_path, monkeypatch
):
    legacy = (
        "[persisted to workspace: 28 UTF-8 bytes; use read_file to inspect]"
    )
    versioned = json.dumps({
        llm_module._PERSISTED_WRITE_RECEIPT_KEY: {
            "version": 42,
            "kind": "write_file",
            "bytes": 28,
        },
        "path": "src/app.js",
        "content": legacy,
    })

    for label, receipt_body in (("legacy", legacy), ("versioned", versioned)):
        workdir = tmp_path / label
        planned = workdir / "src" / "app.js"
        planned.parent.mkdir(parents=True)
        planned.write_text(receipt_body, encoding="utf-8")
        fake = _RecordingClient([
            _tool_turn("finish", {}, f"{label}-finish-1"),
            _tool_turn("finish", {}, f"{label}-finish-2"),
        ])
        monkeypatch.setattr(
            llm_module.httpx, "AsyncClient", lambda *_args, _fake=fake, **_kwargs: _fake
        )
        client = LLMClient(Settings(
            llm_backend="openrouter",
            openrouter_api_key="test-key",
            agentic_verify_on_stop=False,
        ))

        result = asyncio.run(client._openrouter_agentic(
            "Resume and finish the planned app",
            str(workdir),
            "test/model",
            stack="fastapi",
            planned_paths=["src/app.js"],
            enforce_antistub=False,
            verify_on_stop=False,
        ))

        assert result["ok"] is False
        assert result["completed"] is False
        assert result["files_written"] == 0
        assert "receipt metadata" in result["error"]
        assert len(fake.bodies) == 2
        assert "src/app.js" in json.dumps(fake.bodies[-1]["messages"])
        assert planned.read_text(encoding="utf-8") == receipt_body


def test_receipt_only_planned_file_recovers_after_real_rewrite(tmp_path, monkeypatch):
    planned = tmp_path / "src" / "app.js"
    planned.parent.mkdir(parents=True)
    planned.write_text(
        "[persisted to workspace: 28 UTF-8 bytes; use read_file to inspect]",
        encoding="utf-8",
    )
    real_body = "export const recovered = true;\n"
    turns = [
        _tool_turn("finish", {}, "premature-finish"),
        _tool_turn(
            "write_file",
            {"path": "src/app.js", "content": real_body},
            "real-rewrite",
        ),
        _tool_turn("finish", {}, "verified-finish"),
    ]
    fake = _RecordingClient(turns)
    monkeypatch.setattr(
        llm_module.httpx, "AsyncClient", lambda *_args, **_kwargs: fake
    )
    client = LLMClient(Settings(
        llm_backend="openrouter",
        openrouter_api_key="test-key",
        agentic_verify_on_stop=False,
    ))

    result = asyncio.run(client._openrouter_agentic(
        "Resume and finish the planned app",
        str(tmp_path),
        "test/model",
        stack="fastapi",
        planned_paths=["src/app.js"],
        enforce_antistub=False,
        verify_on_stop=False,
    ))

    assert result["ok"] is True
    assert result["completed"] is True
    assert result["files_written"] == 1
    assert len(fake.bodies) == 3
    assert planned.read_text(encoding="utf-8") == real_body


def test_non_agentic_openrouter_omits_output_limit_when_codegen_requests_none(
    monkeypatch,
):
    fake = _RecordingClient([{
        "id": "generation-cap-free",
        "choices": [{"message": {"content": "complete file"}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "cost": 0.001},
    }])
    monkeypatch.setattr(
        llm_module.httpx, "AsyncClient", lambda *_args, **_kwargs: fake
    )
    client = LLMClient(Settings(
        llm_backend="openrouter", openrouter_api_key="test-key", free_only=False
    ))

    result = asyncio.run(client._openrouter(
        "provider/model", "write it", "system", None, False
    ))

    assert result.text == "complete file"
    assert "max_tokens" not in fake.bodies[0]
    assert "max_completion_tokens" not in fake.bodies[0]


def test_agentic_read_and_list_tools_return_full_uncapped_workspace(
    tmp_path, monkeypatch
):
    large_content = "header\n" + "z" * 12_000 + "\nfooter\n"
    (tmp_path / "large.txt").write_text(large_content, encoding="utf-8")
    for index in range(240):
        path = tmp_path / "src" / f"module_{index:03d}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(index), encoding="utf-8")
    turns = [
        _tool_turn("read_file", {"path": "large.txt"}, "read"),
        _tool_turn("list_files", {}, "list"),
        _tool_turn(
            "write_file", {"path": "done.txt", "content": "complete"}, "write"
        ),
        _tool_turn("finish", {}, "finish"),
    ]
    fake = _RecordingClient(turns)
    monkeypatch.setattr(
        llm_module.httpx, "AsyncClient", lambda *_args, **_kwargs: fake
    )
    client = LLMClient(Settings(
        llm_backend="openrouter",
        openrouter_api_key="test-key",
        agentic_verify_on_stop=False,
    ))

    result = asyncio.run(client._openrouter_agentic(
        "inspect and finish",
        str(tmp_path),
        "qwen/qwen3-coder:free",
        enforce_antistub=False,
        verify_on_stop=False,
    ))

    read_result = fake.bodies[1]["messages"][-1]["content"]
    list_result = fake.bodies[2]["messages"][-1]["content"].splitlines()
    assert result["ok"] is True
    assert read_result == large_content
    listed_modules = [item for item in list_result if "module_" in item]
    assert len(listed_modules) == 240
    assert "src\\module_239.txt" in list_result or "src/module_239.txt" in list_result


def test_per_file_codegen_explicitly_requests_cap_free_completion():
    class _CodegenLLM:
        backend = "openrouter"

        def __init__(self):
            self.calls = []

        async def complete(self, prompt, **kwargs):
            self.calls.append({"prompt": prompt, **kwargs})
            return SimpleNamespace(
                backend="openrouter", text="value = 42\n"
            )

    codegen_llm = _CodegenLLM()
    agent = CodeAgent(event_bus=EventBus(), llm=codegen_llm)

    generated = asyncio.run(agent._generate_file(
        "src/main.py",
        "a complete python app",
        "python",
        {"files": [{"path": "src/main.py", "purpose": "entrypoint"}]},
    ))

    assert generated == "value = 42\n"
    assert codegen_llm.calls[0]["max_tokens"] is None


def test_codegen_trace_distinguishes_paid_request_from_free_effective_model(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        ModelRouter,
        "resolve",
        lambda self, tier, *args, **kwargs: (
            "qwen/qwen3-coder:free" if tier == Tier.BACKEND else "unexpected"
        ),
    )
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="test-key",
        free_only=True,
        codegen_cli_provider="",
        openrouter_codegen_model="provider/configured-paid",
        preferred_model="provider/preferred-paid",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
    )
    bus = EventBus()
    runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)

    predicted = runner._codegen_trace_model(
        "provider/requested-paid", profile="full_app"
    )

    assert predicted == "qwen/qwen3-coder:free"

    manifest = SimpleNamespace(extra={
        "requested_model_override": "provider/requested-paid",
        "requested_codegen_model": "provider/requested-paid",
        "effective_codegen_model": predicted,
        "codegen_model": predicted,
    })
    result = SimpleNamespace(
        output={"agentic": {"model": "qwen/effective-fallback:free"}},
        model_id="provider/stale-label",
    )
    assert runner._record_effective_codegen_trace(manifest, result) == (
        "qwen/effective-fallback:free"
    )

    summary = build_summary({"extra": manifest.extra})["model_trace"]
    assert summary["requested_codegen_model"] == "provider/requested-paid"
    assert summary["effective_codegen_model"] == "qwen/effective-fallback:free"
    assert summary["codegen_model"] == summary["effective_codegen_model"]
