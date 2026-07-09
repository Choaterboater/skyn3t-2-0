"""Deterministic mock-LLM provider server (research item 42, wave-2 keystone).

skyn3t builds apps that CALL LLMs, but a generated app cannot be proven headlessly
without a real endpoint + key. This is that endpoint: a local, dependency-light
(stdlib :class:`~http.server.ThreadingHTTPServer`) fixture speaking BOTH the
OpenAI ``/v1/chat/completions`` and Anthropic ``/v1/messages`` shapes, so
``proof_run`` can point a generated LLM app at it and drive the app's own tests
with **zero API spend and fully deterministic output**.

Design mirrors claw-code's ``mock-anthropic-service`` / aimock:
- **Scenario routing** — a token embedded in the prompt selects the response
  (``SCENARIO:refuse`` -> a refusal; ``SCENARIO:tool`` -> a tool/function call;
  default -> a short completion carrying :data:`MOCK_MARKER` + a stable hash of
  the last user message, so a generated test can assert *real* plumbing).
- **Tool use** — when the request declares tools/functions AND the scenario asks,
  a syntactically valid tool call is returned (OpenAI ``tool_calls`` / Anthropic
  ``tool_use``); a tool scenario with NO declared tools falls back to text.
- **Request capture** — a bounded ring of :class:`CapturedRequest` records,
  readable in-process (:meth:`MockLLMServer.captured_requests`) or over HTTP
  (``GET /__captured``), so a proof can assert *what the app actually sent*.

No auth is required; any ``Authorization`` / ``x-api-key`` header is accepted and
IGNORED. Port 0 (OS-assigned) by default. Shutdown never raises. Import has zero
side effects.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Stable marker every default completion carries, so a generated app's test can
# assert the call actually reached a live provider (real plumbing, not a stub).
MOCK_MARKER = "MOCK_LLM_OK"

# Prompt tokens that select a non-default response.
_SCENARIO_REFUSE = "SCENARIO:refuse"
_SCENARIO_TOOL = "SCENARIO:tool"

_CHAT_PATH = "/v1/chat/completions"
_MESSAGES_PATH = "/v1/messages"


@dataclass(slots=True)
class CapturedRequest:
    """One request the mock received — enough for a proof to assert intent."""

    endpoint: str
    model: str
    messages: list[Any]
    stream: bool
    tools: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "messages": self.messages,
            "stream": self.stream,
            "tools": self.tools,
        }


# --- response planning (transport-agnostic) ---------------------------------


def _text_of(content: Any) -> str:
    """Flatten a message ``content`` (a str OR a list of typed blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # Anthropic/OpenAI content blocks: {"type":"text","text":...}.
                t = block.get("text") or block.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts)
    return ""


def _all_prompt_text(messages: list[Any], system: Any = None) -> str:
    parts = [_text_of(system)] if system is not None else []
    for m in messages or []:
        if isinstance(m, dict):
            parts.append(_text_of(m.get("content")))
    return "\n".join(p for p in parts if p)


