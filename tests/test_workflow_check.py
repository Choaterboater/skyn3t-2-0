"""Tests for the deterministic agent-workflow gate (``workflow_check``).

The gate speaks plain HTTP, so the fixtures are stdlib ``http.server`` programs
(zero third-party deps) — the test_rag_check pattern. Every degrade-open path
is pinned as SKIP (no gaps); every real-defect path is pinned as an ISSUE.
The one live integration test boots the REAL workflow scaffold under
fastapi/uvicorn (skipped when unavailable).
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from skyn3t.studio.workflow_check import check_workflow

# A well-behaved workflow runner: honours dry_run / skipped_no_delivery, keeps
# an append-only ledger, rejects unknown workflows with a 404.
_GOOD_SERVER = '''
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

RUNS = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path == "/v1/stats":
            return self._send(200, {"runs": len(RUNS), "workflows": ["briefing"]})
        if path == "/runs":
            return self._send(200, {"runs": RUNS})
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if urlparse(self.path).path != "/trigger":
            return self._send(404, {"detail": "not found"})
        if payload.get("workflow") != "briefing":
            return self._send(404, {"detail": "unknown workflow"})
        dry = bool(payload.get("dry_run", True))
        if dry:
            delivery = {"status": "dry_run"}
        elif os.environ.get("WEBHOOK_URL"):
            delivery = {"status": "sent"}
        else:
            delivery = {"status": "skipped_no_delivery"}
        record = {"run_id": len(RUNS) + 1, "workflow": "briefing",
                  "status": "done", "dry_run": dry}
        RUNS.append(record)
        return self._send(200, {**record, "brief": "the brief", "delivery": delivery})

HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
'''

# Live trigger without config CRASHES instead of skipping delivery.
_CRASHY_LIVE_SERVER = _GOOD_SERVER.replace(
    'delivery = {"status": "skipped_no_delivery"}',
    'return self._send(500, {"detail": "no webhook configured"})',
)

# The ledger never records anything.
_AMNESIC_SERVER = _GOOD_SERVER.replace("RUNS.append(record)", "pass")

# Unknown workflows are accepted instead of rejected.
_ACCEPT_ANYTHING_SERVER = _GOOD_SERVER.replace(
    'return self._send(404, {"detail": "unknown workflow"})',
    'pass',
)

# /v1/stats forgets to name the workflows.
_NAMELESS_SERVER = _GOOD_SERVER.replace(
    '{"runs": len(RUNS), "workflows": ["briefing"]}',
    '{"runs": len(RUNS)}',
)


def _project(tmp_path, main_code: str):
    (tmp_path / "main.py").write_text(main_code, encoding="utf-8")
    return tmp_path


def _fastapi_stack_available() -> bool:
    return all(
        importlib.util.find_spec(mod) is not None for mod in ("fastapi", "uvicorn")
    )


# ---- degrade-open skips ----------------------------------------------------


def test_skips_non_workflow_stack(tmp_path):
    v = check_workflow(_project(tmp_path, _GOOD_SERVER), stack="rag")
    assert v.skipped and v.gaps() == []


def test_skips_missing_main(tmp_path):
    v = check_workflow(tmp_path, stack="workflow")
    assert v.skipped and v.gaps() == []


def test_missing_dep_on_boot_soft_skips(tmp_path):
    _project(tmp_path, 'raise ModuleNotFoundError("No module named \'fastapi\'")\n')
    v = check_workflow(tmp_path, stack="workflow", boot_timeout=10.0)
    assert v.skipped, v.to_dict()
    assert v.gaps() == []


# ---- the contract ----------------------------------------------------------


def test_good_stdlib_server_passes(tmp_path):
    v = check_workflow(_project(tmp_path, _GOOD_SERVER), stack="workflow")
    assert v.ok, v.to_dict()
    assert v.checked["trigger_dry"] == "dry_run"
    assert v.checked["trigger_live"] == "skipped_no_delivery"
    assert v.checked["unknown_workflow_status"] == 404
    assert v.checked["workflow_name"] == "briefing"


def test_live_trigger_crash_is_an_issue(tmp_path):
    v = check_workflow(_project(tmp_path, _CRASHY_LIVE_SERVER), stack="workflow")
    assert not v.skipped
    assert any("skipped_no_delivery" in i for i in v.issues), v.issues


def test_amnesic_ledger_is_an_issue(tmp_path):
    v = check_workflow(_project(tmp_path, _AMNESIC_SERVER), stack="workflow")
    assert not v.skipped
    assert any("ledger" in i for i in v.issues), v.issues


def test_accepting_unknown_workflows_is_an_issue(tmp_path):
    v = check_workflow(_project(tmp_path, _ACCEPT_ANYTHING_SERVER), stack="workflow")
    assert not v.skipped
    assert any("unknown-workflow" in i or "ACCEPTED" in i for i in v.issues), v.issues


def test_stats_without_workflow_names_is_an_issue_and_stops_probing(tmp_path):
    v = check_workflow(_project(tmp_path, _NAMELESS_SERVER), stack="workflow")
    assert not v.skipped
    assert any("workflows" in i for i in v.issues), v.issues
    assert v.checked["trigger"] == "not_probed"


def test_fixtures_are_real_variants():
    # .replace()-built fixtures silently no-op if the base drifts — pin it.
    for variant in (_CRASHY_LIVE_SERVER, _AMNESIC_SERVER,
                    _ACCEPT_ANYTHING_SERVER, _NAMELESS_SERVER):
        assert variant != _GOOD_SERVER


# ---- live integration: the REAL scaffold under fastapi/uvicorn -------------


@pytest.mark.skipif(
    not _fastapi_stack_available(),
    reason="fastapi/uvicorn not importable in this env",
)
def test_real_workflow_scaffold_passes_the_gate(tmp_path):
    from skyn3t.agents._scaffold import scaffold_for

    files = scaffold_for("workflow", "briefing-bot", "an agent that sends daily briefings")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")
    v = check_workflow(tmp_path, stack="workflow", python_exec=sys.executable)
    assert not v.skipped, v.to_dict()
    assert v.ok, v.to_dict()
    assert v.checked["trigger_dry"] == "dry_run"
    assert v.checked["trigger_live"] == "skipped_no_delivery"
