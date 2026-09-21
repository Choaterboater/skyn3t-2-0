"""Source-bound feature evidence for explicitly contracted MCP builds."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from skyn3t.security.secrets import filter_env
from skyn3t.studio.mcp_mist_probe import AP_ID, AP_MAC, BASE, CLIENT_MAC, ORG, SITE, TOKEN
from skyn3t.studio.mcp_profiles import MIST_CAPABILITIES, MIST_PROFILE, mcp_profile_name
from skyn3t.studio.product_spec import ProductSpecV1
from skyn3t.worktree import source_tree_snapshot

_TOOLS = {
    "mist.sites": ("list_org_sites", "list_sites"),
    "mist.inventory": ("get_org_inventory",),
    "mist.devices": ("get_site_device_stats", "get_site_inventory"),
    "mist.alarms": ("search_site_alarms",),
    "mist.insights": ("get_client_insights",),
    "mist.sle": ("list_site_sle_metrics", "get_site_sle_summary"),
    "mist.diagnosis": ("diagnose_site",),
    "mist.scope": ("diagnose_site", "get_org_inventory"),
    "mist.unknown": ("diagnose_site",),
    "mist.failures": ("diagnose_site",),
    "mist.worker": ("list_incidents",),
    "mist.notifications": ("list_incidents", "list_notifications"),
    "mist.credentials": ("diagnose_site",),
}


class _Probe:
    def __init__(
        self, root: Path, python: str, scratch: Path,
        server_entrypoint: str | None, worker_entrypoint: str | None,
    ) -> None:
        self.root, self.python, self.scratch = root, python, scratch
        self.server_entrypoint, self.worker_entrypoint = server_entrypoint, worker_entrypoint
        self.assertions = 0
        self.receipts: list[Path] = []

    def require(self, condition: bool, message: str) -> None:
        self.assertions += 1
        if not condition:
            raise AssertionError(message)

    def spawn(self, scenario: str, *, worker: bool = False, continuous: bool = False):
        from skyn3t.studio.mcp_check import _StdioClient

        identifier = uuid4().hex
        receipt = self.scratch / f"{identifier}.jsonl"
        self.receipts.append(receipt)
        home = self.scratch / "home"
        home.mkdir(exist_ok=True)
        environment = {
            **filter_env(os.environ),
            "HOME": str(home), "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "MIST_API_TOKEN": "" if scenario == "credentials" else TOKEN,
            "MIST_BASE_URL": BASE, "MIST_ORG_ID": ORG,
            "MIST_SITE_ID": "",
            "MIST_DB_PATH": str(self.scratch / "state.sqlite3"),
        }
        if scenario.startswith("scope-"):
            environment["MIST_ORG_ID"] = ""
        environment.pop("PYTHONPATH", None)
        entrypoint = self.worker_entrypoint if worker else self.server_entrypoint
        config = self.scratch / f"{identifier}.json"
        config.write_text(json.dumps({
            "scenario": scenario, "receipt": str(receipt), "environment": environment,
            "entrypoint": entrypoint,
        }))
        script = self.root / ("worker.py" if worker else "server.py")
        args = []
        if worker:
            args = ["--org-id", ORG, "--site-id", SITE]
            args += ["--interval", "1"] if continuous else ["--once"]
        process = subprocess.Popen(
            [self.python, "-B", str(Path(__file__).with_name("mcp_mist_probe.py")),
             str(config), str(script), *args],
            cwd=self.scratch if entrypoint else self.root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=environment,
        )
        return process, _StdioClient(process)

    @contextmanager
    def session(self, scenario: str = "online"):
        from skyn3t.studio.mcp_check import _ServerExited, _shutdown

        process, client = self.spawn(scenario)
        try:
            rid = client.send("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "contract-client", "version": "1"},
            })
            reply = client.read_result(rid, 15)
            self.require("result" in reply, "MCP initialize failed")
            client.send("notifications/initialized", notification=True)
            yield client
        except _ServerExited as exc:
            client.wait_exit(1)
            raise RuntimeError(f"MCP process exited: {client.stderr_text()[-900:]}") from exc
        finally:
            _shutdown(process)

    def call(self, client, tool: str, **arguments) -> dict:
        rid = client.send("tools/call", {"name": tool, "arguments": arguments})
        response = client.read_result(rid, 18)
        result = response.get("result", {})
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and isinstance(structured.get("result"), dict):
            structured = structured["result"]
        if not isinstance(structured, dict) or "success" not in structured:
            content = result.get("content", [])
            text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
            try:
                structured = json.loads(text)
            except (ValueError, TypeError) as exc:
                raise AssertionError(f"{tool} must return a structured JSON envelope: {text[:350]}") from exc
        self.require(isinstance(structured, dict), f"{tool} did not return an object")
        self.require(type(structured.get("success")) is bool, f"{tool} omitted boolean success")
        self.require(isinstance(structured.get("errors"), list), f"{tool} omitted errors list")
        coverage = structured.get("coverage")
        self.require(
            isinstance(coverage, dict) and type(coverage.get("complete")) is bool,
            f"{tool} omitted truthful coverage.complete",
        )
        return structured

    def success(self, result: dict) -> Any:
        self.require(result["success"] is True, f"Required successful read failed: {str(result)[:550]}")
        self.require(not result["errors"], "Successful complete fixture returned errors")
        return result.get("data")

    def failure(self, result: dict) -> None:
        self.require(result["success"] is False, "Failed observation claimed success")
        self.require(bool(result["errors"]), "Failure omitted explicit errors")
        self.require(result["coverage"]["complete"] is False, "Failure claimed complete coverage")
        if isinstance(result.get("data"), dict):
            self.require(result["data"].get("health") != "healthy",
                         "Failed observation still claimed aggregate healthy")

    def requests(self) -> list[dict]:
        records = [
            json.loads(line) for path in self.receipts if path.exists()
            for line in path.read_text().splitlines() if line.strip()
        ]
        self.require(not any(r.get("kind") == "blocked_operation" for r in records),
                     "Probe encountered unsupported external I/O")
        return [r for r in records if r.get("kind") == "http"]

    def read_reached(self, suffix: str) -> None:
        self.require(any(r["path"].endswith(suffix) for r in self.requests()),
                     f"Required real HTTP read was never reached: {suffix}")

    def worker(self, scenario: str, *, continuous: bool = False, failure: bool = False) -> None:
        from skyn3t.studio.mcp_check import _shutdown

        before = len(self.requests())
        process, client = self.spawn(scenario, worker=True, continuous=continuous)
        try:
            if continuous:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and process.poll() is None:
                    records = self.requests()[before:]
                    polls = sum(r["path"].endswith("/stats/devices")
                                and r["params"].get("type", "ap") == "ap" for r in records)
                    if polls >= 2:
                        break
                    time.sleep(0.05)
                self.require(process.poll() is None, "Standalone worker did not remain running")
                self.require(polls >= 2, "Standalone worker did not poll without a chat host")
                process.terminate()
            process.wait(timeout=25)
            client.wait_exit(1)
            self.require((process.returncode != 0) if failure else (process.returncode == 0),
                         f"Worker exited {process.returncode}: {client.stderr_text()[-700:]}")
            records = self.requests()[before:]
            if scenario in {"wrong-org", "credentials", "scope-error"}:
                self.require(all(r["path"] == "/api/v1/self" for r in records),
                             "Worker performed telemetry reads after a failed authorization prerequisite")
                if scenario == "credentials":
                    self.require(not records, "Unconfigured worker performed HTTP reads")
                else:
                    self.require(bool(records), "Worker authorization failure was not exercised")
            else:
                telemetry = next(
                    (index for index, record in enumerate(records)
                     if record["path"].endswith("/stats/devices")), None,
                )
                self.require(telemetry is not None, "Worker did not reach real telemetry")
                prerequisite = records[:telemetry]
                self.require(any(r["path"] == "/api/v1/self" for r in prerequisite),
                             "Worker bypassed /self scope validation before telemetry")
                self.require(any(r["path"].endswith("/sites") and r["params"].get("page") == "3"
                                 for r in prerequisite),
                             "Worker bypassed later-page organization/site membership")
        finally:
            _shutdown(process)


def _inventory(probe: _Probe) -> None:
    with probe.session() as client:
        for kind, unassigned in (("ap", True), ("switch", False), ("gateway", True)):
            result = probe.call(client, "get_org_inventory", org_id=ORG,
                                device_type=kind, unassigned=unassigned, limit=100, page=1)
            data = probe.success(result)
            probe.require(isinstance(data, list) and len(data) == 1, "Inventory must retain asset records")
            probe.require(data[0].get("serial") == "SYNTHETIC-" + kind.upper(),
                          "Organization inventory identity was lost or replaced by site stats")
    probe.read_reached("/inventory")
    probe.require(any(r["params"].get("unassigned", "").lower() == "false"
                      for r in probe.requests() if r["path"].endswith("/inventory")),
                  "Explicit unassigned=false was dropped")


def _exercise(capability: str, probe: _Probe) -> None:
    if capability == "mist.inventory":
        _inventory(probe)
    elif capability == "mist.sites":
        with probe.session() as client:
            result = probe.call(client, "list_sites", org_id=ORG)
            data = probe.success(result)
            probe.require(isinstance(data, list) and len(data) == 250,
                          "list_sites must traverse all 250 authorized sites, not page one")
            probe.require(any(row.get("id") == SITE for row in data), "Later-page site was omitted")
            probe.require(result["coverage"]["complete"] is True, "Complete paged result marked incomplete")
            page = probe.success(probe.call(client, "list_org_sites", org_id=ORG, limit=100, page=3))
            probe.require(isinstance(page, list) and len(page) == 50, "Explicit site page control was lost")
            rid = client.send("resources/read", {"uri": f"mist://orgs/{ORG}/sites"})
            resource = client.read_result(rid, 18).get("result", {}).get("contents", [])
            probe.require(bool(resource), "Organization-sites resource was lost")
            payload = json.loads(resource[0]["text"])
            rows = payload.get("data") if isinstance(payload, dict) else payload
            probe.require(isinstance(rows, list) and len(rows) == 250,
                          "Sites resource does not use complete paginated discovery")
        probe.read_reached("/sites")
        with probe.session("repeated-sites") as client:
            probe.failure(probe.call(client, "list_sites", org_id=ORG))
    elif capability == "mist.devices":
        with probe.session() as client:
            for kind in ("ap", "switch", "gateway"):
                data = probe.success(probe.call(client, "get_site_device_stats",
                    org_id=ORG, site_id=SITE, device_type=kind, status="all", limit=100, page=1))
                probe.require(isinstance(data, list) and data[0].get("type") == kind,
                              f"Current {kind} telemetry was not preserved")
            probe.success(probe.call(client, "get_site_inventory", org_id=ORG, site_id=SITE))
        probe.read_reached("/stats/devices")
    elif capability == "mist.alarms":
        with probe.session("alarm-pages") as client:
            end = int(time.time())
            arguments = dict(org_id=ORG, site_id=SITE, start=end - 3600, end=end,
                             limit=1, group="marvis", severity="warn", acked=False)
            first = probe.call(client, "search_site_alarms", **arguments)
            data = probe.success(first)
            probe.require(isinstance(data, list) and data[0].get("id") == "alarm-1",
                          "Alarm result identifiers were lost")
            cursor = first["coverage"].get("next_cursor")
            probe.require(cursor == "opaque+/cursor==", "Return the opaque cursor, not next's URL")
            second = probe.success(probe.call(client, "search_site_alarms",
                                             **arguments, search_after=cursor))
            probe.require(second[0].get("id") == "alarm-2", "Alarm continuation did not reach page two")
        probe.read_reached("/alarms/search")
        probe.require(any(r["params"].get("acked", "").lower() == "false"
                          for r in probe.requests() if r["path"].endswith("/alarms/search")),
                      "Explicit acked=false was dropped")
        with probe.session("alarm-host") as client:
            result = probe.call(client, "search_site_alarms", org_id=ORG, site_id=SITE)
            probe.require(not result["coverage"].get("next_cursor") and bool(result["errors"]),
                          "Untrusted continuation host was accepted without an explicit error")
    elif capability == "mist.insights":
        with probe.session() as client:
            data = probe.success(probe.call(client, "get_client_insights",
                org_id=ORG, site_id=SITE, client_mac=CLIENT_MAC,
                metrics="top-app-by-bytes", duration="1d", interval="1h", limit=100, page=1))
            probe.require(isinstance(data, dict) and data.get("synthetic_unverified_series") == [17, 31],
                          "Opaque client Insights were normalized, dropped or invented")
        probe.read_reached("/insights/client/" + CLIENT_MAC)
    elif capability == "mist.sle":
        with probe.session() as client:
            metrics = probe.success(probe.call(client, "list_site_sle_metrics", org_id=ORG, site_id=SITE))
            probe.require(metrics == ["time-to-connect"], "Metric discovery was not retained raw")
            data = probe.success(probe.call(client, "get_site_sle_summary",
                org_id=ORG, site_id=SITE, metric="time-to-connect", duration="1d"))
            probe.require(isinstance(data, dict) and data.get("synthetic_unverified_trend") == [23, 47],
                          "Opaque summary-trend was lost or replaced by legacy summary")
        probe.read_reached("/metrics")
        probe.read_reached("/summary-trend")
    elif capability in {"mist.diagnosis", "mist.unknown"}:
        with probe.session("unknown" if capability.endswith("unknown") else "offline") as client:
            result = probe.call(client, "diagnose_site", org_id=ORG, site_id=SITE, duration="1d")
            data = result.get("data")
            if not isinstance(data, dict):
                raise AssertionError("Diagnosis did not return structured evidence")
            probe.require(data.get("health") in {"degraded", "unknown"},
                          "Offline/unknown AP became aggregate healthy or omitted health")
            if capability.endswith("unknown"):
                probe.require(data.get("health") == "unknown", "Missing AP status must remain unknown")
            else:
                probe.require(bool(data.get("findings")), "Disconnected AP produced no diagnosis finding")
                probe.require(bool(data.get("recommendations")), "Diagnosis omitted actionable recommendations")
            evidence = json.dumps(data.get("evidence"))
            probe.require(AP_MAC in evidence and CLIENT_MAC in evidence, "Observed identifiers were lost")
            probe.require(all(r.get("applied") is False for r in data.get("recommendations", [])),
                          "Recommendations claimed applied network changes")
        for path in ("/stats/devices", "/stats/clients", "/alarms/search", "/summary-trend"):
            probe.read_reached(path)
        if capability == "mist.unknown":
            with probe.session("clients-opaque") as client:
                result = probe.call(client, "diagnose_site", org_id=ORG, site_id=SITE)
                data = result.get("data")
                probe.require(isinstance(data, dict) and data.get("health") == "unknown",
                              "An unverified client response shape was treated as healthy/empty")
                probe.require(CLIENT_MAC in json.dumps(data),
                              "Opaque client observations were discarded instead of retained raw")
    elif capability == "mist.scope":
        with probe.session("wrong-org") as client:
            probe.failure(probe.call(client, "diagnose_site", org_id=ORG, site_id=SITE))
        with probe.session("site-only") as client:
            probe.failure(probe.call(client, "get_org_inventory", org_id=ORG, device_type="ap"))
        with probe.session("site-admin") as client:
            probe.failure(probe.call(client, "get_org_inventory", org_id=ORG, device_type="ap"))
        with probe.session("site-wrong-org") as client:
            probe.failure(probe.call(client, "diagnose_site", org_id=ORG, site_id=SITE))
        probe.require(all(r["path"] == "/api/v1/self" for r in probe.requests()),
                      "Wrong-org or site-only privilege granted organization-wide reads")
        with probe.session("scope-single") as client:
            data = probe.success(probe.call(client, "diagnose_site", duration="2h"))
            probe.require(data.get("scope") == {"org_id": ORG, "site_id": SITE},
                          "A single authorized scope was not discovered and retained")
            actions = data.get("next_actions")
            probe.require(isinstance(actions, list) and bool(actions),
                          "Diagnosis omitted scoped drill-down actions")
            for action in actions:
                arguments = action.get("arguments", {})
                probe.require(
                    action.get("tool") in {tool for names in _TOOLS.values() for tool in names}
                    and arguments.get("org_id") == ORG and arguments.get("site_id") == SITE,
                    "Drill-down changed or omitted the selected scope",
                )
        for scenario in ("online", "scope-multiple-orgs", "scope-incomplete"):
            before = len(probe.requests())
            with probe.session(scenario) as client:
                result = probe.call(client, "diagnose_site")
                probe.failure(result)
                if scenario != "scope-incomplete":
                    data = result.get("data")
                    probe.require(
                        isinstance(data, dict) and bool(data.get("question"))
                        and isinstance(data.get("choices"), list) and len(data["choices"]) > 1,
                        "Ambiguous scope must return one focused question and authorized choices",
                    )
            probe.require(all(
                r["path"] == "/api/v1/self" or r["path"].endswith("/sites")
                for r in probe.requests()[before:]
            ), "Ambiguous or incomplete scope triggered telemetry")
    elif capability == "mist.failures":
        for scenario in ("device-error", "device-malformed"):
            with probe.session(scenario) as client:
                result = probe.call(client, "diagnose_site", org_id=ORG, site_id=SITE)
                probe.failure(result)
                probe.require(CLIENT_MAC in json.dumps(result.get("data")),
                              "Partial client evidence was discarded after device failure")
        for scenario in ("scope-error", "malformed", "rate-limit"):
            before = len(probe.requests())
            with probe.session(scenario) as client:
                probe.failure(probe.call(client, "diagnose_site", org_id=ORG, site_id=SITE))
            probe.require(all(r["path"] == "/api/v1/self" for r in probe.requests()[before:]),
                          "Failed or malformed identity lookup did not stop dependent reads")
    elif capability in {"mist.worker", "mist.notifications"}:
        probe.worker("offline")
        probe.worker("offline")
        probe.worker("unknown")
        probe.worker("pending")
        probe.worker("device-error", failure=True)
        with probe.session() as client:
            cases = probe.success(probe.call(client, "list_incidents", org_id=ORG, site_id=SITE))
            matching = [case for case in cases if case.get("device_id") in {AP_ID, AP_MAC}]
            probe.require(len(matching) == 1 and matching[0].get("status") == "open",
                          "Repeated/restarted/unknown observations duplicated or resolved an incident")
            incident_id = matching[0]["id"]
        probe.worker("online")
        with probe.session() as client:
            cases = probe.success(probe.call(client, "list_incidents", org_id=ORG, site_id=SITE))
            matching = [case for case in cases if case.get("id") == incident_id]
            probe.require(len(matching) == 1 and matching[0].get("status") == "resolved",
                          "Fresh positive recovery did not resolve the original incident")
            if capability == "mist.notifications":
                notes = probe.success(probe.call(client, "list_notifications", org_id=ORG, site_id=SITE))
                transitions = [n.get("transition") for n in notes if n.get("incident_id") == incident_id]
                probe.require(sorted(transitions) == ["opened", "resolved"],
                              "Outbox must retain exactly one opened and one resolved transition")
        probe.read_reached("/stats/devices")
        if capability == "mist.worker":
            for scenario in ("wrong-org", "scope-error", "credentials"):
                probe.worker(scenario, failure=True)
            probe.worker("online", continuous=True)
    elif capability == "mist.credentials":
        with probe.session("credentials") as client:
            probe.failure(probe.call(client, "diagnose_site", org_id=ORG, site_id=SITE))
        probe.require(not probe.requests(), "Credential-free invocation issued HTTP requests")
    else:
        raise AssertionError("No registered capability executor")


def mcp_requirement_evidence(
    product: ProductSpecV1 | None, receipt: dict[str, Any], project_dir: str | Path,
    build_id: str,
) -> dict[str, Any]:
    """Compile feature coverage for the same candidate used by the MCP receipt."""
    if product is None or not any(
        acceptance.startswith("mcp:capability:")
        for requirement in product.requirements
        for acceptance in requirement.acceptance_ids
    ):
        return {}
    from skyn3t.studio.requirement_trace import (
        ACCEPTANCE_REGISTRY_V1,
        compile_requirement_trace,
        requirement_evidence_binding,
    )

    source = source_tree_snapshot(project_dir)
    extra = {"mcp_check": receipt}
    run_id = uuid4().hex
    binding = requirement_evidence_binding(
        product, extra, source, acceptance_registry=ACCEPTANCE_REGISTRY_V1,
        build_id=build_id, evidence_run_id=run_id,
    )
    trace = compile_requirement_trace(
        product, extra, source, acceptance_registry=ACCEPTANCE_REGISTRY_V1,
        evidence_binding=binding, build_id=build_id, evidence_run_id=run_id,
        compiled_at=datetime.now(UTC).isoformat(),
    )
    return {"requirement_trace": trace, "requirement_evidence_binding": binding}


def check_mcp_capabilities(
    project_dir: str | Path, *, python_exec: str, tools: list[str],
    server_entrypoint: str | None = None, worker_entrypoint: str | None = None,
    capability_profile: str | None = None,
) -> dict[str, Any] | None:
    root = Path(project_dir)
    product = ProductSpecV1.load(root)
    required = {
        acceptance.removeprefix("mcp:capability:")
        for item in (product.requirements if product is not None else [])
        if item.priority == "must" and item.status not in {
            "archived", "canceled", "cancelled", "deferred", "rejected", "removed", "superseded",
        }
        for acceptance in item.acceptance_ids
        if acceptance.startswith("mcp:capability:")
    }
    profile = mcp_profile_name(product)
    if capability_profile is not None:
        if capability_profile != MIST_PROFILE or profile not in (None, capability_profile):
            raise ValueError("Unknown or conflicting explicit MCP capability profile")
        profile = capability_profile
        required.update(MIST_CAPABILITIES)
    if not required:
        return None
    source = source_tree_snapshot(root)
    from skyn3t.studio.mcp_check import _portability_issues

    portability = _portability_issues(root, source["files"]) if source.get("valid") else []
    allowed_tools = {tool for names in _TOOLS.values() for tool in names} | {
        "get_insight_metric_definitions",
    }
    cases: dict[str, dict[str, Any]] = {}
    for capability in sorted(required):
        with tempfile.TemporaryDirectory(prefix="skyn3t-mcp-capability-") as temporary:
            probe = _Probe(root, python_exec, Path(temporary), server_entrypoint, worker_entrypoint)
            try:
                probe.require(source.get("valid") is True, "Invalid authored source snapshot")
                probe.require(profile == MIST_PROFILE and capability in MIST_CAPABILITIES,
                              "No registered factory executor for required capability")
                probe.require(not (set(tools) - allowed_tools),
                              f"Uncontracted MCP tools exposed: {sorted(set(tools) - allowed_tools)}")
                missing = set(_TOOLS[capability]) - set(tools)
                probe.require(not missing, f"Missing required MCP tools: {sorted(missing)}")
                if capability == "mist.credentials":
                    probe.require(not portability, "; ".join(portability))
                _exercise(capability, probe)
                probe.requests()
            except Exception as exc:  # noqa: BLE001 - preserve a per-case failed receipt
                cases[capability] = {
                    "status": "failed", "assertions": probe.assertions,
                    "reason": f"{type(exc).__name__}: {exc}"[:1000],
                }
            else:
                cases[capability] = {"status": "passed", "assertions": probe.assertions, "reason": ""}
    after = source_tree_snapshot(root)
    if after.get("valid") is not True or after.get("sha256") != source.get("sha256"):
        for case in cases.values():
            case.update(status="failed", reason="Authored source changed during capability verification")
    return {
        "schema_version": 1,
        "profile": profile,
        "source_tree": source,
        "live_validation": "not_run",
        "vendor_response_conformance": "not_verified",
        "fixture_kind": "synthetic; provisional list shapes and opaque Insights/SLE payloads",
        "execution": "installed_console" if server_entrypoint else "source_launchers",
        "cases": cases,
    }
