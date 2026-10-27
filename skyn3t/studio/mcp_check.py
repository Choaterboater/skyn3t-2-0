"""Deterministic MCP-server gate (wave-2 §3.3) — the proof story for the ``mcp``
app type, with ZERO LLM anywhere.

An MCP server is a stdio JSON-RPC program: an AI client (Claude Desktop, Cursor,
…) spawns it, performs an ``initialize`` handshake, lists its tools, and calls
them. This gate is that client, mechanically: it spawns the delivered
``server.py`` and drives the real Model Context Protocol over stdin/stdout —

  initialize  →  notifications/initialized  →  tools/list  →  tools/call (each
  tool, with schema-derived fixture args)  →  one malformed call (missing a
  required param).

For local-only servers it asserts: the handshake completes; ``tools/list`` is non-empty with valid JSON
Schema objects; each tool returns a well-formed, non-error result on valid input;
and a malformed call yields a structured error (JSON-RPC ``error`` OR an
``isError`` result), NOT a crash.

External HTTP integrations instead run with diagnostic credentials and injected
401, 503 and timeout responses. They must report upstream failure, not invented
success. Unsupported/unexercised clients remain unverified. This is not proof of
successful vendor operations, documented endpoint correctness, or full workflow
acceptance.

Design contract — mirrors :mod:`skyn3t.studio.seo_check`:

* **Never hangs a build.** Every stdout read is time-bounded (a background reader
  thread + a deadline-bounded queue). A wedged server → a timeout ISSUE, never a
  blocked build.
* **Never raises.** Any unexpected failure records unavailable verification.
* **Distinguishes not applicable from unavailable.** A non-mcp stack is skipped;
  a missing server, Python runtime, or SDK means verification is unavailable,
  not successful. Unavailable checks remain skipped with no code-repair gaps:
  an environment failure must not trigger speculative implementation repairs.
* **Records ISSUES (ok=False, gaps feed the fix-loop) when it RAN and found a real
  defect** — a broken handshake, an empty tool list, an invalid schema, a tool
  that errors on valid input, or a crash/hang. The runner owns release policy and
  must not count unavailable verification as a pass.

The gate speaks the PROTOCOL, not the SDK — so any MCP-compliant stdio server
(FastMCP, the low-level ``Server`` API, or a hand-rolled JSON-RPC loop) is
provable, and the gate's own tests can use SDK-free fixture servers.
"""

from __future__ import annotations

import ast
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TypeGuard

from skyn3t.security.secrets import filter_env, mask_secrets
from skyn3t.worktree import source_tree_snapshot

# Default per-phase read budgets (seconds). Generous enough for a real SDK boot,
# bounded so a wedged server can never hang a build. Overridable (the tests pass
# small values for the hang case).
_HANDSHAKE_TIMEOUT = 15.0
_LIST_TIMEOUT = 12.0
_CALL_TIMEOUT = 12.0
_BOOT_GRACE = 6.0  # wait for a boot-crash to surface on stderr before classifying

_MAX_TOOLS = 24  # bound the tools we exercise so a pathological list can't stall
_PROTOCOL_VERSION = "2024-11-05"
_HTTP_FAULTS = ("HTTP 401", "HTTP 503", "timeout")
_HTTP_IMPORT = re.compile(
    r"^\s*(?:import\s+[^#\n;]*\b(?:httpx|requests|aiohttp|urllib\.request)\b|"
    r"from\s+(?:httpx|requests|aiohttp|urllib\.request)\b|"
    r"from\s+urllib\s+import\s+.*\brequest\b)", re.M,
)
_CREDENTIAL_NAME = re.compile(r"(?:api_?key|token|password|secret)", re.I)
_PROBE_ID = "00000000-0000-4000-8000-000000000001"

# The delivered server cannot boot without its SDK: verification is unavailable,
# not a protocol pass or evidence of an implementation defect.
_MCP_MISSING_RE = re.compile(
    r"No module named ['\"]mcp['\"]",
    re.I,
)


