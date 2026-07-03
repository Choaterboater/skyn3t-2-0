"""Web layer shared state, dependencies, and auth.

This module imports with **zero side effects** and **no FastAPI dependency** so
it can be used (and unit-tested) even when ``fastapi``/``uvicorn`` are absent.

The :class:`AppState` object is the single wiring point between the HTTP/WS
surface and the SkyN3t spine (event bus, orchestrator, memory, LLM client,
router). It also owns lightweight in-memory registries for *builds* and
*proposals* so the API is fully functional in a degraded, dependency-free mode
(design rule #6: degrade, don't crash; rule #1: delivered != empty).

Trajectory replay / time-travel (2.0 backlog P2, backend hooks only) is served
here: every state captures the event bus, whose bounded history *is* the
trajectory. :meth:`AppState.trajectory` exposes a replayable, filterable,
seekable view over that history.
"""

from __future__ import annotations

import ipaddress
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import Event, EventBus, EventType

# ---------------------------------------------------------------------------
# Optional spine collaborators. Everything here is optional so the web layer
# imports and runs even before sibling packages exist.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - depends on sibling package availability
    from skyn3t.core.orchestrator import Orchestrator
except Exception:  # noqa: BLE001
    Orchestrator = None  # type: ignore[assignment,misc]

try:  # pragma: no cover - depends on optional httpx-backed client
    from skyn3t.adapters.llm import LLMClient
except Exception:  # noqa: BLE001
    LLMClient = None  # type: ignore[assignment,misc]

try:  # pragma: no cover
    from skyn3t.core.model_router import ModelRouter
except Exception:  # noqa: BLE001
    ModelRouter = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Lightweight in-memory records (used as the degraded fallback and as the live
# cache that always reflects the most recent activity).
# ---------------------------------------------------------------------------
@dataclass
class BuildRecord:
    build_id: str
    brief: str
    slug: str = ""
    stack: str = ""
    app_type: str = ""
    engine: str = ""
    stack_selection: dict[str, Any] = field(default_factory=dict)
    classification: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    score: float | None = None
    verdict: str = ""
    cost_usd: float = 0.0
    build_profile: str = ""
    model_trace: dict[str, Any] = field(default_factory=dict)
    quality_scorecard: dict[str, Any] = field(default_factory=dict)
    skills_used: list[str] = field(default_factory=list)
    recall_used: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "brief": self.brief,
            "slug": self.slug,
            "stack": self.stack,
            "app_type": self.app_type,
            "engine": self.engine,
            "stack_selection": self.stack_selection,
            "classification": self.classification,
            "status": self.status,
            "score": self.score,
            "verdict": self.verdict,
            "cost_usd": self.cost_usd,
            "build_profile": self.build_profile,
            "model_trace": self.model_trace,
            "quality_scorecard": self.quality_scorecard,
            "skills_used": self.skills_used,
            "recall_used": self.recall_used,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "correlation_id": self.correlation_id,
        }


@dataclass
class ProposalRecord:
    proposal_id: str
    kind: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | approved | rejected
    decided_by: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "id": self.proposal_id,  # SPA reads p.id for the decide URL + React key
            "kind": self.kind,
            # SPA renders p.title prominently; surface the real proposal title.
            "title": self.payload.get("title", "") or self.summary,
            "summary": self.summary,
            "payload": self.payload,
            "status": self.status,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
        }


