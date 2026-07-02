"""Guards inside the OpenRouter agentic codegen loop (wave-1 item 19 / wave-2 item 49).

Doom-loop breaker: three consecutive IDENTICAL tool calls (same name+arguments)
get one corrective nudge; a repeat trip aborts the loop instead of burning the
turn/wall-clock budget. Verify-on-stop: the model's "finish" is only accepted
after a cheap static verification (unresolved local JS/Python imports — the
workstream-1 dangling-import class — plus Python syntax); a failure denies
finish with the REAL defect list, at most twice, then degrades open (the
pipeline fix-loop takes over — do-no-harm).

The scripted-HTTP test uses its own tiny server rather than MockLLMServer:
the mock's scenarios are deliberately stateless (same prompt -> same single
tool call), which cannot script a multi-turn session.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import skyn3t.adapters.llm as llm
from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _RecordingClient:
    """Replays canned OpenRouter responses AND records every POSTed body."""

    def __init__(self, turns):
        self._turns, self.i, self.bodies = list(turns), 0, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.bodies.append(json)
        if self.i >= len(self._turns):
            return _FakeResp({"choices": [{"message": {"content": "done"}}]})
        p = self._turns[self.i]
        self.i += 1
        return _FakeResp(p)


def _tool_turn(name, args, tcid="t1"):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": tcid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def _client(**kw):
    return LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x", **kw))


def _user_texts(bodies):
    """User-role texts from the FINAL request body. The loop mutates one shared
    messages list (each recorded body references it), so the last body holds the
    complete final history exactly once."""
    if not bodies:
        return []
    return [m.get("content", "") for m in bodies[-1]["messages"]
            if m.get("role") == "user"]


# ---------------------------------------------------------------------------
# Doom-loop breaker (item 49, threshold 3)
# ---------------------------------------------------------------------------


def test_doom_loop_corrective_nudge_then_abort(tmp_path, monkeypatch):
    same = _tool_turn("write_file", {"path": "same.js", "content": "x"})
    fake = _RecordingClient([same] * 12)  # never varies, never finishes
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)

    res = asyncio.run(_client()._openrouter_agentic("build", str(tmp_path), "m", stack="phaser"))

    # 3 identical calls -> one corrective nudge; 3 more -> abort. 6 POSTs, not 12/60.
    assert fake.i == 6, f"expected abort after 6 turns, got {fake.i}"
    assert any("identical tool call" in t.lower() for t in _user_texts(fake.bodies))
    assert res["ok"] is True  # the file WAS written; abort is not a build failure


def test_doom_loop_not_tripped_by_varying_calls(tmp_path, monkeypatch):
    turns = []
    for i in range(6):  # alternating targets -> legitimate progress, no trip
        turns.append(_tool_turn("write_file", {"path": f"f{i % 2}.js", "content": str(i)}))
    turns.append(_tool_turn("finish", {}, "tf"))
    fake = _RecordingClient(turns)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)

    res = asyncio.run(_client()._openrouter_agentic("build", str(tmp_path), "m", stack="phaser"))

    assert res["ok"] is True
    assert not any("identical tool call" in t.lower() for t in _user_texts(fake.bodies))


# ---------------------------------------------------------------------------
# Verify-on-stop (item 19, degrade-open)
# ---------------------------------------------------------------------------


def test_verify_on_stop_denies_finish_until_dangling_import_fixed(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_file", {
            "path": "src/main.js",
            "content": "import './PreloadScene.js';\nconsole.log('boot');\n"}),
        _tool_turn("finish", {}, "t2"),  # premature: PreloadScene.js never written
        _tool_turn("write_file", {
            "path": "src/PreloadScene.js", "content": "export const x = 1;\n"}, "t3"),
        _tool_turn("finish", {}, "t4"),
    ]
    fake = _RecordingClient(turns)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)

    res = asyncio.run(_client()._openrouter_agentic("build a game", str(tmp_path), "m", stack="phaser"))

    assert res["ok"] is True
    assert (tmp_path / "src" / "PreloadScene.js").exists()  # the denial drove the fix
    denials = [t for t in _user_texts(fake.bodies) if "VERIFY-ON-STOP" in t]
    assert denials and "PreloadScene" in denials[0]  # real defect text, not generic
    assert fake.i == 4


def test_verify_on_stop_catches_python_syntax_error(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_file", {"path": "main.py", "content": "def broken(:\n"}),
        _tool_turn("finish", {}, "t2"),
        _tool_turn("write_file", {"path": "main.py", "content": "def ok():\n    return 1\n"}, "t3"),
        _tool_turn("finish", {}, "t4"),
    ]
    fake = _RecordingClient(turns)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)

    res = asyncio.run(_client()._openrouter_agentic("an api", str(tmp_path), "m", stack="fastapi"))

    assert res["ok"] is True
    denials = [t for t in _user_texts(fake.bodies) if "VERIFY-ON-STOP" in t]
    assert denials and "syntax" in denials[0].lower()
    compile((tmp_path / "main.py").read_text(), "main.py", "exec")  # fixed version kept


def test_verify_on_stop_degrades_open_after_two_denials(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_file", {
            "path": "src/main.js", "content": "import './Missing.js';\n"}),
        _tool_turn("finish", {}, "t2"),  # denial 1
        _tool_turn("finish", {}, "t3"),  # denial 2
        _tool_turn("finish", {}, "t4"),  # accepted: degrade-open, fix-loop's problem now
    ]
    fake = _RecordingClient(turns)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)

    res = asyncio.run(_client()._openrouter_agentic("x", str(tmp_path), "m", stack="phaser"))

    assert res["ok"] is True  # never brick a build on the verifier
    assert fake.i == 4
    assert len([t for t in _user_texts(fake.bodies) if "VERIFY-ON-STOP" in t]) == 2


def test_verify_on_stop_disabled_by_setting(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_file", {
            "path": "src/main.js", "content": "import './Missing.js';\n"}),
        _tool_turn("finish", {}, "t2"),
    ]
    fake = _RecordingClient(turns)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)

    res = asyncio.run(_client(agentic_verify_on_stop=False)
                      ._openrouter_agentic("x", str(tmp_path), "m", stack="phaser"))

    assert res["ok"] is True
    assert fake.i == 2  # finish accepted immediately
    assert not any("VERIFY-ON-STOP" in t for t in _user_texts(fake.bodies))


# ---------------------------------------------------------------------------
# Zero-spend own-CI: the loop over a REAL HTTP seam (no monkeypatched client)
# ---------------------------------------------------------------------------


class _ScriptedHandler(BaseHTTPRequestHandler):
    turns: list[dict] = []
    captured: list[dict] = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        with self.lock:
            type(self).captured.append(
                {"body": body, "auth": self.headers.get("Authorization", "")})
            i = len(type(self).captured) - 1
            payload = (type(self).turns[i] if i < len(type(self).turns)
                       else {"choices": [{"message": {"content": "done"}}]})
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def test_agentic_loop_end_to_end_over_real_http(tmp_path, monkeypatch):
    _ScriptedHandler.turns = [
        _tool_turn("write_file", {"path": "src/main.js", "content": "console.log(1);\n"}),
        _tool_turn("finish", {}, "t2"),
    ]
    _ScriptedHandler.captured = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(
            llm, "OPENROUTER_URL",
            f"http://127.0.0.1:{srv.server_port}/api/v1/chat/completions")
        res = asyncio.run(_client()._openrouter_agentic("build", str(tmp_path), "m", stack="phaser"))
    finally:
        srv.shutdown()
        srv.server_close()

    assert res["ok"] is True
    assert (tmp_path / "src" / "main.js").read_text() == "console.log(1);\n"
    first = _ScriptedHandler.captured[0]
    assert first["auth"] == "Bearer x"  # real header over the wire
    tool_names = {t["function"]["name"] for t in first["body"]["tools"]}
    assert {"write_file", "finish"} <= tool_names
