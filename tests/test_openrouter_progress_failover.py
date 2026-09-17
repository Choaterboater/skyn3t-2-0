"""Regression coverage for model recovery without losing useful file writes."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

import skyn3t.adapters.llm as llm
from skyn3t.config.settings import Settings

PRIMARY = "diagnostic/reader:free"
FALLBACK = "diagnostic/writer:free"
SECOND = "diagnostic/second:free"


def tool(name, arguments):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "",
        "tool_calls": [{
            "id": "call",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }}]}


class Provider:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []
        self.counts = Counter()
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json, **kwargs):
        model = json["model"]
        self.calls.append(model)
        self.counts[model] += 1
        self.requests.append(deepcopy(json))
        if len(self.calls) > 40:
            raise RuntimeError("test provider exhausted its request budget")
        payload = self.respond(model, self.counts[model])
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    for index in range(3):
        (project / f"context-{index}.py").write_text(f"context = {index}\n")

    def make(respond, *, candidates=None, **overrides):
        values = dict(
            _env_file=None,
            llm_backend="openrouter",
            openrouter_api_key="test-only",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            vector_db_path=tmp_path / "vectors",
            projects_dir=tmp_path / "projects",
            free_only=True,
            auto_route=False,
            model_evolution=False,
            llm_max_retries=0,
            llm_fallback_models=f"{FALLBACK},{SECOND}",
            openrouter_agentic_max_turns=4,
            openrouter_agentic_no_write_turns=4,
            agentic_verify_on_stop=False,
        )
        values.update(overrides)
        client = llm.LLMClient(Settings(**values))
        provider = Provider(respond)
        monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: provider)
        monkeypatch.setattr(
            client, "_healthy_fallback_models",
            lambda model, tier: (
                candidates(model) if candidates is not None else [FALLBACK, SECOND]
            ),
        )
        return client, provider, project

    return make


def run(client, project):
    return asyncio.run(client._openrouter_agentic(
        "Preserve existing work and complete the repair.",
        str(project), PRIMARY, stack="python", timeout=5,
        enforce_antistub=False, verify_on_stop=False,
    ))


def recover(model, count):
    if model == PRIMARY:
        if count == 1:
            return tool("write_file", {"path": "preserved.py", "content": "value = 1\n"})
        return tool("read_file", {"path": f"context-{count % 3}.py"})
    if count == 1:
        return tool("write_file", {"path": "recovered.py", "content": "recovered = True\n"})
    return tool("finish", {})


@pytest.mark.parametrize("early_limit", [0, 2, 4])
def test_read_only_turns_fail_over_at_early_or_ultimate_limit(setup, early_limit):
    client, provider, project = setup(
        recover, openrouter_agentic_no_write_turns=early_limit
    )
    activity = AsyncMock()
    client._report_retry_decision = activity
    result = run(client, project)
    limit = early_limit or 4
    assert provider.calls == [PRIMARY] * (limit + 1) + [FALLBACK] * 2
    assert result["ok"]
    assert result["progress_fallbacks"] == 1
    assert result["stalled"]
    assert result["attempted_model"] == PRIMARY
    assert result["model"] == result["fallback_model"] == client.last_model == FALLBACK
    assert (project / "preserved.py").read_text() == "value = 1\n"
    assert (project / "recovered.py").read_text() == "recovered = True\n"
    assert len({request["session_id"] for request in provider.requests}) == 1
    assert any(
        message.get("role") == "assistant" and "preserved.py" in json.dumps(message)
        for message in provider.requests[-1]["messages"]
    )
    activity.assert_awaited_once_with(
        model=FALLBACK, attempt=1, reason="no_write_progress",
        delay=0.0, outcome="model_failover",
    )


@pytest.mark.parametrize("same_content", [False, True])
def test_doom_loops_switch_without_resetting_existing_work(setup, same_content):
    def respond(model, count):
        if model != PRIMARY or count == 1:
            return recover(model, count)
        if same_content:
            return tool("write_file", {"path": "preserved.py", "content": "value = 1\n"})
        return tool("read_file", {"path": "context-0.py"})

    client, provider, project = setup(
        respond, openrouter_agentic_no_write_turns=0,
        openrouter_agentic_max_turns=60,
    )
    result = run(client, project)
    assert result["ok"]
    assert result["files_written"] == 2
    assert provider.counts[PRIMARY] <= 8
    assert result["progress_fallbacks"] == 1


def test_productive_writes_reset_turn_limit_without_capping_total_turns(setup):
    def respond(model, count):
        if count > 12:
            return tool("finish", {})
        if count % 2:
            return tool("read_file", {"path": f"context-{count % 3}.py"})
        return tool("write_file", {"path": f"module_{count}.py", "content": f"n = {count}\n"})

    client, provider, project = setup(respond, openrouter_agentic_no_write_turns=2)
    result = run(client, project)
    assert result["ok"]
    assert result["files_written"] == 6
    assert result["progress_fallbacks"] == 0
    assert provider.calls == [PRIMARY] * 13


def test_disabled_fallback_does_not_switch_even_if_candidates_exist(setup):
    client, provider, project = setup(recover, llm_fallback_enabled=False)
    result = run(client, project)
    assert not result["ok"]
    assert result["error"]
    assert provider.calls == [PRIMARY] * 5
    assert result["progress_fallbacks"] == 0


@pytest.mark.parametrize("cap, expected", [(1, [PRIMARY, FALLBACK]), (0, [PRIMARY, FALLBACK, SECOND])])
def test_exhausted_models_do_not_cycle_when_quarantine_expires(setup, cap, expected):
    def respond(model, count):
        return tool("read_file", {"path": f"context-{count % 3}.py"})

    client, provider, project = setup(
        respond, llm_max_fallbacks=cap, llm_unhealthy_ttl_seconds=0,
        openrouter_agentic_no_write_turns=2,
        candidates=lambda model: [PRIMARY, FALLBACK, SECOND],
    )
    result = run(client, project)
    assert not result["ok"]
    assert result["error"]
    assert list(dict.fromkeys(provider.calls)) == expected
    assert provider.calls == [model for model in expected for _ in range(2)]
    assert result["progress_fallbacks"] == len(expected) - 1


@pytest.mark.parametrize("failure", ["no_write_progress", "model_unavailable"])
def test_public_agentic_disabled_fallback_keeps_every_request_on_the_pinned_model(
    tmp_path, monkeypatch, failure
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("value = 1\n")
    settings = Settings(
        _env_file=None, llm_backend="openrouter", openrouter_api_key="test-only",
        data_dir=tmp_path / "data", logs_dir=tmp_path / "logs",
        projects_dir=tmp_path / "projects", vector_db_path=tmp_path / "vectors",
        free_only=True, auto_route=False, model_evolution=False,
        preferred_model=PRIMARY, openrouter_codegen_model=PRIMARY,
        llm_fallback_enabled=False, llm_max_retries=0, llm_max_fallbacks=0,
        llm_fallback_models=FALLBACK,
        openrouter_agentic_max_turns=2, openrouter_agentic_no_write_turns=2,
        agentic_verify_on_stop=False,
    )
    def respond(model, count):
        if model != PRIMARY:
            return tool("finish", {})
        if failure == "model_unavailable":
            return httpx.Response(
                404, json={"error": {"message": "No endpoints found"}},
                request=httpx.Request("POST", llm.OPENROUTER_URL),
            )
        return tool("read_file", {"path": "main.py"})

    provider = Provider(respond)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: provider)
    client = llm.LLMClient(settings)
    result = asyncio.run(client.agentic_build(
        "Inspect the existing source.", str(project),
        model=PRIMARY, stack="python", timeout=5,
    ))
    assert provider.calls == [PRIMARY] * (2 if failure == "no_write_progress" else 1)
    assert not result["ok"]
    assert result["progress_fallbacks"] == 0


def test_transport_fallback_cannot_resurrect_session_exhausted_model(setup):
    def respond(model, count):
        if model == PRIMARY:
            return recover(model, count)
        if model == FALLBACK:
            return httpx.Response(
                404, json={"error": {"message": "No endpoints found"}},
                request=httpx.Request("POST", llm.OPENROUTER_URL),
            )
        return recover(model, count)

    client, provider, project = setup(
        respond, llm_unhealthy_ttl_seconds=0,
        candidates=lambda model: [PRIMARY, FALLBACK, SECOND],
    )
    result = run(client, project)
    assert result["ok"]
    assert provider.calls == [PRIMARY] * 5 + [FALLBACK] + [SECOND] * 2
    assert result["model"] == SECOND
    assert (project / "preserved.py").exists()
