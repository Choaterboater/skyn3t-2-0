"""Deterministic MCP-server gate (``skyn3t.studio.mcp_check``) — wave-2 §3.3.

The gate spawns the delivered ``server.py`` and speaks the MCP JSON-RPC protocol
over stdio: ``initialize`` handshake → ``tools/list`` → ``tools/call`` each tool
with schema-derived fixture args → one malformed call. It NEVER hangs a build
(every read is time-bounded), NEVER raises, and records unavailable verification
when the mcp SDK / a Python runtime cannot run. Issues feed the fix-loop;
unavailable evidence must not be counted as successful verification.

The ``mcp`` package is NOT a test dependency, so these fixtures speak RAW
newline-delimited JSON-RPC over stdio WITHOUT the SDK — the gate speaks the
protocol, not the SDK, so it exercises every path on any machine. (The real
scaffold uses the SDK; the gate reports unavailable when the SDK can't boot —
see the missing-lib fixture.)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from skyn3t.studio.mcp_check import McpVerdict, check_mcp
from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord


def test_required_capability_cannot_pass_just_because_protocol_works(tmp_path: Path):
    (tmp_path / "server.py").write_text(_GOOD_SERVER)
    ProductSpecV1(
        project_id="limited-mcp",
        acceptance_registry_version=1,
        requirements=[RequirementRecord(
            text="Implement real paginated inventory",
            acceptance_ids=["mcp:capability:mist.inventory"],
            provenance={"mcp_profile": "mist-readonly-proactive-v1"},
        )],
    ).save(tmp_path)
    result = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert result.ok is False
    assert any("capability" in issue.lower() for issue in result.issues)


def test_agent_can_exercise_the_profile_before_delivery_metadata_exists(tmp_path: Path):
    (tmp_path / "server.py").write_text(_GOOD_SERVER)
    result = check_mcp(
        tmp_path, "mcp", python_exec=sys.executable,
        capability_profile="mist-readonly-proactive-v1",
    )
    assert not result.ok
    coverage = result.checked["capability_coverage"]
    assert len(coverage["cases"]) == 13
    assert coverage["cases"]["mist.worker"]["status"] == "failed"


def test_mcp_profile_rejects_machine_specific_dependency_paths(tmp_path: Path):
    from skyn3t.studio.mcp_capabilities import check_mcp_capabilities

    (tmp_path / "server.py").write_text(_GOOD_SERVER)
    (tmp_path / "environment_hack.py").write_text(
        'DEPENDENCIES = "/opt/some-machine/.venv/lib/python3.13/site-packages"\n'
    )
    ProductSpecV1(
        project_id="nonportable",
        requirements=[RequirementRecord(
            text="Portable credential-free startup",
            acceptance_ids=["mcp:capability:mist.credentials"],
            provenance={"mcp_profile": "mist-readonly-proactive-v1"},
        )],
    ).save(tmp_path)
    result = check_mcp_capabilities(tmp_path, python_exec=sys.executable, tools=["diagnose_site"])
    assert result is not None
    case = result["cases"]["mist.credentials"]
    assert case["status"] == "failed"
    assert "machine-specific dependency" in case["reason"]


def test_unprofiled_mcp_cannot_pass_with_host_dependency_override(tmp_path: Path):
    (tmp_path / "server.py").write_text(
        'import sys\nsys.path.insert(0, "/opt/developer/site-packages")\n' + _GOOD_SERVER
    )
    result = check_mcp(tmp_path, "mcp", python_exec=sys.executable)
    assert not result.ok
    assert any("machine-specific dependency" in issue for issue in result.issues)


@pytest.mark.parametrize("drop_false", [False, True])
def test_inventory_capability_exercises_actual_http_and_preserves_false_filters(tmp_path: Path, drop_false):
    from skyn3t.studio.mcp_capabilities import check_mcp_capabilities
    from skyn3t.studio.mcp_profiles import MIST_PROFILE, mcp_profile_product

    adapter = '''
import os, httpx
original_handle = handle
def handle(msg):
    if msg.get("method") == "tools/call" and msg["params"]["name"] == "get_org_inventory":
        args = msg["params"]["arguments"]
        response = httpx.get(
            "https://api.mist.com/api/v1/orgs/" + args["org_id"] + "/inventory",
            headers={"Authorization": "Token " + os.environ["MIST_API_TOKEN"]},
            params={"type": args["device_type"], "unassigned": args["unassigned"],
                    "limit": args.get("limit", 100), "page": args.get("page", 1)})
        response.raise_for_status()
        return _result(msg["id"], json.dumps({
            "success": True, "data": response.json(), "errors": [],
            "coverage": {"complete": True, "limitations": []}}))
    return original_handle(msg)
'''
    script = _GOOD_SERVER.replace("if __name__ == \"__main__\":", adapter + '\nif __name__ == "__main__":')
    if drop_false:
        script = script.replace('"unassigned": args["unassigned"]', '"unassigned": True')
    (tmp_path / "server.py").write_text(script)
    mcp_profile_product(MIST_PROFILE, "inventory-test", "Inventory contract").save(tmp_path)
    receipt = check_mcp_capabilities(
        tmp_path, python_exec=sys.executable, tools=["get_org_inventory"],
    )
    assert receipt is not None
    assert receipt["cases"]["mist.inventory"]["status"] == ("failed" if drop_false else "passed"), receipt

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
    assert v.to_dict()["status"] == "pass"
    assert not v.to_dict()["unavailable"]


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
    assert v.to_dict()["status"] == "fail"
    assert not v.to_dict()["unavailable"]


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


def test_missing_mcp_lib_reports_unavailable_verification(tmp_path):
    _write_server(tmp_path, _MISSING_LIB_SERVER)
    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    # Environment failure is not an implementation-repair gap or a protocol pass.
    assert v.skipped, v.to_dict()
    assert not v.ok
    assert v.gaps() == []
    assert v.issues == []
    recorded = v.to_dict()
    assert recorded["status"] == "unavailable"
    assert recorded["unavailable"] is True
    assert recorded["ok"] is False
    assert recorded["checked"]["booted"] is False
    assert recorded["checked"]["handshake"] == "unavailable"
    assert recorded["checked"]["missing_dependency"] == "mcp"


def test_incompatible_mcp_import_is_a_startup_defect(tmp_path):
    _write_server(
        tmp_path,
        _MISSING_LIB_SERVER.replace(
            "ModuleNotFoundError: No module named 'mcp'",
            "ImportError: cannot import name 'FastMCP' from 'mcp.server.fastmcp'",
        ),
    )
    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert not v.skipped
    assert v.to_dict()["status"] == "fail"
    assert not v.to_dict()["unavailable"]
    assert v.checked["booted"] is False
    assert v.gaps()


def test_non_mcp_stack_soft_skips(tmp_path):
    _write_server(tmp_path, _GOOD_SERVER)
    v = check_mcp(tmp_path, stack="fastapi", python_exec=sys.executable)
    assert v.skipped
    assert v.gaps() == []
    assert not v.ok
    assert v.to_dict()["status"] == "skipped"
    assert v.to_dict()["unavailable"] is False


def test_no_server_file_reports_unavailable_verification(tmp_path):
    v = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert v.skipped
    assert v.gaps() == []
    assert v.to_dict()["status"] == "unavailable"
    assert v.to_dict()["ok"] is False


def test_no_python_runtime_reports_unavailable_verification(tmp_path, monkeypatch):
    _write_server(tmp_path, _GOOD_SERVER)
    monkeypatch.setattr("skyn3t.studio.mcp_check._python_exec", lambda explicit: None)
    v = check_mcp(tmp_path, stack="mcp")
    assert v.to_dict()["status"] == "unavailable"
    assert v.to_dict()["ok"] is False
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


_HTTP_SERVER = r'''
import json, sys
import httpx

def call(args):
    if not args.get("api_token"):
        return {"success": False, "error": "credentials_required"}
    try:
        response = httpx.get("https://mist.invalid/api/v1/sites",
                             headers={"Authorization": "Token " + args["api_token"]})
        response.raise_for_status()
    except httpx.HTTPError:
        return FAILURE_RESULT
    return {"success": True, "sites": response.json()}

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "http-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "site_health", "description": "Read real site health.",
                  "inputSchema": {"type": "object", "properties": {
                      "api_token": {"type": "string", "default": ""}}}}]}
    elif method == "tools/call":
        value = call(request["params"].get("arguments", {}))
        result = {"content": [{"type": "text", "text": json.dumps(value)}],
                  "structuredContent": value, "isError": False}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''


def test_http_failures_cannot_be_replaced_with_success(tmp_path):
    _write_server(tmp_path, _HTTP_SERVER.replace(
        "FAILURE_RESULT", '{"success": True, "action_status": "completed", "devices": ["demo-ap"]}',
    ))

    verdict = check_mcp(tmp_path, "mcp", python_exec=sys.executable)

    assert not verdict.ok, verdict.to_dict()
    assert not verdict.unavailable
    assert any("HTTP 401" in issue for issue in verdict.issues)
    assert any("HTTP 503" in issue for issue in verdict.issues)
    assert any("timeout" in issue for issue in verdict.issues)


def test_http_failures_are_exercised_and_reported_honestly(tmp_path):
    _write_server(tmp_path, _HTTP_SERVER.replace(
        "FAILURE_RESULT", '{"success": False, "error": "upstream_failed"}',
    ))

    verdict = check_mcp(tmp_path, "mcp", python_exec=sys.executable)

    assert verdict.ok, verdict.to_dict()
    checks = verdict.checked["http_failure_handling"]
    assert checks["exercised_tools"] == ["site_health"]
    assert {case["fault"] for case in checks["cases"]} == {"HTTP 401", "HTTP 503", "timeout"}
    assert all(case["status"] == "pass" for case in checks["cases"])


def test_success_with_an_error_field_is_still_false_success(tmp_path):
    _write_server(tmp_path, _HTTP_SERVER.replace(
        "FAILURE_RESULT", '{"success": True, "error": "unauthorized"}',
    ))

    verdict = check_mcp(tmp_path, "mcp", python_exec=sys.executable)

    assert not verdict.ok, verdict.to_dict()
    assert any("site_health" in issue for issue in verdict.issues)


def test_http_integration_without_exercisable_calls_is_unavailable(tmp_path):
    source = _HTTP_SERVER.replace("if not args.get(\"api_token\"):", "if True:")
    _write_server(tmp_path, source.replace(
        "FAILURE_RESULT", '{"success": False, "error": "upstream_failed"}',
    ))

    verdict = check_mcp(tmp_path, "mcp", python_exec=sys.executable)

    assert verdict.unavailable, verdict.to_dict()
    assert not verdict.ok
    assert verdict.gaps() == []
    assert "not exercised" in verdict.reason


@pytest.mark.parametrize("client", ["httpx_async", pytest.param("httpx_async_socketpair_fallback", marks=pytest.mark.requires_loopback), "requests", "urllib"])
def test_supported_http_clients_report_injected_failures(tmp_path, client, monkeypatch):
    source = _HTTP_SERVER.replace("FAILURE_RESULT", '{"success": False, "error": "upstream_failed"}')
    if client == "httpx_async_socketpair_fallback":
        import skyn3t.studio.mcp_check as gate

        # Exercise CPython's Windows socketpair path on every host. The wrapper
        # changes the trusted harness startup, never the generated server.
        wrapper = tmp_path.parent / (tmp_path.name + "_probe.py")
        harness = Path(gate.__file__).with_name("mcp_http_probe.py")
        wrapper.write_text(
            "import runpy, socket\n"
            "socket.socketpair = socket._fallback_socketpair\n"
            f"runpy.run_path({str(harness)!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        original_popen = gate.subprocess.Popen

        def run_fallback_probe(command, *args, **kwargs):
            if len(command) > 2 and command[2] == str(harness):
                command = [*command[:2], str(wrapper), *command[3:]]
            return original_popen(command, *args, **kwargs)

        monkeypatch.setattr(gate.subprocess, "Popen", run_fallback_probe)
    if client.startswith("httpx_async"):
        setup = '''
import asyncio
async def _request(*args, **kwargs):
    async with httpx.AsyncClient() as client:
        return await client.get(*args, **kwargs)
def request_api(*args, **kwargs):
    return asyncio.run(_request(*args, **kwargs))
'''
        source = source.replace("httpx.get(", "request_api(")
    elif client == "requests":
        pytest.importorskip("requests")
        setup = "\nimport requests\n"
        source = source.replace("httpx.get(", "requests.get(").replace(
            "except httpx.HTTPError:", "except (httpx.HTTPError, requests.RequestException):",
        )
    else:
        setup = '''
import urllib.request, urllib.error
def request_api(url, headers):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers))
'''
        source = source.replace("httpx.get(", "request_api(").replace(
            "except httpx.HTTPError:", "except (httpx.HTTPError, urllib.error.URLError, TimeoutError):",
        )
    source = source.replace("import httpx", "import httpx\n" + setup, 1)
    _write_server(tmp_path, source)

    verdict = check_mcp(tmp_path, "mcp", python_exec=sys.executable)

    assert verdict.ok, verdict.to_dict()
    cases = verdict.checked["http_failure_handling"]["cases"]
    assert len(cases) == 3 and all(case["requests"] == 1 for case in cases)


@pytest.mark.parametrize("operation", [
    'socket.create_connection(("127.0.0.1", 1))',
    'subprocess.run([sys.executable, "-c", "open(\'unexpected-write\', \'w\').write(\'bad\')"])',
])
def test_http_probe_blocks_raw_network_and_subprocesses(tmp_path, operation):
    source = _HTTP_SERVER.replace("FAILURE_RESULT", '{"success": False, "error": "blocked"}')
    source = source.replace("import httpx", "import httpx, socket, subprocess", 1)
    source = source.replace(
        'response = httpx.get("https://mist.invalid/api/v1/sites",\n'
        '                             headers={"Authorization": "Token " + args["api_token"]})',
        f"response = {operation}",
    ).replace("except httpx.HTTPError:", "except (httpx.HTTPError, OSError):")
    _write_server(tmp_path, source)

    verdict = check_mcp(tmp_path, "mcp", python_exec=sys.executable)

    assert not verdict.ok
    assert verdict.unavailable, verdict.to_dict()
    assert not (tmp_path / "unexpected-write").exists()
    assert all(
        case["blocked_operation"]
        for case in verdict.checked["http_failure_handling"]["cases"]
    )


def test_http_probe_uses_a_valid_time_window_and_scope():
    from skyn3t.studio.mcp_check import _http_fixture_args

    args = _http_fixture_args({
        "type": "object", "required": ["start", "end", "site_id"],
        "properties": {
            "start": {"type": "integer"}, "end": {"type": "integer"},
            "site_id": {"type": "string", "format": "uuid"},
        },
    })
    assert args["end"] - args["start"] == 900
    assert args["end"] < time.time()
    assert args["site_id"] == "00000000-0000-4000-8000-000000000001"


def test_http_probe_preserves_application_endpoint_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("MIST_BASE_URL", raising=False)
    source = _HTTP_SERVER.replace("import json, sys", "import json, sys, os")
    source = source.replace(
        "def call(args):",
        'def call(args):\n'
        '    if os.environ.get("MIST_BASE_URL", "https://api.mist.com") != "https://api.mist.com":\n'
        '        return {"success": False, "error": "unsupported cloud hostname"}',
    ).replace("FAILURE_RESULT", '{"success": False, "error": "upstream unavailable"}')
    _write_server(tmp_path, source)

    verdict = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert verdict.ok, verdict.to_dict()


@pytest.mark.parametrize("statement", [
    "import urllib.request", "import json, httpx", "import os, requests as client",
])
def test_http_detection_handles_supported_import_forms(tmp_path, statement):
    from skyn3t.studio.mcp_check import _http_requirements

    _write_server(tmp_path, statement + "\n")
    assert _http_requirements(tmp_path, "")[0]


def test_http_credentials_must_not_be_required_for_startup(tmp_path):
    source = _HTTP_SERVER.replace(
        "import httpx",
        'import httpx, os\n'
        'if not os.getenv("MIST_API_TOKEN"):\n'
        '    raise RuntimeError("credentials incorrectly required before tool listing")',
    ).replace("FAILURE_RESULT", '{"success": False, "error": "upstream unavailable"}')
    _write_server(tmp_path, source)

    verdict = check_mcp(tmp_path, stack="mcp", python_exec=sys.executable)
    assert not verdict.ok
    assert any("startup" in issue for issue in verdict.issues)


@pytest.mark.parametrize("success", [False, True])
def test_failure_semantics_cover_sdk_wrappers_and_conflicting_error_flags(success):
    from skyn3t.studio.mcp_check import _failure_reported

    response = {"result": {
        "content": [], "isError": success,
        "structuredContent": {"result": {"success": success, "error": "upstream failed"}},
    }}
    assert _failure_reported(response) is not success


@pytest.mark.parametrize("dependency_path", [
    "/opt/developer/site-packages", "/usr/local/lib/python3.12/dist-packages",
    r"C:\developer\venv\Lib\site-packages", r"C:venv\Lib\site-packages",
    r"\\server\share\venv\Lib\site-packages", r"\venv\Lib\site-packages",
])
def test_dependency_portability_is_lexical_across_host_platforms(tmp_path, monkeypatch, dependency_path):
    from pathlib import PureWindowsPath

    import skyn3t.studio.mcp_check as gate

    class WindowsHostPath(type(Path())):
        def is_absolute(self):
            return PureWindowsPath(str(self)).is_absolute()

    (tmp_path / "server.py").write_text(f"DEPENDENCIES = {dependency_path!r}\n", encoding="utf-8")
    monkeypatch.setattr(gate, "Path", WindowsHostPath)
    issues = gate._portability_issues(tmp_path, ["server.py"])
    assert issues == ["server.py embeds a machine-specific dependency path"]


def test_socketpair_connect_allowance_matches_only_its_socket_and_listener():
    from skyn3t.studio.mcp_http_probe import _is_socketpair_connection

    client, other = object(), object()
    address = ("127.0.0.1", 12345)
    allowed = {client: address}
    assert _is_socketpair_connection("socket.connect", (client, address), allowed)
    assert not _is_socketpair_connection("socket.connect", (other, address), allowed)
    assert not _is_socketpair_connection("socket.connect", (client, ("127.0.0.1", 12346)), allowed)
    assert not _is_socketpair_connection("socket.connect", (client, ("198.51.100.1", 12345)), allowed)
    assert not _is_socketpair_connection("socket.sendto", (client, address), allowed)
    assert not _is_socketpair_connection("socket.connect", (client, address), {})


def test_socketpair_connect_failure_revokes_allowance_and_closes_socket(monkeypatch):
    from unittest.mock import MagicMock

    from skyn3t.studio import mcp_http_probe as probe

    allowed = {}
    client = MagicMock()
    listener = MagicMock()
    listener.__enter__.return_value = listener
    listener.getsockname.return_value = ("127.0.0.1", 12345)

    def reject_connection(address):
        assert allowed == {client: address}
        raise OSError("fixture connection failure")

    client.connect.side_effect = reject_connection
    monkeypatch.setattr(probe.socket, "socket", MagicMock(side_effect=[client, listener]))
    with pytest.raises(OSError, match="fixture connection failure"):
        probe._local_socketpair(allowed)
    assert allowed == {}
    client.close.assert_called_once()
    listener.__exit__.assert_called_once()


def test_project_relative_dependencies_remain_portable(tmp_path):
    from skyn3t.studio.mcp_check import _portability_issues

    (tmp_path / "server.py").write_text(
        'DEPENDENCIES = "vendor/site-packages"\n', encoding="utf-8",
    )
    assert _portability_issues(tmp_path, ["server.py"]) == []


def test_socketpair_rejects_unexpected_peer_and_closes_both_sockets(monkeypatch):
    from unittest.mock import MagicMock

    from skyn3t.studio import mcp_http_probe as probe

    allowed = {}
    client, listener, peer = MagicMock(), MagicMock(), MagicMock()
    listener.__enter__.return_value = listener
    listener.getsockname.return_value = ("127.0.0.1", 12345)
    listener.accept.return_value = (peer, ("127.0.0.1", 54321))
    peer.getsockname.return_value = ("127.0.0.1", 12345)
    client.getpeername.return_value = ("127.0.0.1", 99999)
    monkeypatch.setattr(probe.socket, "socket", MagicMock(side_effect=[client, listener]))
    with pytest.raises(ConnectionError, match="Unexpected peer connection"):
        probe._local_socketpair(allowed)
    assert allowed == {}
    client.close.assert_called_once()
    peer.close.assert_called_once()