class AppState:
    """Holds every collaborator the routes and websockets need.

    All collaborators are optional. When one is missing the corresponding
    endpoints degrade to deterministic, offline behavior rather than failing.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        event_bus: EventBus | None = None,
        orchestrator: Any | None = None,
        llm_client: Any | None = None,
        router: Any | None = None,
        memory: Any | None = None,
        studio: Any | None = None,
        cortex: Any | None = None,
        knowledge: Any | None = None,
        skills: Any | None = None,
        patterns: Any | None = None,
        messaging: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.event_bus = event_bus or EventBus()
        self.orchestrator = orchestrator
        self.memory = memory
        self.studio = studio
        self.cortex = cortex
        self.knowledge = knowledge
        self.skills = skills
        self.patterns = patterns
        self.messaging = messaging

        if llm_client is None and LLMClient is not None:
            try:  # pragma: no cover - construction is cheap and offline-safe
                llm_client = LLMClient(self.settings)
            except Exception:  # noqa: BLE001
                llm_client = None
        self.llm_client = llm_client

        if router is None:
            router = getattr(llm_client, "router", None)
        if router is None and ModelRouter is not None:
            try:  # pragma: no cover
                router = ModelRouter(self.settings)
            except Exception:  # noqa: BLE001
                router = None
        self.router = router

        # In-memory caches (always present).
        self.builds: dict[str, BuildRecord] = {}
        self.proposals: dict[str, ProposalRecord] = {}

        # Live preview servers (Spec 3 two-pane workspace): slug -> RunningApp,
        # plus a shared AppRunner. Populated lazily by the serve endpoints.
        self.running_apps: dict[str, Any] = {}
        self.app_runner: Any | None = None

        # Mirror cortex proposals into the cache as they are created.
        self._wire_proposal_capture()

    def stop_all_serves(self) -> None:
        """Stop every live preview server and release its child + logfile.

        Best-effort, never raises — call on dashboard shutdown so detached
        preview processes don't outlive the host."""
        runner = self.app_runner
        if runner is None:
            return
        try:
            from skyn3t.studio.app_runner import cleanup_serve
        except Exception:  # noqa: BLE001 - studio optional
            cleanup_serve = None  # type: ignore[assignment]
        for slug, app in list(self.running_apps.items()):
            try:
                runner.stop(app)
                if cleanup_serve is not None:
                    cleanup_serve(app)
            except Exception:  # noqa: BLE001 - shutdown must not crash
                pass
            self.running_apps.pop(slug, None)

    # ---- event helpers ---------------------------------------------------
    def _wire_proposal_capture(self) -> None:
        async def _on_proposal(ev: Event) -> None:  # pragma: no cover - async wiring
            pid = str(ev.payload.get("proposal_id") or ev.id)
            if pid not in self.proposals:
                # PROPOSAL_CREATED carries type/title (not kind/summary); map them
                # so the dashboard shows the real proposal instead of "generic".
                p = ev.payload
                self.proposals[pid] = ProposalRecord(
                    proposal_id=pid,
                    kind=str(p.get("kind") or p.get("type") or "proposal"),
                    summary=str(p.get("summary") or p.get("title") or ""),
                    payload=dict(p),
                )

        async def _on_proposal_decided(ev: Event) -> None:  # pragma: no cover - async wiring
            # Keep the cache honest. The cortex emits PROPOSAL_DECIDED for every
            # gate / approve / reject / apply AND for store-rejected duplicates.
            # Without this, every proposal stayed "pending" forever, so the
            # dashboard's pending count inflated with duplicates + decided items.
            pid = str(ev.payload.get("proposal_id") or ev.id)
            rec = self.proposals.get(pid)
            if rec is None:
                return
            payload = ev.payload or {}
            # Two emitter shapes: the web API sends {"approved": bool}; the cortex
            # sends {"status": "<ProposalStatus>"}. Handle both; never clobber on
            # an unrecognized payload (leave the prior status intact).
            if "approved" in payload:
                rec.status = "approved" if payload.get("approved") else "rejected"
                return
            raw = str(payload.get("status", "")).lower()
            if raw in ("approved", "applied"):
                rec.status = "approved"
            elif raw in ("rejected", "failed"):
                rec.status = "rejected"
            elif raw in ("gated", "pending"):
                rec.status = "pending"  # genuinely awaiting a human decision

        async def _on_build(ev: Event) -> None:  # pragma: no cover - async wiring
            bid = str(ev.payload.get("build_id") or ev.correlation_id or ev.id)
            rec = self.builds.get(bid)
            if rec is None:
                rec = BuildRecord(build_id=bid, brief=str(ev.payload.get("brief", "")))
                self.builds[bid] = rec
            # Identity fields may arrive on any build event.
            for k in ("slug", "stack", "app_type", "engine", "build_profile"):
                if ev.payload.get(k):
                    setattr(rec, k, str(ev.payload[k]))
            for k in ("stack_selection", "classification", "model_trace", "quality_scorecard"):
                if isinstance(ev.payload.get(k), dict):
                    setattr(rec, k, dict(ev.payload[k]))
            for k in ("skills_used", "recall_used"):
                if isinstance(ev.payload.get(k), list):
                    setattr(rec, k, list(ev.payload[k]))
            if "cost_usd" in ev.payload:
                try:
                    rec.cost_usd = float(ev.payload["cost_usd"])
                except (TypeError, ValueError):
                    pass
            # Build status is driven ONLY by build-level events. Stage events
            # carry the *stage's* status/verdict/score — never copy those onto
            # the build record (that was flipping builds to "completed" early).
            if ev.type == EventType.BUILD_STARTED:
                rec.status = "running"
            elif ev.type == EventType.BUILD_COMPLETED:
                rec.status = str(ev.payload.get("status") or "completed")
                if ev.payload.get("verdict"):
                    rec.verdict = str(ev.payload["verdict"])
                if "score" in ev.payload:
                    try:
                        rec.score = float(ev.payload["score"])
                    except (TypeError, ValueError):
                        pass
            elif ev.type == EventType.BUILD_FAILED:
                rec.status = "failed"
            rec.updated_at = time.time()

        self.event_bus.subscribe(EventType.PROPOSAL_CREATED, _on_proposal)
        self.event_bus.subscribe(EventType.PROPOSAL_DECIDED, _on_proposal_decided)
        for et in (
            EventType.BUILD_STARTED,
            EventType.BUILD_STAGE_STARTED,
            EventType.BUILD_STAGE_COMPLETED,
            EventType.BUILD_COMPLETED,
            EventType.BUILD_FAILED,
        ):
            self.event_bus.subscribe(et, _on_build)

    # ---- snapshots used by /api/status -----------------------------------
    def status(self) -> dict[str, Any]:
        s = self.settings
        return {
            "app": s.app_name,
            "version": s.version,
            "ok": True,
            "events_published": self.event_bus.published_count,
            "agents": len(self.orchestrator.agents) if self.orchestrator else 0,
            "builds": len(self.builds),
            "proposals_pending": sum(
                1 for p in self.proposals.values() if p.status == "pending"
            ),
            "backends": {
                "orchestrator": self.orchestrator is not None,
                "llm": self.llm_client is not None,
                "router": self.router is not None,
                "memory": self.memory is not None,
                "studio": self.studio is not None,
                "cortex": self.cortex is not None,
                "knowledge": self.knowledge is not None,
            },
            "policy": {
                "free_only": s.free_only,
                "no_claude": s.no_claude,
                "autonomous_builds": s.autonomous_builds,
                "approval_gates": s.approval_gates,
                "has_any_llm": s.has_any_llm,
                "claude_available": s.claude_available,
                # The build KNOWS it can generate real images when a Replicate
                # token is set; the asset-gen step additionally needs asset_gen.
                "replicate_available": getattr(s, "replicate_available", False),
                "asset_gen": s.asset_gen,
                "visual_self_heal": s.visual_self_heal,
                "app_type_override": s.app_type_override,
                "engine_override": s.engine_override,
            },
        }

    def agents_snapshot(self) -> list[dict[str, Any]]:
        if not self.orchestrator:
            return []
        out: list[dict[str, Any]] = []
        for name, agent in self.orchestrator.agents.items():
            out.append(
                {
                    "name": name,
                    "type": getattr(agent, "agent_type", ""),
                    "provider": getattr(agent, "provider", ""),
                    "status": getattr(getattr(agent, "status", None), "value", str(getattr(agent, "status", ""))),
                    "capabilities": [
                        getattr(c, "name", str(c))
                        for c in getattr(agent, "capabilities", {}).values()
                    ]
                    if isinstance(getattr(agent, "capabilities", None), dict)
                    else [],
                }
            )
        return out

    def llm_backends(self) -> dict[str, Any]:
        status = {}
        if self.llm_client is not None and hasattr(self.llm_client, "backend_status"):
            try:
                status = self.llm_client.backend_status()
            except Exception:  # noqa: BLE001
                status = {}
        info: dict[str, Any] = {
            "backend": getattr(self.llm_client, "backend", "stub" if self.llm_client is None else "unknown"),
            "available": self.llm_client is not None,
            "status": status,
            "routing": status,
        }
        if self.router is not None:
            try:
                info["tiers"] = self.router.describe()
            except Exception:  # noqa: BLE001
                info["tiers"] = {}
        else:
            info["tiers"] = {}
        info["budget"] = self.budget_snapshot()
        return info

    def budget_snapshot(self) -> dict[str, Any]:
        s = self.settings
        budget = getattr(self.llm_client, "budget", None)
        snap: dict[str, Any] = {
            "per_build_usd_cap": s.per_build_usd_cap,
            "daily_usd_cap": s.daily_usd_cap,
            "daily_token_cap": s.daily_token_cap,
        }
        if budget is not None:
            snap.update(
                {
                    "spent_build": getattr(budget, "spent_build", 0.0),
                    "spent_day": getattr(budget, "spent_day", 0.0),
                    "tokens_day": getattr(budget, "tokens_day", 0),
                    "calls": len(getattr(budget, "calls", [])),
                }
            )
        else:
            snap.update({"spent_build": 0.0, "spent_day": 0.0, "tokens_day": 0, "calls": 0})
        return snap

    # ---- trajectory replay / time-travel (P2 backend hooks) --------------
    def trajectory(
        self,
        *,
        limit: int | None = None,
        event_type: EventType | None = None,
        correlation_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return a replayable, filterable slice of the event history.

        This is the backend hook for the time-travel UI: the dashboard can
        page/seek over the full trajectory, scope it to a single build via
        ``correlation_id``, or window it in time.
        """
        events = self.event_bus.history(event_type=event_type)
        if correlation_id is not None:
            events = [e for e in events if e.correlation_id == correlation_id]
        if since is not None:
            events = [e for e in events if e.timestamp >= since]
        if until is not None:
            events = [e for e in events if e.timestamp <= until]
        if limit is not None:
            events = events[-limit:]
        return [e.to_dict() for e in events]

    def trajectory_snapshot(self) -> dict[str, Any]:
        """Full serialized trajectory for save/restore (time-travel anchor)."""
        return self.event_bus.snapshot()

    # ---- build / proposal mutation --------------------------------------
    def new_build_id(self) -> str:
        return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Auth — independent of FastAPI so it is directly unit-testable.
# ---------------------------------------------------------------------------
def extract_bearer(authorization: str | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def is_loopback(client_host: str | None) -> bool:
    """True when the request originates from localhost."""
    if not client_host:
        return False
    if client_host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return False


def check_auth(
    settings: Settings,
    *,
    authorization: str | None,
    client_host: str | None,
) -> bool:
    """Authorize a request.

    Policy (design rule #4: safe by default):
      * If ``auth_token`` is configured, a matching bearer token is required
        from every caller, regardless of origin.
      * If ``auth_token`` is empty, only loopback callers are permitted.
    """
    token = settings.auth_token.strip()
    if token:
        presented = extract_bearer(authorization)
        if presented is None:
            return False
        return _consteq(presented, token)
    return is_loopback(client_host)


def _consteq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b, strict=False):
        result |= ord(x) ^ ord(y)
    return result == 0
