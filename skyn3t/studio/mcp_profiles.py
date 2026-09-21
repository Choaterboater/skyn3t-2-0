"""Factory-owned MCP capability contracts, independent of generated tests."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord, ResearchSourceRecord

MIST_PROFILE = "mist-readonly-proactive-v1"
MIST_CAPABILITIES = {
    "mist.sites": "Discover all accessible sites with bounded pagination, including sites after page one.",
    "mist.inventory": "Read actual organization inventory, including unassigned APs, switches and gateways.",
    "mist.devices": "Expose current AP, switch and gateway statistics with type, status and page controls.",
    "mist.alarms": "Expose alarm search with absolute/relative windows and explicit server-provided continuation.",
    "mist.insights": "Return vendor client Insights without inventing metrics or root causes.",
    "mist.sle": "Return vendor site SLE summary with its actual metric and time-window parameters.",
    "mist.diagnosis": "Correlate real device/client/alarm/SLE evidence and return recommendations, never device writes.",
    "mist.scope": "Reject wrong-organization and missing scope before telemetry; handle membership after page one.",
    "mist.unknown": "Treat absent device status and failed observations as unknown, never healthy or recovered.",
    "mist.failures": "Preserve partial evidence and explicitly fail on malformed payloads and upstream failures.",
    "mist.worker": "Run without a chat host; persist, deduplicate and freshly resolve incidents across worker restarts.",
    "mist.notifications": "Persist one notification per incident transition, readable through MCP without sending external writes.",
    "mist.credentials": "Start and list tools without credentials; authenticated operations must fail without making HTTP calls.",
}

MIST_INTERFACE_CONTRACT = """
INTERFACE AND IMPLEMENTATION CONTRACT
Use the official MCP Python SDK 2.2 MCPServer, injectable httpx, and a regular
mist_mcp package. Ship root server.py and worker.py launchers, installable wheel,
console entrypoints mist-mcp=mist_mcp.server:main and mist-watch=mist_mcp.worker:main.
No web UI. Tests must use synthetic HTTP fixtures, never a tenant or demo mode.
Configuration: MIST_API_TOKEN, MIST_BASE_URL (default https://api.mist.com),
MIST_ORG_ID, MIST_DB_PATH (SQLite; share between MCP and standalone worker).
Always add /api/v1 exactly once. Authorization is Token <token>. No tokens in
arguments, logs, errors or output. Import/start/list without credentials must work.
Do not start the polling worker implicitly when starting the MCP server.
Use declared dependencies in the selected interpreter. Never inject a developer
machine's site-packages or user site into sys.path to make imports pass.
Do not describe synthetic-only evidence as production-grade or production-proven.

Every tool returns a JSON object with success:boolean, data (actual records/raw
JSON), errors:list, and coverage:{complete:boolean,limitations:list}. Errors and
partial evidence must never imply healthy/complete. Lists stay lists, not canned
summaries. Preserve upstream identifiers, unknown fields, observation times and
per-source provenance. For opaque vendor schemas say schema_verified=false;
successful retrieval is NOT validated interpretation or vendor conformance.

MCP tools (retain these public controls; extra optional parameters are allowed):
- list_org_sites(org_id,limit=100,page=1): one explicit page, with coverage metadata.
- list_sites(org_id): bounded automatic page traversal; return all 250 sites in a
  250-site fixture, not just the first page. Stop safely on repeated/nonadvancing
  pages, inconsistent headers or request-budget exhaustion and report partial.
- get_org_inventory(org_id,device_type="ap",unassigned=true,limit=100,page=1):
  real org assets, NOT site statistics. Preserve explicit unassigned=false.
- get_site_device_stats(site_id,device_type="ap",status="all",limit=100,page=1,
  org_id=None): optional org_id defaults to configured MIST_ORG_ID; supports type
  ap/switch/gateway as raw read candidates, not full wired/WAN diagnostic claims.
- get_site_inventory(org_id,site_id,device_type="ap"): current site telemetry,
  clearly labelled as such, never presented as organization asset inventory.
- search_site_alarms(site_id,start=None,end=None,duration="1d",limit=100,
  search_after=None,org_id=None,group=None,severity=None,acked=None):
  return results as data:list and coverage.next_cursor extracted from next's
  search_after query value, not the whole URL. Preserve false boolean filters.
- get_client_insights(org_id,site_id,client_mac,metrics,start=None,end=None,
  duration="1d",interval=None,limit=100,page=1): metrics is a REQUIRED comma-separated
  query string. Retain opaque vendor response JSON without guessed classifiers.
- list_site_sle_metrics(org_id,site_id): retain discovery response raw.
- get_site_sle_summary(org_id,site_id,metric,start=None,end=None,duration="1d"):
  use summary-trend, retain opaque JSON; do not substitute the legacy /summary.
- diagnose_site(org_id=None,site_id=None,start=None,end=None,duration="1d",
  time_window_hours=None): normalize hours as a relative-window compatibility
  alias; explicit paired bounds override the default duration. Collect current
  AP/switch/gateway stats, client stats, alarm evidence and a site SLE trend
  (time-to-connect is a candidate, not a guaranteed supported metric). Use real
  matching identifiers to correlate client/AP/alarms. data must include health
  (healthy/degraded/unknown), findings, recommendations with applied=false, and
  evidence keyed by source. Unknown AP status or missing required evidence must
  prevent aggregate healthy. Historical alarms must be temporally qualified.
  Omitted scope uses explicit/configured values, then authorized discovery.
  Automatically use a single unambiguous org/site ONLY after complete discovery.
  If multiple orgs/sites remain, return success=false, incomplete coverage and
  data:{question:string,choices:list} with authorized org_id/site_id choices;
  make no telemetry requests until resolved. Incomplete discovery never proves
  a unique scope. Keep missing/malformed identity an explicit failure.
  Successful diagnosis includes data.scope:{org_id,site_id} and
  data.next_actions:[{tool,arguments}] referencing existing tools with that scope
  and the compatible time window carried forward. No global last-selected site;
  the host carries the returned explicit context into subsequent calls.
- list_incidents(org_id,site_id,status="all"): read the local incident store.
- list_notifications(org_id,site_id): read the local transition outbox.
Local incident/outbox reads do not need cloud HTTP and must identify their scope
and historical/local provenance. Do not infer current health from stored cases.
Do not expose extra uncontracted tools (especially write/approval tools); the
optional get_insight_metric_definitions read is the sole additional tool allowed.
Retain resources mist://system/info and mist://orgs/{org_id}/sites, sharing the
same paginated site reader and authorization checks rather than duplicate logic.
System info must explain safe credential setup, configured scope, read-only
capabilities and unavailable elevation. No secret values. Document a bounded
read-only connection/permission check through the existing interface.

Verified REQUEST interfaces from the separately MIT-licensed mistapi 0.64.0
wrapper commit 66a57a64f2cae222da36c3a45a639c2a70c54156 (do NOT copy implementation):
All GET, under /api/v1. The allowed requests are:
/self (no query): privileges is provisionally a list of scoped grants.
/orgs/{org_id}/sites: limit,page; defaults 100,1.
/orgs/{org_id}/inventory: type,unassigned,limit,page; type ap/switch/gateway;
  unassigned=true means DISPLAY unassigned too, not 'only unassigned'.
/sites/{site_id}/stats/devices: type,status,limit,page only; no time window.
/sites/{site_id}/stats/clients: wired,limit,start,end OR duration; NO page/cursor.
/sites/{site_id}/insights/client/{client_mac}: required metrics; start,end OR
  duration; optional interval,limit,page. NEVER add /{metric} to this path.
/sites/{site_id}/sle/site/{site_id}/metrics: no query parameters.
/sites/{site_id}/sle/site/{site_id}/metric/{metric}/summary-trend:
  start,end OR duration only; NO interval,limit,page.
/sites/{site_id}/alarms/search: start,end OR duration; limit,sort,search_after,
  group,severity,type,ack_admin_name,acked. group=marvis is supported.
  severity is critical/info/warn; sort=-timestamp requests descending order.
/const/insight_metrics: optional metric-definition discovery, no query.
Preserve false values. Paired start<end OR duration is our LOCAL consistency
rule; vendor precedence is not established. No undocumented paging arguments.
Page headers are X-Page-Total (record count), X-Page-Limit, X-Page-Page. Without
headers, a short page may terminate list traversal; at a limit report uncertainty.
Alarm next is a continuation URL: validate host, exact route, org/site scope,
filters and fixed window before extracting its opaque search_after value. Never
follow an arbitrary next URL or manufacture a cursor; reject repeated cursors.

Scope before telemetry: /self grants must MATCH org_id, not merely scope="org".
An exact site-scoped grant may authorize that site, never all org inventory.
Unsupported/malformed privilege shapes fail explicitly. For org grants check site
membership across bounded org-sites pagination, including a site on page three.
Budget exhaustion means scope unknown/incomplete, NOT unauthorized/not-found.
Reject wrong org before any org/site data request. Do not silently accept malformed
list/envelope responses as empty success. Keep HTTP timeouts/retries finite; no
401/403 retries. Use a finite per-workflow read/page budget; no HTTP writes.
The full current schemas for Insights/SLE, switch/gateway telemetry and REST
alarm recovery semantics are NOT established: retain raw, explicitly qualified
observations; do not claim synthetic fixtures prove vendor response conformance.

PROMPTLESS WORKER
worker.py / mist-watch --org-id ID --site-id ID --once performs one real bounded
observation cycle and exits; without --once runs continuously using --interval
(positive seconds), independent of MCP or any chat host. Graceful shutdown and
clear errors; missing credentials must exit nonzero without HTTP. Exit zero for a
processed offline/unknown observation, not just healthy data. Use normal
configuration and injectable transport, no fixture/probe-specific runtime branches.
Use transactional SQLite incidents plus notification outbox. An observed
disconnected AP opens an incident keyed by organization/site/device/kind; repeat
observations and process restarts update that incident, not duplicate it.
Each incident includes id,org_id,site_id,device_id,status(open/resolved),
evidence and observation timestamps. Notifications include id,incident_id and
transition(opened/resolved); emit each transition once, retained across restarts.
Fresh connected status can resolve an AP connectivity incident ONLY after a
successful required observation set. Missing status, missing device, stale data,
empty/partial pages and HTTP errors do NOT resolve it. Alarm disappearance is
NOT recovery; unresolved vendor alarms remain explicitly unverified.
Output transition notifications as structured JSON on worker stdout and persist
them for list_notifications; external email/Slack delivery is deferred.
Keep local hypotheses distinct from vendor findings. No proprietary AI claim.
Document daemon/service deployment, configuration, source references, scope,
synthetic verification and outstanding live-tenant validation.
"""


def mcp_profile_instructions(name: str) -> str:
    if name != MIST_PROFILE:
        raise ValueError(f"Unknown MCP capability profile: {name}")
    verifier = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r}); "
        "import json; from skyn3t.studio.mcp_check import check_mcp; "
        f"r=check_mcp('.', 'mcp', capability_profile={name!r}); "
        "print(json.dumps(r.to_dict(), indent=2)); raise SystemExit(0 if r.ok else 1)"
    )
    return (
        "MANDATORY MCP CAPABILITY PROFILE: " + name + "\n"
        "These requirements are independently exercised by the factory, not advisory skills.\n"
        + "\n".join(f"- {key}: {text}" for key, text in MIST_CAPABILITIES.items())
        + "\nDo not remove requirements, modify the product contract, weaken tests, "
        "recognize verification fixtures, or substitute successful startup for feature coverage.\n"
        + MIST_INTERFACE_CONTRACT
        + "\nBUILD-TIME FEEDBACK (not an application dependency):\n"
        "When working agentically, run the following from YOUR CANDIDATE WORKTREE "
        "after your project tests. Read the actual failed cases, fix their root causes, "
        "and rerun before finishing. This also works before delivery metadata exists.\n"
        + shlex.quote(sys.executable) + " -c " + shlex.quote(verifier) + "\n"
        "Do not edit the factory verifier, copy it into the application, import it at "
        "application runtime, or special-case its synthetic inputs. The factory will "
        "independently rerun it after you finish.\n"
    )


def mcp_profile_product(name: str, project_id: str, brief: str) -> ProductSpecV1:
    if name != MIST_PROFILE:
        raise ValueError(f"Unknown MCP capability profile: {name}")
    return ProductSpecV1(
        project_id=project_id,
        goal=brief,
        acceptance_registry_version=1,
        requirements=[
            RequirementRecord(
                text=text, acceptance_ids=[f"mcp:capability:{capability}"],
                source="mcp_profile",
                provenance={"mcp_profile": name, "version": 1},
            )
            for capability, text in MIST_CAPABILITIES.items()
        ],
        non_goals=[
            "No device writes, configuration changes, automatic remediation or live tenant calls during generation/tests.",
            "No claim of proprietary Marvis intelligence, complete vendor parity or production/live-tenant validation.",
            "No restricted mistsys/mist_openapi input, copied SDK implementation or probe-specific behavior.",
        ],
        architecture_decisions=[
            "Shared injectable httpx adapter, pure diagnosis and a SQLite-backed standalone polling worker.",
            "Required capability evidence is factory-owned and cannot be substituted by a generic passing test suite.",
        ],
        research_sources=[
            ResearchSourceRecord(
                url="https://www.juniper.net/documentation/us/en/software/mist/"
                    "automation-integration/topics/concept/api-mist-metrics.html",
                ideas=["Use vendor-defined metrics; distinguish discovery definitions from measurement schemas."],
            ),
            ResearchSourceRecord(
                url="https://www.juniper.net/documentation/us/en/software/mist/"
                    "automation-integration/topics/concept/rest-api-pagination.html",
                ideas=["Use documented paging and continuation; report incomplete collection explicitly."],
            ),
            ResearchSourceRecord(
                url="https://github.com/tmunzer/mistapi_python/tree/"
                    "66a57a64f2cae222da36c3a45a639c2a70c54156/src/mistapi/api/v1",
                repository="tmunzer/mistapi_python",
                commit="66a57a64f2cae222da36c3a45a639c2a70c54156",
                license="MIT", usage_policy="patterns_allowed",
                ideas=["Use permitted wrapper interfaces as references, not copied implementation."],
            ),
            ResearchSourceRecord(
                url="mcp://hpe-network-mcp/mist-core",
                usage_policy="idea_only",
                ideas=["Curated inventory, client Insights and SLE tool organization; catalog metadata is not behavioral proof."],
            ),
        ],
    )


def mcp_profile_name(product: ProductSpecV1 | dict[str, Any] | None) -> str | None:
    if product is None:
        return None
    value = product.to_dict() if isinstance(product, ProductSpecV1) else product
    profiles = {
        item.get("provenance", {}).get("mcp_profile")
        for item in value.get("requirements", [])
        if isinstance(item, dict) and isinstance(item.get("provenance"), dict)
        and item["provenance"].get("mcp_profile")
    }
    if not profiles:
        return None
    if profiles != {MIST_PROFILE}:
        raise ValueError(f"Unsupported or conflicting MCP capability profiles: {sorted(profiles)}")
    return MIST_PROFILE