@dataclass(slots=True)
class McpVerdict:
    """The outcome of the deterministic MCP gate.

    ``ok`` means the applicable protocol/failure probes ran without issues, not
    that the tools satisfy the product brief. ``unavailable`` distinguishes checks that could
    not run from not-applicable skips. Both remain ``ok=False`` with no repair
    gaps; the runner must withhold verified status when evidence is unavailable.
    """

    skipped: bool = False
    issues: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    checked: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    unavailable: bool = False

    @property
    def ok(self) -> bool:
        return (not self.skipped) and (not self.unavailable) and (not self.issues)

    @property
    def status(self) -> str:
        if self.unavailable:
            return "unavailable"
        if self.skipped:
            return "skipped"
        return "pass" if self.ok else "fail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "unavailable": self.unavailable,
            "status": self.status,
            "issues": list(self.issues),
            "tools": list(self.tools),
            "checked": dict(self.checked),
            "reason": self.reason,
            "gaps": self.gaps(),
        }

    def gaps(self) -> list[str]:
        """Actionable code-repair strings; absent evidence is not a code defect."""
        if self.skipped or self.unavailable or self.ok:
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
        except (BrokenPipeError, ValueError, OSError) as exc:
            # The server closed stdin / exited — treat as a server exit so the
            # caller can classify the boot failure.
            raise _ServerExited("server stdin is closed (process exited)") from exc
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
            except queue.Empty as exc:
                raise _Timeout(f"no response to id={rid} within {timeout:.0f}s") from exc
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


def _is_valid_schema(schema: Any) -> TypeGuard[dict[str, Any]]:
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


