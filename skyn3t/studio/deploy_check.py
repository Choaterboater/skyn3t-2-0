"""Ship pillar — the `deploy_check` gate: verify a build at its LIVE url.

The verify ladder proves a build locally; once it's really deployed (via
`skyn3t deploy --now`), this re-runs the same liveness probe against the deployed
URL instead of localhost. It reuses the liveness HTTP helpers verbatim — the only
difference from the in-build liveness gate is that we skip all local boot
(`AppRunner`) and feed it the remote base URL.

Never raises. An UNREACHABLE url is `skipped` (could-not-verify, e.g. transient
network / DNS not yet propagated). Callers can distinguish that from a positive
health verdict and decline to activate an unverified deployment. A reachable URL
that serves errors IS reported as issues.
"""

from __future__ import annotations

import asyncio

from skyn3t.studio.gate_verdict import GateVerdict


async def check_deploy(base_url: str, stack: str = "") -> GateVerdict:
    """Probe a live deploy: the root must be routed, and any same-origin routes it
    links to must be alive. Returns a :class:`GateVerdict` (advisory)."""
    base = str(base_url or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        return GateVerdict(skipped=True, reason="no deploy url to check")

    try:
        from skyn3t.core.stacks import UI_WEB_STACKS
        from skyn3t.studio.liveness import _hit, _wired, check_liveness, crawl_routes
    except Exception as exc:  # noqa: BLE001 - a missing dep must not break delivery
        return GateVerdict(skipped=True, reason=f"deploy check unavailable: {exc}")

    root_status = await asyncio.to_thread(_hit, base + "/", "GET")
    if root_status == 0:
        # Unreachable — do NOT flag (DNS may still be propagating / transient).
        return GateVerdict(skipped=True, checked={"url": base},
                           reason="deploy url unreachable — could not verify (retry shortly)")

    checked: dict = {"url": base, "root_status": root_status}
    issues: list[str] = []
    # Every deployed HTTP service must avoid a server error at '/'. API stacks
    # may legitimately return a routed 4xx (commonly 404) there, while UI stacks
    # still need a rendered/wired root. This explicit 5xx check matters when the
    # broken root exposes no links and the route crawler therefore finds nothing.
    is_ui_stack = (stack or "").strip().lower() in UI_WEB_STACKS
    if root_status >= 500 or (is_ui_stack and not _wired(root_status)):
        issues.append(f"root {base}/ returned {root_status}")

    # Best-effort: probe the same-origin routes the live root links to.
    try:
        routes = await crawl_routes(base)
        if routes:
            report = await check_liveness(base, routes)
            checked.update({"routes": report.total, "routes_ok": report.ok,
                            "routes_dead": report.dead})
            issues.extend(f"dead route {p}" for p in report.dead_routes)
    except Exception:  # noqa: BLE001 - route crawl is a bonus, never fatal
        pass

    return GateVerdict(
        skipped=False, issues=issues, checked=checked,
        reason="live deploy verified" if not issues
        else f"live deploy has {len(issues)} issue(s)",
    )
