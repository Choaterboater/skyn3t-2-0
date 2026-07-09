"""Deterministic MCP-server gate (``skyn3t.studio.mcp_check``) — wave-2 §3.3.

The gate spawns the delivered ``server.py`` and speaks the MCP JSON-RPC protocol
over stdio: ``initialize`` handshake → ``tools/list`` → ``tools/call`` each tool
with schema-derived fixture args → one malformed call. It NEVER hangs a build
(every read is time-bounded), NEVER raises, and soft-skips when the mcp SDK / a
Python runtime is unavailable. It is ADVISORY (like seo_check): issues feed the
fix-loop; it never flips the verdict.

The ``mcp`` package is NOT a test dependency, so these fixtures speak RAW
newline-delimited JSON-RPC over stdio WITHOUT the SDK — the gate speaks the
protocol, not the SDK, so it exercises every path on any machine. (The real
scaffold uses the SDK; the gate soft-skips when the SDK can't boot — see the
missing-lib fixture.)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from skyn3t.studio.mcp_check import McpVerdict, check_mcp

# ── raw JSON-RPC stdio fixture servers (no mcp SDK) ────────────────────────

# A well-behaved server: 2 tools with valid JSON Schemas; valid args succeed;
# a call missing a required param returns an isError result (not a crash).
_GOOD_SERVER = r'''
import sys, json

TOOLS = [
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "shout", "description": "Uppercase some text.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
]

def _result(mid, content, is_error=False):
    return {"jsonrpc": "2.0", "id": mid,
            "result": {"content": [{"type": "text", "text": content}], "isError": is_error}}

def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "good-fixture", "version": "0.1.0"}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "add":
            if "a" not in args or "b" not in args:
                return _result(mid, "missing required parameter", True)
            return _result(mid, str(args["a"] + args["b"]))
        if name == "shout":
            if "text" not in args:
                return _result(mid, "missing text", True)
            return _result(mid, str(args["text"]).upper())
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "unknown tool " + str(name)}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return None

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
'''

# A server whose tool ALWAYS errors, even on valid input (simulates a tool that
# raises and is wrapped as an isError result by the SDK).
_RAISING_SERVER = r'''
import sys, json

TOOLS = [
    {"name": "boom", "description": "Always raises.",
     "inputSchema": {"type": "object",
                     "properties": {"x": {"type": "string"}}, "required": ["x"]}},
]

def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "raising-fixture", "version": "0.1.0"}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": "Tool raised: boom"}],
                           "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}}
    return None

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
'''

# A server that reads a request but NEVER answers — a wedged/hanging server.
_HANGING_SERVER = r'''
import sys, time

def main():
    for line in sys.stdin:
        # Received a request but never respond — the gate must time out, not hang.
        time.sleep(3600)

if __name__ == "__main__":
    main()
'''

# A server that fails to boot because the mcp SDK is missing (the real scaffold's
# failure mode when `mcp` isn't installed). Reproduces the exact boot stderr.
_MISSING_LIB_SERVER = (
    "import sys\n"
    "sys.stderr.write('Traceback (most recent call last):\\n')\n"
    "sys.stderr.write('  File \"server.py\", line 3, in <module>\\n')\n"
    "sys.stderr.write('    from mcp.server.fastmcp import FastMCP\\n')\n"
    "sys.stderr.write(\"ModuleNotFoundError: No module named 'mcp'\\n\")\n"
    "sys.exit(1)\n"
)


# The real scaffold's shape: server.py imports a SIBLING module for its logic
# (`import tools`). Regression for the `python -I` gotcha — since 3.11 isolated
# mode implies -P (safe path), which drops the script dir from sys.path, so the
# sibling import crashed at boot and the gate filed a FALSE "crashed on boot"
# issue whenever the SDK was importable. The gate must boot split-file servers.
_SIBLING_HELPERS = "def upper_text(text):\n    return str(text).upper()\n"
_SIBLING_IMPORT_SERVER = _GOOD_SERVER.replace(
    "import sys, json\n", "import sys, json\nimport helpers\n", 1,
).replace('str(args["text"]).upper()', 'helpers.upper_text(args["text"])')


def _write_server(tmp_path: Path, source: str) -> Path:
    (tmp_path / "server.py").write_text(source, encoding="utf-8")
    return tmp_path


# ── tests ──────────────────────────────────────────────────────────────────
def test_good_server_passes(tmp_path):
    _write_server(tmp_path, _GOOD_SERVER)
    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert isinstance(v, McpVerdict)
    assert not v.skipped, v.to_dict()
    assert v.ok, v.to_dict()
    assert v.issues == []
    assert set(v.tools) == {"add", "shout"}
    assert v.gaps() == []


def test_server_importing_a_sibling_module_boots(tmp_path):
    # The scaffold's own layout (`server.py` + `import tools`) must be provable:
    # the spawn must keep the script's directory on sys.path (no `-I`).
    _write_server(tmp_path, _SIBLING_IMPORT_SERVER)
    (tmp_path / "helpers.py").write_text(_SIBLING_HELPERS, encoding="utf-8")
    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert not v.skipped, v.to_dict()
    assert v.ok, v.to_dict()
    assert set(v.tools) == {"add", "shout"}


def test_raising_tool_is_an_issue(tmp_path):
    _write_server(tmp_path, _RAISING_SERVER)
    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert not v.skipped, v.to_dict()
    assert not v.ok, v.to_dict()
    assert v.issues, "a tool that errors on valid input must be recorded as an issue"
    assert any("boom" in i for i in v.issues), v.issues
    # Issues surface to the fix-loop as gaps.
    assert v.gaps()


def test_hanging_server_times_out_without_hanging_the_build(tmp_path):
    _write_server(tmp_path, _HANGING_SERVER)
    start = time.monotonic()
    v = check_mcp(
        tmp_path, stack="mcp", python_exec=sys.executable,
        handshake_timeout=1.5, list_timeout=1.5, call_timeout=1.5,
    )
    elapsed = time.monotonic() - start
    # Bounded: the gate returned promptly instead of hanging on the wedged server.
    assert elapsed < 20, f"gate took {elapsed:.1f}s — it must never hang a build"
    assert not v.skipped, v.to_dict()
    assert not v.ok, v.to_dict()
    assert any("timeout" in i.lower() or "did not respond" in i.lower() for i in v.issues), v.issues


def test_missing_mcp_lib_soft_skips(tmp_path):
    _write_server(tmp_path, _MISSING_LIB_SERVER)
    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    # Could-not-run (SDK absent) → degrade open: skipped, ok=False, NO gaps/issues.
    assert v.skipped, v.to_dict()
    assert not v.ok
    assert v.gaps() == []
    assert "mcp" in v.reason.lower()


def test_non_mcp_stack_soft_skips(tmp_path):
    _write_server(tmp_path, _GOOD_SERVER)
    v = check_mcp(tmp_path, stack="fastapi", python_exec=sys.executable)
    assert v.skipped
    assert v.gaps() == []


def test_no_server_file_soft_skips(tmp_path):
    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert v.skipped
    assert v.gaps() == []


def test_check_mcp_never_raises_on_garbage(tmp_path):
    # A non-directory / bogus input must degrade to a skip, never raise.
    v = check_mcp(tmp_path / "does-not-exist", stack="mcp")
    assert isinstance(v, McpVerdict)
    assert v.skipped


def test_mcp_check_does_not_pass_host_secrets_to_server(tmp_path, monkeypatch):
    import subprocess

    captured = {}
    _write_server(tmp_path, _GOOD_SERVER)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-host-secret")

    def fake_popen(*args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        raise OSError("stop after capture")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)

    assert v.skipped
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["env"]["PYTHONUNBUFFERED"] == "1"