def _last_user_text(messages: list[Any]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return _text_of(m.get("content"))
    # No explicit user role — fall back to the last message of any role.
    for m in reversed(messages or []):
        if isinstance(m, dict):
            return _text_of(m.get("content"))
    return ""


def _first_tool_name(tools: Any) -> str | None:
    """The first declared tool/function name, across OpenAI + Anthropic shapes."""
    if not isinstance(tools, list):
        return None
    for t in tools:
        if not isinstance(t, dict):
            continue
        # OpenAI: {"type":"function","function":{"name":...}}
        fn = t.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            return fn["name"]
        # Anthropic + OpenAI legacy functions: {"name":...}
        if isinstance(t.get("name"), str):
            return t["name"]
    return None


@dataclass(slots=True)
class _Plan:
    """The transport-agnostic decision for a request."""

    kind: str  # "text" | "tool"
    text: str = ""
    tool_name: str = ""


def _plan_response(messages: list[Any], tools: Any, system: Any = None) -> _Plan:
    prompt = _all_prompt_text(messages, system)
    if _SCENARIO_REFUSE in prompt:
        return _Plan("text", text="I can't help with that request. (SCENARIO:refuse)")
    if _SCENARIO_TOOL in prompt:
        name = _first_tool_name(tools)
        if name:  # only emit a tool call when the request actually declared one
            return _Plan("tool", tool_name=name)
    echo = hashlib.sha256(_last_user_text(messages).encode("utf-8")).hexdigest()[:12]
    return _Plan("text", text=f"{MOCK_MARKER} echo={echo}")


def _tok(text: str) -> int:
    """A crude but DETERMINISTIC token estimate (never zero)."""
    return max(1, len(text) // 4)


# --- OpenAI envelope builders ------------------------------------------------


def _openai_json(model: str, plan: _Plan, prompt_text: str) -> dict[str, Any]:
    if plan.kind == "tool":
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_mock_0",
                "type": "function",
                "function": {"name": plan.tool_name, "arguments": "{}"},
            }],
        }
        finish = "tool_calls"
        out_text = plan.tool_name
    else:
        message = {"role": "assistant", "content": plan.text}
        finish = "stop"
        out_text = plan.text
    p, c = _tok(prompt_text), _tok(out_text)
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c},
    }


def _openai_sse(model: str, plan: _Plan) -> str:
    """OpenAI streaming body: role delta, content/tool delta(s), stop, [DONE]."""
    def chunk(delta: dict, finish: Any = None) -> str:
        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return "data: " + json.dumps(payload) + "\n\n"

    parts = [chunk({"role": "assistant"})]
    if plan.kind == "tool":
        parts.append(chunk({"tool_calls": [{
            "index": 0, "id": "call_mock_0", "type": "function",
            "function": {"name": plan.tool_name, "arguments": "{}"},
        }]}))
        parts.append(chunk({}, finish="tool_calls"))
    else:
        # Two content deltas so a consumer sees real incremental streaming; the
        # first carries the marker so a test can assert it in the stream.
        mid = max(1, len(plan.text) // 2)
        parts.append(chunk({"content": plan.text[:mid]}))
        parts.append(chunk({"content": plan.text[mid:]}))
        parts.append(chunk({}, finish="stop"))
    parts.append("data: [DONE]\n\n")
    return "".join(parts)


# --- Anthropic envelope builders --------------------------------------------


def _anthropic_json(model: str, plan: _Plan, prompt_text: str) -> dict[str, Any]:
    if plan.kind == "tool":
        content = [{"type": "tool_use", "id": "toolu_mock_0",
                    "name": plan.tool_name, "input": {}}]
        stop_reason = "tool_use"
        out_text = plan.tool_name
    else:
        content = [{"type": "text", "text": plan.text}]
        stop_reason = "end_turn"
        out_text = plan.text
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": _tok(prompt_text), "output_tokens": _tok(out_text)},
    }


def _anthropic_sse(model: str, plan: _Plan, prompt_text: str) -> str:
    """Minimal Anthropic message stream (text scenarios stream text deltas)."""
    def event(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    start_msg = _anthropic_json(model, _Plan("text", text=""), prompt_text)
    start_msg["content"] = []
    out: list[str] = [event("message_start", {"type": "message_start", "message": start_msg})]
    if plan.kind == "tool":
        out.append(event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_mock_0",
                              "name": plan.tool_name, "input": {}}}))
        out.append(event("content_block_stop", {"type": "content_block_stop", "index": 0}))
        stop_reason = "tool_use"
    else:
        out.append(event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}))
        out.append(event("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": plan.text}}))
        out.append(event("content_block_stop", {"type": "content_block_stop", "index": 0}))
        stop_reason = "end_turn"
    out.append(event("message_delta", {
        "type": "message_delta", "delta": {"stop_reason": stop_reason}}))
    out.append(event("message_stop", {"type": "message_stop"}))
    return "".join(out)


