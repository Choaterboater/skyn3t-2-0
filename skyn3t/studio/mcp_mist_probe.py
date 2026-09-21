"""Synthetic HTTP boundary for the Mist contract probe; never a vendor emulator.

Run only in a child interpreter. Python network/process operations are blocked;
this is diagnostic isolation, not an OS security sandbox.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import runpy
import sys
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ORG = "11111111-1111-4111-8111-111111111111"
OTHER_ORG = "22222222-2222-4222-8222-222222222222"
SITE = "00000000-0000-4000-8000-000000000201"
AP_ID = "33333333-3333-4333-8333-333333333333"
AP_MAC = "aabbccddeeff"
CLIENT_MAC = "112233445566"
TOKEN = "synthetic-contract-credential"
BASE = "https://api.mist.com"


def main() -> None:
    import httpx

    config_path, script, *arguments = sys.argv[1:]
    config = json.loads(Path(config_path).read_text())
    os.environ.update(config["environment"])
    scenario = config["scenario"]
    receipt = Path(config["receipt"])
    calls = 0

    def record(value: dict) -> None:
        with receipt.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    def respond(request):
        nonlocal calls
        calls += 1
        path = request.url.path
        params = dict(request.url.params)
        record({"kind": "http", "method": request.method, "path": path, "params": params})
        if calls > 300:
            raise RuntimeError("Contract probe request budget exceeded")
        assert request.method == "GET", "Network writes are forbidden"
        assert str(request.url).startswith(BASE + "/api/v1/"), "Unexpected API host/prefix"
        assert request.headers.get("authorization") == "Token " + TOKEN, "Invalid Token authorization"
        if path == "/api/v1/self":
            assert not params, "/self takes no query parameters"
            if scenario == "scope-error":
                return httpx.Response(503, json={"error": "synthetic identity failure"}, request=request)
            if scenario == "rate-limit":
                return httpx.Response(429, json={"error": "synthetic rate limit"},
                                      headers={"Retry-After": "Infinity"}, request=request)
            if scenario == "malformed":
                return httpx.Response(200, json={"privileges": "not-a-list"}, request=request)
            privilege = {"scope": "org", "role": "read", "org_id": ORG}
            if scenario == "scope-multiple-orgs":
                return httpx.Response(200, json={"privileges": [
                    privilege, {**privilege, "org_id": OTHER_ORG},
                ]}, request=request)
            if scenario == "wrong-org":
                privilege["org_id"] = OTHER_ORG
            if scenario in {"site-only", "site-admin", "site-wrong-org"}:
                privilege.update(scope="site", site_id=SITE)
                if scenario == "site-admin":
                    privilege["role"] = "admin"
                if scenario == "site-wrong-org":
                    privilege["org_id"] = OTHER_ORG
            return httpx.Response(200, json={"privileges": [privilege]}, request=request)
        allowed = {
            f"/api/v1/orgs/{ORG}/sites": {"limit", "page"},
            f"/api/v1/orgs/{ORG}/inventory": {"type", "unassigned", "limit", "page"},
            f"/api/v1/sites/{SITE}/stats/devices": {"type", "status", "limit", "page"},
            f"/api/v1/sites/{SITE}/stats/clients": {"wired", "limit", "start", "end", "duration"},
            f"/api/v1/sites/{SITE}/alarms/search": {
                "start", "end", "duration", "limit", "sort", "search_after",
                "group", "severity", "type", "ack_admin_name", "acked",
            },
            f"/api/v1/sites/{SITE}/insights/client/{CLIENT_MAC}": {
                "metrics", "start", "end", "duration", "interval", "limit", "page",
            },
            f"/api/v1/sites/{SITE}/sle/site/{SITE}/metrics": set(),
            f"/api/v1/sites/{SITE}/sle/site/{SITE}/metric/time-to-connect/summary-trend": {
                "start", "end", "duration",
            },
            "/api/v1/const/insight_metrics": set(),
        }
        assert path in allowed, f"Undocumented or wrong-scope path: {path}"
        assert set(params) <= allowed[path], f"Unsupported parameters for {path}: {params}"
        assert ("start" in params) == ("end" in params), "Both absolute bounds are required"
        assert not ("duration" in params and "start" in params), "Mixed absolute/relative windows"
        if "start" in params:
            assert float(params["start"]) < float(params["end"]), "Invalid time window"
        headers = {}
        if path.endswith("/sites"):
            limit, page = int(params.get("limit", 100)), int(params.get("page", 1))
            assert 1 <= limit <= 1000 and page >= 1
            values = [{
                "id": f"00000000-0000-4000-8000-{index:012d}", "org_id": ORG,
                "name": f"Synthetic site {index}",
            } for index in range(1, 251)]
            if scenario in {"scope-single", "scope-incomplete"}:
                values = [{"id": SITE, "org_id": ORG, "name": "Synthetic only site"}]
            body = values[(page - 1) * limit:page * limit]
            if scenario == "repeated-sites":
                body = values[:limit]
            total = 250 if scenario == "scope-incomplete" else len(values)
            headers = {"X-Page-Total": str(total), "X-Page-Limit": str(limit), "X-Page-Page": str(page)}
        elif path.endswith("/inventory"):
            kind = params.get("type", "ap")
            assert kind in {"ap", "switch", "gateway"}
            flag = params.get("unassigned", "true").lower()
            assert flag in {"true", "false"}, "unassigned must retain its boolean value"
            body = [{
                "id": "asset-" + kind, "serial": "SYNTHETIC-" + kind.upper(),
                "type": kind, "org_id": ORG, "site_id": None if flag == "true" else SITE,
                "mac": "aabbccddee00", "model": "synthetic-contract-model",
            }]
            headers = {"X-Page-Total": "1", "X-Page-Limit": params.get("limit", "100"),
                       "X-Page-Page": params.get("page", "1")}
        elif path.endswith("/stats/devices"):
            kind = params.get("type", "ap")
            assert kind in {"ap", "switch", "gateway"}
            if scenario == "device-error":
                return httpx.Response(503, json={"error": "synthetic device failure"}, request=request)
            if scenario == "device-malformed":
                return httpx.Response(200, json={"not": "a device list"}, request=request)
            device = {
                "id": AP_ID if kind == "ap" else "device-" + kind,
                "mac": AP_MAC if kind == "ap" else "aabbccddee01",
                "site_id": SITE, "type": kind, "status": "connected", "last_seen": time.time(),
            }
            if kind == "ap" and scenario in {"offline", "unknown"}:
                device["status"] = "disconnected"
            if kind == "ap" and scenario == "unknown":
                device.pop("status")
            if kind == "ap" and scenario == "pending":
                device["status"] = "pending"
            body = [device]
            headers = {"X-Page-Total": "1", "X-Page-Limit": params.get("limit", "100"),
                       "X-Page-Page": params.get("page", "1")}
        elif path.endswith("/stats/clients"):
            body = [{"mac": CLIENT_MAC, "ap_mac": AP_MAC, "rssi": -51, "site_id": SITE}]
            if scenario == "clients-opaque":
                body = {"unverified_client_shape": {"client_id": CLIENT_MAC}}
        elif path.endswith("/alarms/search"):
            body = {"results": [], "next": None}
            if scenario in {"alarm-pages", "alarm-host"}:
                body["results"] = [{
                    "id": "alarm-2" if params.get("search_after") else "alarm-1",
                    "timestamp": time.time() - 10, "severity": "warn",
                    "group": "marvis", "type": "synthetic-contract-alarm", "ap_mac": AP_MAC,
                }]
                if not params.get("search_after"):
                    query = {**params, "search_after": "opaque+/cursor=="}
                    host = BASE if scenario == "alarm-pages" else "https://untrusted.invalid"
                    body["next"] = host + path + "?" + urlencode(query)
        elif "/insights/client/" in path:
            assert params.get("metrics"), "Client Insights requires the metrics query parameter"
            body = {"synthetic_unverified_series": [17, 31], "requested_metrics": params["metrics"]}
        elif path.endswith("/metrics"):
            body = ["time-to-connect"]
        elif path.endswith("/summary-trend"):
            body = {"synthetic_unverified_trend": [23, 47], "metric": "time-to-connect"}
        else:
            body = {"top-app-by-bytes": {"scopes": ["client"], "type": "list"}}
        return httpx.Response(200, json=body, headers=headers, request=request)

    def send(self, request, **kwargs):
        return respond(request)

    async def send_async(self, request, **kwargs):
        return respond(request)

    def block_network(event: str, args: tuple) -> None:
        if event in {
            "socket.connect", "socket.getaddrinfo", "socket.sendto",
            "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
        }:
            record({"kind": "blocked_operation", "operation": event})
            raise OSError("External I/O is blocked by the offline contract probe")

    sys.addaudithook(block_network)
    entrypoint = config.get("entrypoint")
    with patch.object(httpx.Client, "send", send), patch.object(httpx.AsyncClient, "send", send_async):
        if entrypoint:
            sys.argv = [entrypoint, *arguments]
            matches = list(importlib.metadata.entry_points(group="console_scripts", name=entrypoint))
            if len(matches) != 1:
                raise RuntimeError(f"Expected one installed console entrypoint: {entrypoint}")
            matches[0].load()()
        else:
            target = Path(script).resolve()
            sys.path.insert(0, str(target.parent))
            sys.argv = [str(target), *arguments]
            runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
