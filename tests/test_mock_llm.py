"""Deterministic mock-LLM provider server (research item 42).

A local, dependency-light OpenAI/Anthropic-compatible fixture server so a
generated LLM-calling app can be proven headlessly with ZERO API spend. These
tests exercise the server directly (real HTTP over a loopback socket): the
OpenAI JSON + SSE shapes, the Anthropic shape, scenario routing, tool calls,
request capture, header tolerance, and clean lifecycle.
"""

from __future__ import annotations

import json
import urllib.request

from skyn3t.studio.mock_llm import MOCK_MARKER, MockLLMServer


def _post(url: str, payload: dict, headers: dict | None = None):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_openai_chat_completion_json_envelope():
    with MockLLMServer() as srv:
        status, ctype, raw = _post(
            srv.base_url + "/v1/chat/completions",
            {"model": "gpt-test", "messages": [{"role": "user", "content": "hello world"}]},
        )
    assert status == 200
    assert "application/json" in ctype
    data = json.loads(raw)
    assert data["object"] == "chat.completion"
    assert data["model"] == "gpt-test"
    msg = data["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert MOCK_MARKER in msg["content"]
    # Deterministic echo of the last user message so tests can assert real plumbing.
    assert data["choices"][0]["finish_reason"] == "stop"
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert isinstance(data["usage"][k], int)


def test_openai_completion_is_deterministic_per_input():
    with MockLLMServer() as srv:
        _, _, a = _post(srv.base_url + "/v1/chat/completions",
                        {"messages": [{"role": "user", "content": "same input"}]})
        _, _, b = _post(srv.base_url + "/v1/chat/completions",
                        {"messages": [{"role": "user", "content": "same input"}]})
        _, _, c = _post(srv.base_url + "/v1/chat/completions",
                        {"messages": [{"role": "user", "content": "different"}]})
    ca = json.loads(a)["choices"][0]["message"]["content"]
    cb = json.loads(b)["choices"][0]["message"]["content"]
    cc = json.loads(c)["choices"][0]["message"]["content"]
    assert ca == cb          # same input -> identical completion
    assert ca != cc          # a different last message changes the echo hash


def test_openai_streaming_sse():
    with MockLLMServer() as srv:
        status, ctype, raw = _post(
            srv.base_url + "/v1/chat/completions",
            {"model": "gpt-test", "stream": True,
             "messages": [{"role": "user", "content": "stream please"}]},
        )
    assert status == 200
    assert "text/event-stream" in ctype
    assert "data:" in raw
    assert MOCK_MARKER in raw            # the streamed delta carries the marker
    assert "chat.completion.chunk" in raw
    assert raw.rstrip().endswith("[DONE]")   # OpenAI stream terminator


def test_anthropic_messages_json_envelope():
    with MockLLMServer() as srv:
        status, ctype, raw = _post(
            srv.base_url + "/v1/messages",
            {"model": "claude-test", "max_tokens": 64,
             "messages": [{"role": "user", "content": "hi there"}]},
        )
    assert status == 200
    data = json.loads(raw)
    assert data["type"] == "message"
    assert data["role"] == "assistant"
    assert data["model"] == "claude-test"
    assert data["content"][0]["type"] == "text"
    assert MOCK_MARKER in data["content"][0]["text"]
    assert data["stop_reason"] == "end_turn"
    for k in ("input_tokens", "output_tokens"):
        assert isinstance(data["usage"][k], int)


def test_scenario_refuse_routes_to_refusal():
    with MockLLMServer() as srv:
        _, _, raw = _post(
            srv.base_url + "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "please SCENARIO:refuse now"}]},
        )
    content = json.loads(raw)["choices"][0]["message"]["content"].lower()
    assert "can't" in content or "cannot" in content or "unable" in content
    assert MOCK_MARKER not in json.loads(raw)["choices"][0]["message"]["content"]


def test_openai_tool_call_when_declared_and_requested():
    with MockLLMServer() as srv:
        _, _, raw = _post(
            srv.base_url + "/v1/chat/completions",
            {
                "messages": [{"role": "user", "content": "weather? SCENARIO:tool"}],
                "tools": [{
                    "type": "function",
                    "function": {"name": "get_weather",
                                 "parameters": {"type": "object",
                                                "properties": {"city": {"type": "string"}}}},
                }],
            },
        )
    data = json.loads(raw)
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    call = data["choices"][0]["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    # arguments must be a JSON STRING per the OpenAI schema, and parse cleanly.
    assert isinstance(call["function"]["arguments"], str)
    json.loads(call["function"]["arguments"])


def test_anthropic_tool_use_when_declared_and_requested():
    with MockLLMServer() as srv:
        _, _, raw = _post(
            srv.base_url + "/v1/messages",
            {
                "model": "claude-test", "max_tokens": 64,
                "messages": [{"role": "user", "content": "weather? SCENARIO:tool"}],
                "tools": [{"name": "get_weather",
                           "input_schema": {"type": "object",
                                            "properties": {"city": {"type": "string"}}}}],
            },
        )
    data = json.loads(raw)
    assert data["stop_reason"] == "tool_use"
    block = next(b for b in data["content"] if b["type"] == "tool_use")
    assert block["name"] == "get_weather"
    assert isinstance(block["input"], dict)


def test_tool_scenario_without_declared_tools_falls_back_to_text():
    # SCENARIO:tool but NO tools declared -> a tool call would be invalid, so
    # the server returns a normal completion instead of a bogus function call.
    with MockLLMServer() as srv:
        _, _, raw = _post(
            srv.base_url + "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "SCENARIO:tool"}]},
        )
    data = json.loads(raw)
    assert data["choices"][0]["finish_reason"] == "stop"
    assert MOCK_MARKER in data["choices"][0]["message"]["content"]


def test_captured_requests_endpoint():
    with MockLLMServer() as srv:
        _post(srv.base_url + "/v1/chat/completions",
              {"model": "m1", "messages": [{"role": "user", "content": "capture me"}]})
        _post(srv.base_url + "/v1/messages",
              {"model": "m2", "max_tokens": 8, "stream": True,
               "messages": [{"role": "user", "content": "and me"}]})
        status, raw = _get(srv.base_url + "/__captured")
    assert status == 200
    captured = json.loads(raw)
    assert len(captured) == 2
    first, second = captured
    assert first["endpoint"].endswith("/v1/chat/completions")
    assert first["model"] == "m1"
    assert first["stream"] is False
    assert any("capture me" in json.dumps(first["messages"]) for _ in [0])
    assert second["endpoint"].endswith("/v1/messages")
    assert second["model"] == "m2"
    assert second["stream"] is True


def test_capture_also_available_in_process():
    with MockLLMServer() as srv:
        _post(srv.base_url + "/v1/chat/completions",
              {"model": "mx", "messages": [{"role": "user", "content": "X-MARKER-42"}]})
        recs = srv.captured_requests()
    assert recs
    assert recs[-1].model == "mx"
    assert "X-MARKER-42" in json.dumps(recs[-1].messages)


def test_auth_headers_are_accepted_and_ignored():
    with MockLLMServer() as srv:
        status, _, raw = _post(
            srv.base_url + "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer totally-fake",
                     "x-api-key": "sk-not-real-key"},
        )
    assert status == 200
    assert MOCK_MARKER in json.loads(raw)["choices"][0]["message"]["content"]


def test_context_manager_stops_server():
    srv = MockLLMServer()
    with srv as entered:
        assert entered is srv
        base = srv.base_url
        assert base.startswith("http://127.0.0.1:")
        status, _, _ = _post(base + "/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "up"}]})
        assert status == 200
    # After exit the port is closed — a connect must fail, not hang.
    import urllib.error
    try:
        _post(base + "/v1/chat/completions",
              {"messages": [{"role": "user", "content": "down"}]})
        raised = False
    except (urllib.error.URLError, OSError):
        raised = True
    assert raised


def test_stop_is_idempotent_and_never_raises():
    srv = MockLLMServer()
    srv.start()
    srv.stop()
    srv.stop()  # second stop must be a no-op, never raise
