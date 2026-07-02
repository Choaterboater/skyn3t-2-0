"""Deterministic MCP-server gate (wave-2 §3.3) — the proof story for the ``mcp``
app type, with ZERO LLM anywhere.

An MCP server is a stdio JSON-RPC program: an AI client (Claude Desktop, Cursor,
…) spawns it, performs an ``initialize`` handshake, lists its tools, and calls
them. This gate is that client, mechanically: it spawns the delivered
``server.py`` and drives the real Model Context Protocol over stdin/stdout —

  initialize  →  notifications/initialized  →  tools/list  →  tools/call (each
  tool, with schema-derived fixture args)  →  one malformed call (missing a
  required param).

It asserts: the handshake completes; ``tools/list`` is non-empty with valid JSON
Schema objects; each tool returns a well-formed, non-error result on valid input;
and a malformed call yields a structured error (JSON-RPC ``error`` OR an
``isError`` result), NOT a crash.

Design contract — mirrors :mod:`skyn3t.studio.seo_check`:

* **Never hangs a build.** Every stdout read is time-bounded (a background reader
  thread + a deadline-bounded queue). A wedged server → a timeout ISSUE, never a
  blocked build.
* **Never raises.** Any unexpected failure degrades to a soft-skip.
* **Soft-skips (degrade open, no gaps) when it CANNOT run the check** — a non-mcp
  stack, no ``server.py``, no Python runtime, or the ``mcp`` SDK not importable
  (the delivered server crashes on boot with a ``No module named 'mcp'``). This
  is the deliberate split the wave-2 brief calls for: the scaffold's runtime dep
  is declared, and the gate soft-skips if it can't boot.
* **Records ISSUES (ok=False, gaps feed the fix-loop) when it RAN and found a real
  defect** — a broken handshake, an empty tool list, an invalid schema, a tool
  that errors on valid input, or a crash/hang. ADVISORY: the runner records the
  verdict and feeds gaps to ONE repair, but NEVER flips the build's verdict.

The gate speaks the PROTOCOL, not the SDK — so any MCP-compliant stdio server
(FastMCP, the low-level ``Server`` API, or a hand-rolled JSON-RPC loop) is
provable, and the gate's own tests can use SDK-free fixture servers.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default per-phase read budgets (seconds). Generous enough for a real SDK boot,
# bounded so a wedged server can never hang a build. Overridable (the tests pass
# small values for the hang case).
_HANDSHAKE_TIMEOUT = 15.0
_LIST_TIMEOUT = 12.0
_CALL_TIMEOUT = 12.0
_BOOT_GRACE = 6.0  # wait for a boot-crash to surface on stderr before classifying

_MAX_TOOLS = 24  # bound the tools we exercise so a pathological list can't stall
_PROTOCOL_VERSION = "2024-11-05"

# The delivered server crashed on boot because the mcp SDK is not importable —
# a degrade-open SKIP signal, not a code defect (its dep is declared in
# requirements.txt; the proof env just doesn't have it installed).
_MCP_MISSING_RE = re.compile(
    r"No module named ['\"]mcp['\"]"
    r"|(?:ModuleNotFoundError|ImportError)[^\n]*\bmcp\b",
    re.I,
)


@dataclass(slots=True)
class McpVerdict:
    """The outcome of the deterministic MCP gate.

    ``ok`` is a property (not a gate): True when the gate RAN and found no issues.
    ``skipped`` marks a degrade-open (non-mcp stack, no server, no runtime, or the
    SDK not importable): a skipped verdict is ``ok=False`` yet produces NO gaps, so
    it can never false-flag a build. Advisory only — the runner never flips the
    verdict from this.
    """

    skipped: bool = False
    issues: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    checked: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return (not self.skipped) and (not self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "issues": list(self.issues),
            "tools": list(self.tools),
            "checked": dict(self.checked),
            "reason": self.reason,
            "gaps": self.gaps(),
        }

    def gaps(self) -> list[str]:
        """Actionable repair strings for the improve loop. Empty when the verdict is
        ok or a soft-skip (a could-not-run gate must never false-flag). The issues
        are authored to be directly actionable, so they ARE the gaps."""
        if self.skipped or self.ok:
            return []
        return list(self.issues)


# ── internal control-flow signals (never escape check_mcp) ──────────────────
class _Timeout(Exception):
    pass


class _ServerExited(Exception):
    pass


class _StdioError(Exception):
    pass


class _StdioClient:
    """A minimal newline-delimited JSON-RPC 2.0 client over a subprocess's stdio.

    MCP's stdio transport frames each message as one line of UTF-8 JSON. Reads are
    served from a background thread through a queue so every wait is deadline-bounded
    (the gate can never block on a wedged server)."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._out_q: queue.Queue[str | None] = queue.Queue()
        self._err_lines: list[str] = []
        self._id = 0
        self._t_out = threading.Thread(target=self._pump_stdout, daemon=True)
        self._t_err = threading.Thread(target=self._pump_stderr, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _pump_stdout(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self._out_q.put(line)
        except Exception:  # noqa: BLE001 - a read failure just ends the stream
            pass
        finally:
            self._out_q.put(None)  # EOF sentinel

    def _pump_stderr(self) -> None:
        try:
            assert self.proc.stderr is not None
            for line in self.proc.stderr:
                self._err_lines.append(line)
        except Exception:  # noqa: BLE001
            pass

    def stderr_text(self) -> str:
        return "".join(self._err_lines)

    def wait_exit(self, timeout: float) -> None:
        """Best-effort: let a crashed process finish + its stderr flush, so the
        exit can be classified (mcp-missing vs a genuine crash)."""
        try:
            self.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        self._t_err.join(timeout=2.0)

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def send(self, method: str, params: dict | None = None, *, notification: bool = False) -> int | None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        rid: int | None = None
        if not notification:
            rid = self._next_id()
            msg["id"] = rid
        data = json.dumps(msg) + "\n"
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            # The server closed stdin / exited — treat as a server exit so the
            # caller can classify the boot failure.
            raise _ServerExited("server stdin is closed (process exited)")
        return rid

    def read_result(self, rid: int, timeout: float) -> dict:
        """Read messages until the response with ``id == rid`` arrives, or the
        deadline passes. Skips notifications, other ids, and non-JSON log lines.
        Raises :class:`_Timeout` / :class:`_ServerExited`."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Timeout(f"no response to id={rid} within {timeout:.0f}s")
            try:
                line = self._out_q.get(timeout=remaining)
            except queue.Empty:
                raise _Timeout(f"no response to id={rid} within {timeout:.0f}s")
            if line is None:  # EOF — the server's stdout closed
                raise _ServerExited("server closed stdout (process exited)")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001 - tolerate a stray non-JSON log line
                continue
            if isinstance(msg, dict) and msg.get("id") == rid:
                return msg
            # A notification or a different id — keep reading.


def _python_exec(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    # Prefer the interpreter running the gate (its installed packages match the
    # proof env); fall back to a PATH python.
    return sys.executable or shutil.which("python") or shutil.which("python3")


def _is_valid_schema(schema: Any) -> bool:
    """A tool ``inputSchema`` must be a JSON Schema OBJECT (``type: object`` with a
    dict ``properties`` when present) — what an MCP client needs to build args."""
    if not isinstance(schema, dict):
        return False
    t = schema.get("type")
    if t is not None and t != "object":
        return False
    props = schema.get("properties")
    return props is None or isinstance(props, dict)


def _fixture_value(spec: Any) -> Any:
    """A schema-typed fixture value: strings→"test", numbers→1, bools→true, etc."""
    if isinstance(spec, dict):
        if "default" in spec:
            return spec["default"]
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        t = spec.get("type")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), "string")
    else:
        t = None
    if t in ("number", "integer"):
        return 1
    if t == "boolean":
        return True
    if t == "array":
        return []
    if t == "object":
        return {}
    return "test"


def _fixture_args(schema: dict) -> dict:
    """Build a minimal valid ``arguments`` dict from a tool's inputSchema — only
    the REQUIRED params (the smallest well-formed call)."""
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return {name: _fixture_value(props.get(name, {})) for name in required}


def _call_ok(resp: dict) -> bool:
    """A well-formed, NON-error tool result: no JSON-RPC error, a ``result`` object
    with a ``content`` list, and ``isError`` not true."""
    if not isinstance(resp, dict) or resp.get("error") is not None:
        return False
    result = resp.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        return False
    return isinstance(result.get("content"), list)


def _is_error_response(resp: dict) -> bool:
    """A structured error: a JSON-RPC ``error`` OR an ``isError`` tool result."""
    if not isinstance(resp, dict):
        return False
    if resp.get("error") is not None:
        return True
    result = resp.get("result")
    return isinstance(result, dict) and result.get("isError") is True


def _err_detail(resp: dict) -> str:
    if isinstance(resp, dict):
        if resp.get("error") is not None:
            return str(resp["error"])[:200]
        result = resp.get("result")
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and first.get("text"):
                    return str(first["text"])[:200]
    return "no result content"


def _tail(text: str, n: int = 300) -> str:
    return (text or "").strip()[-n:]


def check_mcp(
    project_dir: str | Path,
    stack: str = "",
    *,
    python_exec: str | None = None,
    server_rel: str = "server.py",
    handshake_timeout: float = _HANDSHAKE_TIMEOUT,
    list_timeout: float = _LIST_TIMEOUT,
    call_timeout: float = _CALL_TIMEOUT,
) -> McpVerdict:
    """Drive the delivered MCP server over stdio and return an advisory
    :class:`McpVerdict`. Never raises; never hangs a build (all reads are
    time-bounded); soft-skips when it cannot run (see the module docstring)."""
    try:
        s = (stack or "").strip().lower()
        if s and s != "mcp":
            return McpVerdict(skipped=True, reason=f"stack '{stack}' is not an mcp stack")
        root = Path(project_dir)
        if not root.is_dir():
            return McpVerdict(skipped=True, reason="project dir is not a directory")
        server = root / server_rel
        if not server.is_file():
            return McpVerdict(skipped=True, reason=f"no {server_rel} to run")
        py = _python_exec(python_exec)
        if not py:
            return McpVerdict(skipped=True, reason="no python interpreter available")
        return _probe_server(
            root, server.name, py,
            handshake_timeout=handshake_timeout,
            list_timeout=list_timeout,
            call_timeout=call_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - a gate must never break a build
        return McpVerdict(skipped=True, reason=f"mcp check error: {exc}")


def _probe_server(
    root: Path,
    server_name: str,
    py: str,
    *,
    handshake_timeout: float,
    list_timeout: float,
    call_timeout: float,
) -> McpVerdict:
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        # NOT `-I`: since Python 3.11 isolated mode implies -P (safe path), which
        # drops the script's directory from sys.path — the scaffold's own
        # `import tools` would crash at boot and the gate would file a FALSE
        # "crashed on boot" issue whenever the mcp SDK is installed. `-B` keeps
        # the proof from polluting the artifact with __pycache__ instead.
        proc = subprocess.Popen(
            [py, "-B", server_name],
            cwd=str(root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001
        return McpVerdict(skipped=True, reason=f"could not spawn server: {exc}")

    client = _StdioClient(proc)
    checked: dict[str, Any] = {}
    try:
        # 1. initialize handshake.
        try:
            rid = client.send("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "skyn3t-mcp-check", "version": "1.0"},
            })
            init = client.read_result(rid, handshake_timeout)  # type: ignore[arg-type]
        except _ServerExited:
            client.wait_exit(_BOOT_GRACE)
            err = client.stderr_text()
            if _MCP_MISSING_RE.search(err):
                return McpVerdict(skipped=True,
                                  reason="mcp SDK not importable — gate soft-skipped (declared runtime dep)")
            return McpVerdict(
                issues=[f"server exited during startup without completing the MCP "
                        f"handshake (it crashed on boot): {_tail(err)}"],
                checked={"booted": False},
            )
        except _Timeout:
            return McpVerdict(
                issues=[f"server did not respond to the MCP initialize handshake within "
                        f"{handshake_timeout:.0f}s — it may hang or block on stdin"],
                checked={"booted": False, "handshake": "timeout"},
            )

        if not isinstance(init, dict) or init.get("error") is not None or "result" not in init:
            return McpVerdict(
                issues=[f"initialize returned an error/invalid response: {init.get('error')}"],
                checked={"handshake": "error"},
            )
        info = (init.get("result") or {}).get("serverInfo") or {}
        checked = {"booted": True, "handshake": "ok", "server_name": info.get("name", "")}

        # 2. initialized notification (required before the server serves requests).
        try:
            client.send("notifications/initialized", notification=True)
        except _ServerExited:
            return McpVerdict(
                issues=["server exited right after the handshake (before it could list tools)"],
                checked=checked,
            )

        # 3. tools/list.
        try:
            rid = client.send("tools/list", {})
            listed = client.read_result(rid, list_timeout)  # type: ignore[arg-type]
        except _Timeout:
            checked["tools_list"] = "timeout"
            return McpVerdict(issues=[f"tools/list did not respond within {list_timeout:.0f}s"], checked=checked)
        except _ServerExited:
            return McpVerdict(issues=["server exited during tools/list"], checked=checked)

        if not isinstance(listed, dict) or "result" not in listed:
            return McpVerdict(
                issues=[f"tools/list returned an error: {listed.get('error')}"],
                checked=checked,
            )
        tools = (listed.get("result") or {}).get("tools")
        if not isinstance(tools, list) or not tools:
            return McpVerdict(
                issues=["the server exposed NO tools (tools/list was empty) — an MCP "
                        "server must register at least one tool with a name + description"],
                checked=checked,
            )

        # 4. validate each tool's schema + call it with valid fixture args.
        issues: list[str] = []
        tool_names: list[str] = []
        first_required: tuple[str, dict] | None = None
        for entry in tools[:_MAX_TOOLS]:
            if not isinstance(entry, dict):
                issues.append("tools/list contains a non-object tool entry")
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                issues.append("a tool is missing a non-empty name")
                continue
            tool_names.append(name)
            schema = entry.get("inputSchema")
            if not _is_valid_schema(schema):
                issues.append(f"tool '{name}' has an invalid/missing inputSchema "
                              "(it must be a JSON Schema object with typed properties)")
                continue
            desc = entry.get("description")
            if not (isinstance(desc, str) and desc.strip()):
                issues.append(f"tool '{name}' has no description — MCP clients select "
                              "tools by name + description, so every tool needs one")
            required = schema.get("required")
            if isinstance(required, list) and required and first_required is None:
                first_required = (name, schema)

            try:
                rid = client.send("tools/call", {"name": name, "arguments": _fixture_args(schema)})
                resp = client.read_result(rid, call_timeout)  # type: ignore[arg-type]
            except _Timeout:
                issues.append(f"tool '{name}' did not return within {call_timeout:.0f}s on "
                              "valid input — it may hang")
                break  # a wedged server won't recover; stop compounding timeouts
            except _ServerExited:
                issues.append(f"server exited while calling tool '{name}' — it crashed "
                              "instead of returning a result")
                break
            if not _call_ok(resp):
                issues.append(f"tool '{name}' failed on valid fixture input "
                              f"(expected a well-formed result): {_err_detail(resp)}")

        # 5. one malformed call — a required-param-omitted call must yield a
        # structured error, not a crash.
        if first_required is not None and proc.poll() is None:
            name, _schema = first_required
            try:
                rid = client.send("tools/call", {"name": name, "arguments": {}})
                resp = client.read_result(rid, call_timeout)  # type: ignore[arg-type]
            except _Timeout:
                issues.append(f"tool '{name}' hung on a malformed call (missing required "
                              "params) instead of returning an error")
            except _ServerExited:
                issues.append("server crashed on a malformed tool call (missing required "
                              "params) instead of returning a JSON-RPC error")
            else:
                handled = _is_error_response(resp)
                checked["malformed_call_handled"] = handled
                if not handled:
                    issues.append(f"tool '{name}' did NOT reject a call with missing required "
                                  "parameters (no JSON-RPC error and isError!=true) — it must "
                                  "validate its inputs")

        checked["tools"] = tool_names
        checked["tool_count"] = len(tool_names)
        return McpVerdict(issues=issues, tools=tool_names, checked=checked)
    finally:
        _shutdown(proc)


def _shutdown(proc: subprocess.Popen) -> None:
    """Tear the server down — close stdin, then terminate, then kill. Never raises."""
    for closer in (lambda: proc.stdin and proc.stdin.close(),):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.terminate()
        proc.wait(timeout=2.0)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