def _fixture_args(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal valid ``arguments`` dict from a tool's inputSchema — only
    the REQUIRED params (the smallest well-formed call)."""
    props_value = schema.get("properties")
    props = props_value if isinstance(props_value, dict) else {}
    required_value = schema.get("required")
    required = (
        [name for name in required_value if isinstance(name, str)]
        if isinstance(required_value, list)
        else []
    )
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


def _mask(text: str) -> str:
    """Output-side secret masking for gate output. Never breaks a build."""
    try:
        return mask_secrets(text)
    except Exception:  # noqa: BLE001
        return text


def _mask_value(value: Any) -> Any:
    """Mask strings (and string items of lists) inside a ``checked`` dict."""
    if isinstance(value, str):
        return _mask(value)
    if isinstance(value, list):
        return [_mask_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _mask_value(item) for key, item in value.items()}
    return value


def _mask_verdict(verdict: McpVerdict) -> McpVerdict:
    """Scrub registered host secrets from gate output BEFORE it is recorded.

    The delivered server is untrusted: anything it prints (boot-crash stderr
    tails, tool error text, server/tool names) lands in issues/checked, and
    from there in build manifests and the fix-loop's gaps. ``filter_env``
    keeps host keys out of the server's environment; this is the output-side
    complement — a host credential that reaches that text by ANY channel
    comes back masked.
    """
    try:
        verdict.issues = [_mask(issue) for issue in verdict.issues]
        verdict.tools = [_mask(name) for name in verdict.tools]
        verdict.reason = _mask(verdict.reason)
        verdict.checked = {key: _mask_value(value) for key, value in verdict.checked.items()}
    except Exception:  # noqa: BLE001 - masking must never break a build
        pass
    return verdict


def _portability_issues(root: Path, files: list[str]) -> list[str]:
    issues = []
    for relative in files:
        path = Path(relative)
        if path.suffix != ".py" or "tests" in path.parts or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            issues.append(f"Cannot inspect runtime dependency portability: {relative}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                parts = set(node.value.replace("\\", "/").split("/"))
                if {"site-packages", "dist-packages"} & parts and (
                    PurePosixPath(node.value).is_absolute() or PureWindowsPath(node.value).anchor
                ):
                    issues.append(f"{relative} embeds a machine-specific dependency path")
            elif (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getusersitepackages"
            ):
                issues.append(f"{relative} consults external user-site dependencies")
    return list(dict.fromkeys(issues))


def check_mcp(
    project_dir: str | Path,
    stack: str = "",
    *,
    python_exec: str | None = None,
    server_rel: str = "server.py",
    handshake_timeout: float = _HANDSHAKE_TIMEOUT,
    list_timeout: float = _LIST_TIMEOUT,
    call_timeout: float = _CALL_TIMEOUT,
    brief: str = "",
    capability_profile: str | None = None,
) -> McpVerdict:
    """Check stdio plus bounded upstream failures for HTTP integrations.

    Failure injection does not establish endpoint correctness or successful
    business behavior. Local-only servers retain the protocol-only check.
    """
    try:
        s = (stack or "").strip().lower()
        if s and s != "mcp":
            return McpVerdict(skipped=True, reason=f"stack '{stack}' is not an mcp stack")
        root = Path(project_dir)
        if not root.is_dir():
            return McpVerdict(skipped=True, unavailable=True, reason="project dir is not a directory")
        server = root / server_rel
        if not server.is_file():
            return McpVerdict(skipped=True, unavailable=True, reason=f"no {server_rel} to run")
        py = _python_exec(python_exec)
        if not py:
            return McpVerdict(skipped=True, unavailable=True, reason="no python interpreter available")
        source = source_tree_snapshot(root)
        if not source.get("valid"):
            return McpVerdict(unavailable=True, reason="Cannot bind MCP verification to authored source")
        portability = _portability_issues(root, source["files"])
        if portability:
            return McpVerdict(issues=portability, checked={"runtime_portability": "failed"})
        requires_http, keys = _http_requirements(root, brief)
        if not requires_http:
            return _with_capability_evidence(root, py, _probe_server(
                root, server.name, py, handshake_timeout=handshake_timeout,
                list_timeout=list_timeout, call_timeout=call_timeout,
            ), capability_profile)
        with tempfile.TemporaryDirectory(prefix="skyn3t-mcp-http-") as temporary:
            scratch = Path(temporary)
            verdicts = []
            for index, fault in enumerate(_HTTP_FAULTS):
                case_root = scratch / str(index)
                case_root.mkdir()
                verdicts.append(_probe_server(
                    root, server.name, py, handshake_timeout=handshake_timeout,
                    list_timeout=list_timeout, call_timeout=call_timeout,
                    fault=fault, receipt=case_root / "requests.jsonl",
                    probe_environment=_probe_environment(keys, case_root),
                ))
            startup_root = scratch / "startup"
            startup_root.mkdir()
            startup_environment = _probe_environment(keys, startup_root)
            for key in keys:
                if _CREDENTIAL_NAME.search(key):
                    startup_environment[key] = ""
            startup = _probe_server(
                root, server.name, py, handshake_timeout=handshake_timeout,
                list_timeout=list_timeout, call_timeout=call_timeout,
                fault="HTTP 401", receipt=startup_root / "requests.jsonl",
                probe_environment=startup_environment, list_only=True,
            )
            verdicts.append(startup)
        unavailable_reason = next(
            (result.reason for result in verdicts if result.unavailable and result.reason), "",
        )
        verdict = verdicts[0]
        cases = [
            case for result in verdicts for case in result.checked.get("http_cases", [])
        ]
        exercised = sorted({case["tool"] for case in cases if case["requests"] > 0})
        verdict.issues = list(dict.fromkeys(issue for result in verdicts for issue in result.issues))
        verdict.unavailable = any(result.unavailable for result in verdicts)
        verdict.checked.pop("http_cases", None)
        verdict.checked["verification_scope"] = "protocol_and_http_failure_handling"
        verdict.checked["startup_without_credentials"] = startup.to_dict()
        verdict.checked["http_failure_handling"] = {
            "exercised_tools": exercised,
            "cases": cases,
            "network": "supported HTTP clients replaced; Python socket/process operations blocked",
        }
        exercised_faults = {case["fault"] for case in cases if case["requests"] > 0}
        if verdict.unavailable:
            verdict.reason = unavailable_reason
        elif exercised_faults != set(_HTTP_FAULTS) and not verdict.issues:
            verdict.unavailable = True
            verdict.reason = (
                "External API failure handling not exercised: no supported HTTP request "
                "was reached with diagnostic inputs; provide testable client configuration."
            )
        return _with_capability_evidence(root, py, verdict, capability_profile)
    except Exception as exc:  # noqa: BLE001 - a gate must never break a build
        return McpVerdict(skipped=True, unavailable=True, reason=_mask(f"mcp check error: {exc}"))


def _with_capability_evidence(
    root: Path, py: str, verdict: McpVerdict, capability_profile: str | None = None,
) -> McpVerdict:
    from skyn3t.studio.mcp_capabilities import check_mcp_capabilities

    coverage = check_mcp_capabilities(
        root, python_exec=py, tools=verdict.tools, capability_profile=capability_profile,
    )
    if coverage is not None:
        verdict.checked["capability_coverage"] = coverage
        for capability, case in coverage["cases"].items():
            if case["status"] != "passed":
                verdict.issues.append(f"MCP capability {capability}: {case['reason']}")
    return _mask_verdict(verdict)


def _http_requirements(root: Path, brief: str) -> tuple[bool, set[str]]:
    from skyn3t.worktree import SOURCE_TREE_EXCLUDED_DIR_NAMES

    required = bool(re.search(r"\bAPI\b.*https?://", brief, re.I | re.S))
    keys: set[str] = set()
    example = root / ".env.example"
    if example.is_file() and not example.is_symlink():
        keys.update(re.findall(
            r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=",
            example.read_text(encoding="utf-8"), re.M,
        ))
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            name for name in dirs
            if name.casefold() not in SOURCE_TREE_EXCLUDED_DIR_NAMES
            and name not in {"tests", "test"} and not name.startswith(".")
        ]
        for name in names:
            path = Path(directory) / name
            if not name.endswith(".py") or name.startswith("test_") or path.is_symlink():
                continue
            text = path.read_text(encoding="utf-8")
            required = required or bool(_HTTP_IMPORT.search(text))
            keys.update(re.findall(
                r"(?:getenv|environ\.get)\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]", text,
            ))
    return required or any(_CREDENTIAL_NAME.search(key) for key in keys), keys


def _probe_environment(keys: set[str], scratch: Path) -> dict[str, str]:
    home = scratch / "home"
    home.mkdir()
    environment: dict[str, str] = {
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
    }
    for key in keys:
        low = key.lower()
        if _CREDENTIAL_NAME.search(key):
            environment[key] = "skyn3t-diagnostic-not-a-real-credential"
        elif low.endswith(("_org_id", "_site_id", "_device_id")):
            environment[key] = _PROBE_ID
        elif low.endswith(("_db_path", "_database_path")):
            environment[key] = str(scratch / "state.db")
        elif any(part in low for part in ("offline", "simulation", "demo_mode", "dry_run")):
            environment[key] = "false"
    return environment


def _http_fixture_args(schema: dict[str, Any]) -> dict[str, Any]:
    args = _fixture_args(schema)
    end = int(time.time()) - 60
    for name, spec in (schema.get("properties") or {}).items():
        low = name.lower()
        if _CREDENTIAL_NAME.search(name):
            args[name] = "skyn3t-diagnostic-not-a-real-credential"
        elif (
            low in {"base_url", "api_url", "endpoint"} and name in args
            and not (isinstance(spec, dict) and (spec.get("default") or spec.get("enum")))
        ):
            args[name] = "https://skyn3t-probe.invalid"
        elif low in {"org_id", "site_id", "device_id"}:
            args[name] = _PROBE_ID
        elif (
            low in {"start", "end", "start_time", "end_time", "since", "until"}
            and isinstance(spec, dict)
            and (
                spec.get("type") in ("integer", "number")
                or any(
                    isinstance(option, dict) and option.get("type") in ("integer", "number")
                    for option in (spec.get("anyOf") or [])
                )
            )
        ):
            args[name] = end if low in {"end", "end_time", "until"} else end - 900
        elif low in {"offline", "offline_mode", "simulate", "simulation", "dry_run"}:
            args[name] = False
        elif (
            low in {"approved", "confirm", "confirmed"}
            and isinstance(spec, dict) and spec.get("type") == "boolean"
        ):
            args[name] = True
    return args


def _failure_reported(response: dict[str, Any]) -> bool:
    failed = _is_error_response(response)
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return failed
    structured = result.get("structuredContent")
    payloads = [structured]
    if isinstance(structured, dict) and isinstance(structured.get("result"), dict):
        payloads.append(structured["result"])
    for content in result.get("content") or []:
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            try:
                payloads.append(json.loads(content["text"]))
            except json.JSONDecodeError:
                continue
    for value in payloads:
        if not isinstance(value, dict):
            continue
        status = str(value.get("status") or value.get("action_status") or "").lower()
        if (
            value.get("success") is True or value.get("ok") is True
            or status in {"completed", "success", "succeeded"}
        ):
            return False
        failed = failed or (
            value.get("success") is False or value.get("ok") is False
            or bool(value.get("error")) or status in {"error", "failed", "unauthorized"}
        )
    return failed


def _probe_events(receipt: Path | None) -> list[dict[str, Any]]:
    if receipt is None or not receipt.exists():
        return []
    if receipt.stat().st_size > 1_000_000:
        raise ValueError("MCP HTTP probe receipt exceeded its limit")
    return [json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()]


def _probe_server(
    root: Path,
    server_name: str,
    py: str,
    *,
    handshake_timeout: float,
    list_timeout: float,
    call_timeout: float,
    fault: str = "",
    receipt: Path | None = None,
    probe_environment: dict[str, str] | None = None,
    list_only: bool = False,
) -> McpVerdict:
    env = {**filter_env(os.environ), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    command = [py, "-B", server_name]
    if fault:
        command = [
            py, "-B", str(Path(__file__).with_name("mcp_http_probe.py")),
            str((root / server_name).resolve()), fault, str(receipt),
            json.dumps(probe_environment or {}),
        ]
    try:
        # NOT `-I`: since Python 3.11 isolated mode implies -P (safe path), which
        # drops the script's directory from sys.path — the scaffold's own
        # `import tools` would crash at boot and the gate would file a FALSE
        # "crashed on boot" issue whenever the mcp SDK is installed. `-B` keeps
        # the proof from polluting the artifact with __pycache__ instead.
        proc = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001
        return McpVerdict(skipped=True, unavailable=True, reason=f"could not spawn server: {exc}")

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
                return McpVerdict(
                    skipped=True,
                    unavailable=True,
                    reason="mcp SDK not importable — protocol verification unavailable",
                    checked={"booted": False, "handshake": "unavailable", "missing_dependency": "mcp"},
                )
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
        cases: list[dict[str, Any]] = []
        unsupported = False
        deadline = time.monotonic() + 90.0
        tool_names: list[str] = []
        first_required: tuple[str, dict] | None = None
        for entry in tools[:_MAX_TOOLS]:
            if fault and time.monotonic() >= deadline:
                issues.append("MCP HTTP failure checks exceeded the bounded 90-second tool-call budget")
                break
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
            if list_only:
                continue
            required = schema.get("required")
            if isinstance(required, list) and required and first_required is None:
                first_required = (name, schema)

            try:
                before = len(_probe_events(receipt))
                arguments = _http_fixture_args(schema) if fault else _fixture_args(schema)
                rid = client.send("tools/call", {"name": name, "arguments": arguments})
                resp = client.read_result(rid, call_timeout)  # type: ignore[arg-type]
            except _Timeout:
                issues.append(f"tool '{name}' did not return within {call_timeout:.0f}s on "
                              "valid input — it may hang")
                break  # a wedged server won't recover; stop compounding timeouts
            except _ServerExited:
                issues.append(f"server exited while calling tool '{name}' — it crashed "
                              "instead of returning a result")
                break
            if fault:
                events = _probe_events(receipt)[before:]
                requests = sum(event.get("kind") == "http" for event in events)
                blocked = any(event.get("kind") == "blocked_operation" for event in events)
                unsupported = unsupported or blocked
                status = "not_exercised"
                if requests:
                    status = "pass" if _failure_reported(resp) else "fail"
                    if status == "fail":
                        issues.append(
                            f"tool '{name}' did not report failure after injected {fault}; "
                            "propagate upstream errors instead of returning demo data or "
                            "claiming completed actions (tools.py / external client)."
                        )
                cases.append({
                    "tool": name, "fault": fault, "requests": requests,
                    "status": status, "blocked_operation": blocked,
                })
            elif not _call_ok(resp):
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
        if fault:
            checked["http_cases"] = cases
        return McpVerdict(
            issues=issues, tools=tool_names, checked=checked, unavailable=unsupported,
            reason=(
                "HTTP failure verification unavailable: an unsupported socket/process "
                "operation was blocked by the offline probe."
                if unsupported else ""
            ),
        )
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
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