# --- HTTP plumbing -----------------------------------------------------------


class _MockHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, *, capture_max: int):
        super().__init__(addr, handler)
        self._captured: deque[CapturedRequest] = deque(maxlen=capture_max)
        self._lock = threading.Lock()

    def record(self, req: CapturedRequest) -> None:
        with self._lock:
            self._captured.append(req)

    def snapshot(self) -> list[CapturedRequest]:
        with self._lock:
            return list(self._captured)


class _Handler(BaseHTTPRequestHandler):
    # Silence the default stderr access log — this is a test/proof fixture.
    def log_message(self, *args: Any) -> None:  # noqa: D401
        return

    protocol_version = "HTTP/1.1"

    # -- response helpers --
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def _sse(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/event-stream; charset=utf-8")

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        return data if isinstance(data, dict) else {}

    # -- routes --
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__captured":
            self._json([r.to_dict() for r in self.server.snapshot()])  # type: ignore[attr-defined]
        elif path in ("/health", "/"):
            self._json({"status": "ok", "service": "mock_llm"})
        else:
            self._json({"error": {"message": f"no route {path}"}}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        messages_value = body.get("messages")
        messages: list[Any] = messages_value if isinstance(messages_value, list) else []
        model_value = body.get("model")
        model = model_value if isinstance(model_value, str) else "mock-model"
        stream = bool(body.get("stream"))
        tools = body.get("tools") or body.get("functions")
        system = body.get("system")

        if path == _CHAT_PATH or path == _MESSAGES_PATH:
            self.server.record(CapturedRequest(  # type: ignore[attr-defined]
                endpoint=path, model=model, messages=messages, stream=stream,
                tools=tools if isinstance(tools, list) else [],
            ))
            plan = _plan_response(messages, tools, system)
            prompt_text = _all_prompt_text(messages, system)
            if path == _CHAT_PATH:
                if stream:
                    self._sse(_openai_sse(model, plan))
                else:
                    self._json(_openai_json(model, plan, prompt_text))
            else:  # Anthropic messages
                if stream:
                    self._sse(_anthropic_sse(model, plan, prompt_text))
                else:
                    self._json(_anthropic_json(model, plan, prompt_text))
            return
        self._json({"error": {"message": f"no route {path}"}}, status=404)


class MockLLMServer:
    """A local OpenAI/Anthropic-compatible fixture server.

    Usage::

        with MockLLMServer() as srv:
            base = srv.base_url            # http://127.0.0.1:<port>
            ... point the app's OPENAI_BASE_URL / ANTHROPIC_BASE_URL at it ...
            reqs = srv.captured_requests()

    or explicitly ``start()`` / ``stop()``. Port 0 => OS-assigned. ``stop()`` is
    idempotent and never raises.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, capture_max: int = 256):
        self._host = host
        self._port = port
        self._capture_max = capture_max
        self._server: _MockHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --
    def start(self) -> str:
        """Start serving on a background thread; return the base URL."""
        if self._server is not None:
            return self.base_url
        self._server = _MockHTTPServer(
            (self._host, self._port), _Handler, capture_max=self._capture_max)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
            name="mock-llm", daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        """Stop the server and join its thread. Idempotent; never raises."""
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None:
            try:
                thread.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    # -- introspection --
    @property
    def base_url(self) -> str:
        if self._server is None:
            return ""
        host, port = self._server.server_address[0], self._server.server_address[1]
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="replace")
        return f"http://{host}:{port}"

    def captured_requests(self) -> list[CapturedRequest]:
        """The captured requests seen so far (in arrival order)."""
        return self._server.snapshot() if self._server is not None else []

    # -- context manager --
    def __enter__(self) -> MockLLMServer:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
