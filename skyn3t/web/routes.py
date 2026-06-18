"""REST endpoints for the SkyN3t dashboard / control API.

FastAPI is a guarded optional dependency. :func:`build_router` only runs when
FastAPI is importable; importing this module never requires it. All handlers
read payloads defensively and degrade gracefully when a spine collaborator is
absent (design rule #6).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog

from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import Event, EventType
from skyn3t.web.deps import AppState, BuildRecord, ProposalRecord, check_auth

log = structlog.get_logger(__name__)

# Strong references to in-flight background build tasks (prevent GC mid-run).
_BUILD_TASKS: set = set()


def _reap_build_task(task: Any) -> None:
    _BUILD_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # build task should never raise (runner catches), but log if it does
        log.error("web.build_task_crashed", error=str(exc))

try:  # pragma: no cover - exercised only when fastapi present
    from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
    _HAVE_FASTAPI = True
except Exception:  # noqa: BLE001
    APIRouter = Body = Depends = HTTPException = Query = Request = None  # type: ignore[assignment,misc]
    _HAVE_FASTAPI = False


# ---------------------------------------------------------------------------
# Backend-agnostic handler implementations. These take an AppState and plain
# kwargs so they are unit-testable without FastAPI or a running server.
# ---------------------------------------------------------------------------
async def status_payload(state: AppState) -> dict[str, Any]:
    return state.status()


async def agents_payload(state: AppState) -> dict[str, Any]:
    return {"agents": state.agents_snapshot()}


async def llm_backends_payload(state: AppState) -> dict[str, Any]:
    return state.llm_backends()


async def budget_payload(state: AppState) -> dict[str, Any]:
    return state.budget_snapshot()


async def submit_build(state: AppState, brief: str, stack: str = "", slug: str = "") -> dict[str, Any]:
    """Queue a build. Uses the studio if wired, else records + emits an event."""
    if not brief or not brief.strip():
        raise ValueError("brief is required")
    build_id = state.new_build_id()
    rec = BuildRecord(
        build_id=build_id,
        brief=brief.strip(),
        slug=slug.strip(),
        stack=stack.strip(),
        status="queued",
        correlation_id=build_id,
    )
    state.builds[build_id] = rec

    # Prefer a wired StudioRunner (async start(brief, slug=None, extra=None)),
    # falling back to a legacy submit(...) if present. The build runs as a
    # background task so the endpoint returns immediately with the build_id.
    studio = state.studio
    dispatched = False
    runner = None
    if studio is not None:
        if hasattr(studio, "start"):
            runner = lambda: studio.start(brief, slug=slug or None, extra={"stack": stack, "build_id": build_id})
        elif hasattr(studio, "submit"):  # pragma: no cover - legacy shape
            runner = lambda: studio.submit(brief=brief, slug=slug, stack=stack, build_id=build_id)
    if runner is not None:
        try:
            res = runner()
            if hasattr(res, "__await__"):
                import asyncio
                # Keep a strong reference so the build task isn't garbage-
                # collected mid-run, and retrieve any exception on completion.
                task = asyncio.ensure_future(res)
                _BUILD_TASKS.add(task)
                task.add_done_callback(_reap_build_task)
            dispatched = True
        except Exception:  # noqa: BLE001 - never let a build crash the API
            dispatched = False

    await state.event_bus.emit(
        EventType.BUILD_STARTED,
        source="web.api",
        payload={"build_id": build_id, "brief": rec.brief, "slug": rec.slug, "stack": rec.stack},
        correlation_id=build_id,
    )
    if not dispatched:
        rec.status = "queued_no_studio"
    return {"build_id": build_id, "status": rec.status, "dispatched": dispatched}


async def list_builds(state: AppState, limit: int = 25) -> dict[str, Any]:
    builds: list[dict[str, Any]] = []
    # Live cache first.
    cached = sorted(state.builds.values(), key=lambda r: r.updated_at, reverse=True)
    builds.extend(r.to_dict() for r in cached[:limit])
    # Augment with persisted history when memory is available.
    seen = {b["build_id"] for b in builds}
    if state.memory is not None and hasattr(state.memory, "recent_builds"):
        try:  # pragma: no cover - depends on memory backend
            for row in await state.memory.recent_builds(limit=limit):
                bid = str(row.get("build_id", ""))
                if bid and bid not in seen:
                    builds.append(row)
                    seen.add(bid)
        except Exception:  # noqa: BLE001
            pass
    return {"builds": builds[:limit]}


async def approve_build(state: AppState, build_id: str, approved: bool = True, reason: str = "") -> dict[str, Any]:
    rec = state.builds.get(build_id)
    if rec is None:
        raise KeyError(build_id)
    rec.status = "approved" if approved else "rejected"
    rec.updated_at = time.time()
    await state.event_bus.emit(
        EventType.PROPOSAL_DECIDED,
        source="web.api",
        payload={"build_id": build_id, "approved": approved, "reason": reason, "kind": "build_approval"},
        correlation_id=rec.correlation_id,
    )
    return {"build_id": build_id, "status": rec.status}


async def list_proposals(state: AppState, status: str = "") -> dict[str, Any]:
    items = list(state.proposals.values())
    if status:
        items = [p for p in items if p.status == status]
    items.sort(key=lambda p: p.created_at, reverse=True)
    return {"proposals": [p.to_dict() for p in items]}


async def decide_proposal(state: AppState, proposal_id: str, approved: bool, reason: str = "", decided_by: str = "api") -> dict[str, Any]:
    rec = state.proposals.get(proposal_id)
    if rec is None:
        # Allow deciding an unseen proposal id so cortex can be authoritative.
        rec = ProposalRecord(proposal_id=proposal_id, kind="unknown", summary="")
        state.proposals[proposal_id] = rec
    rec.status = "approved" if approved else "rejected"
    rec.reason = reason
    rec.decided_by = decided_by
    rec.decided_at = time.time()

    cortex = state.cortex
    if cortex is not None and hasattr(cortex, "decide"):
        try:  # pragma: no cover - depends on sibling package
            res = cortex.decide(proposal_id=proposal_id, approved=approved, reason=reason)
            if hasattr(res, "__await__"):
                await res
        except Exception:  # noqa: BLE001
            pass

    await state.event_bus.emit(
        EventType.PROPOSAL_DECIDED,
        source="web.api",
        payload={"proposal_id": proposal_id, "approved": approved, "reason": reason, "kind": rec.kind},
    )
    return {"proposal_id": proposal_id, "status": rec.status}


async def list_skills(state: AppState) -> dict[str, Any]:
    skills = state.skills
    # SkillLibrary exposes .all() -> list[Skill]; serialize for the SPA.
    getter = getattr(skills, "all", None) or getattr(skills, "list_skills", None)
    if skills is not None and getter is not None:
        try:
            res = getter()
            if hasattr(res, "__await__"):
                res = await res
            out = []
            for s in res:
                if isinstance(s, dict):
                    out.append(s)
                else:
                    out.append({
                        "slug": getattr(s, "slug", ""),
                        "title": getattr(s, "title", ""),
                        "stack": getattr(s, "stack", ""),
                        "tags": list(getattr(s, "tags", []) or []),
                        "score": getattr(s, "score", 0),
                        "source": getattr(s, "source", ""),
                    })
            return {"skills": out}
        except Exception:  # noqa: BLE001
            pass
    # Degraded: surface configured skill-hub paths from settings.
    paths = [p for p in state.settings.skills_hub_paths.split(",") if p.strip()]
    return {"skills": [], "hub_paths": paths}


async def knowledge_search(state: AppState, q: str, limit: int = 10) -> dict[str, Any]:
    knowledge = state.knowledge
    if knowledge is not None and hasattr(knowledge, "search"):
        try:  # pragma: no cover - depends on sibling package
            res = knowledge.search(q, limit=limit)
            if hasattr(res, "__await__"):
                res = await res
            return {"query": q, "results": list(res)}
        except Exception:  # noqa: BLE001
            pass
    # Degraded keyword scan over recent lessons when memory is present.
    results: list[dict[str, Any]] = []
    if state.memory is not None and hasattr(state.memory, "relevant_lessons"):
        try:  # pragma: no cover
            lessons = await state.memory.relevant_lessons(stack="", stage="", limit=limit)
            ql = q.lower()
            results = [l for l in lessons if ql in str(l.get("text", "")).lower()] or lessons
        except Exception:  # noqa: BLE001
            results = []
    return {"query": q, "results": results[:limit], "degraded": True}


def _event_counts(state: AppState) -> dict[str, int]:
    """Return monotonic per-type event counts.

    Avoids the O(history) rescan on every scrape and, unlike scanning the
    bounded history ring, does not silently undercount once the buffer wraps.
    A counter dict is attached to the event bus and kept current via a single
    ALL-subscription registered lazily on first use; it is seeded once from the
    current history so counts reconcile with ``events_published_total``.
    """
    bus = state.event_bus
    counts: dict[str, int] | None = getattr(bus, "_skyn3t_event_counts", None)
    if counts is None:
        counts = {}
        for ev in bus.history():
            counts[ev.type.value] = counts.get(ev.type.value, 0) + 1
        try:
            bus._skyn3t_event_counts = counts  # type: ignore[attr-defined]

            async def _tick(ev: Event) -> None:  # pragma: no cover - async wiring
                counts[ev.type.value] = counts.get(ev.type.value, 0) + 1

            bus.subscribe(EventType.ALL, _tick)
        except Exception:  # noqa: BLE001 - degrade to per-call scan if subscribe unavailable
            return counts
    return dict(counts)


async def metrics_payload(state: AppState) -> dict[str, Any]:
    s = state.status()
    counts = _event_counts(state)
    return {
        "events_published": state.event_bus.published_count,
        "event_counts": counts,
        "agents": s["agents"],
        "builds": s["builds"],
        "proposals_pending": s["proposals_pending"],
        "budget": state.budget_snapshot(),
    }


def render_prometheus(metrics: dict[str, Any]) -> str:
    """Render the metrics dict in Prometheus text exposition format."""
    lines: list[str] = []

    def _metric(name: str, value: Any, help_text: str, labels: str = "") -> None:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return
        lines.append(f"# HELP skyn3t_{name} {help_text}")
        lines.append(f"# TYPE skyn3t_{name} gauge")
        lines.append(f"skyn3t_{name}{labels} {num}")

    _metric("events_published_total", metrics.get("events_published", 0), "Total events published")
    _metric("agents", metrics.get("agents", 0), "Registered agents")
    _metric("builds", metrics.get("builds", 0), "Known builds")
    _metric("proposals_pending", metrics.get("proposals_pending", 0), "Pending proposals")
    budget = metrics.get("budget", {})
    _metric("budget_spent_day_usd", budget.get("spent_day", 0.0), "USD spent today")
    _metric("budget_tokens_day", budget.get("tokens_day", 0), "Tokens used today")
    for et, count in metrics.get("event_counts", {}).items():
        safe = et.replace(".", "_").replace("*", "all")
        _metric("event_count", count, "Events by type", labels=f'{{type="{safe}"}}')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Runtime LLM configuration (set keys / switch backend from the dashboard).
# ---------------------------------------------------------------------------
_PROVIDER_FIELDS = {
    "openrouter": "openrouter_api_key",
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "kimi": "kimi_api_key",
}


def _persist_env_var(name: str, value: str) -> None:
    """Upsert ``NAME=value`` in the repo .env (best-effort; never raises)."""
    try:
        from skyn3t.config.settings import REPO_ROOT

        env = REPO_ROOT / ".env"
        lines = env.read_text().splitlines() if env.exists() else []
        out: list[str] = []
        found = False
        for ln in lines:
            stripped = ln.strip().lstrip("#").strip()
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key == name:
                out.append(f"{name}={value}")
                found = True
            else:
                out.append(ln)
        if not found:
            out.append(f"{name}={value}")
        env.write_text("\n".join(out) + "\n")
    except Exception:  # noqa: BLE001
        pass


async def llm_secrets_payload(state: AppState) -> dict[str, Any]:
    s = state.settings
    backend = state.llm_client.backend if state.llm_client is not None else "n/a"
    return {
        "providers": {p: bool(getattr(s, f, "")) for p, f in _PROVIDER_FIELDS.items()},
        "backend": backend,
        "backend_pref": getattr(s, "llm_backend", "auto"),
        "cli_provider": getattr(s, "cli_llm_provider", "claude"),
    }


async def set_llm_key(state: AppState, provider: str, key: str, persist: bool = True) -> dict[str, Any]:
    field = _PROVIDER_FIELDS.get((provider or "").lower())
    if field is None:
        raise ValueError(f"unknown provider {provider!r}")
    key = (key or "").strip()
    setattr(state.settings, field, key)
    if state.llm_client is not None:
        try:
            state.llm_client.settings = state.settings  # same singleton, kept explicit
        except Exception:  # noqa: BLE001
            pass
    if persist:
        _persist_env_var(f"SKYN3T_{field.upper()}", key)
    backend = state.llm_client.backend if state.llm_client is not None else "n/a"
    return {"provider": provider.lower(), "configured": bool(key), "backend": backend}


# Messaging channels read their credentials from the environment via
# integrations.channels.env_token(); configuring one means persisting to .env
# AND setting os.environ so it takes effect without a restart. (Live bot
# delivery is wired separately — this is the config surface.)
_CHANNEL_ENV = {
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "discord": ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"),
    "slack": ("SLACK_BOT_TOKEN", "SLACK_CHANNEL"),
}


async def integrations_payload(state: AppState) -> dict[str, Any]:
    import os

    channels = {}
    for name, (tok, tgt) in _CHANNEL_ENV.items():
        configured = bool(os.environ.get(f"SKYN3T_{tok}") or os.environ.get(tok))
        target_set = bool(os.environ.get(f"SKYN3T_{tgt}") or os.environ.get(tgt))
        channels[name] = {"configured": configured, "target_set": target_set}
    listener = {"running": False, "available": state.messaging is not None}
    if state.messaging is not None:
        try:
            listener.update(state.messaging.status())
        except Exception:  # noqa: BLE001
            pass
    return {"channels": channels, "listener": listener}


async def messaging_control(state: AppState, action: str) -> dict[str, Any]:
    if state.messaging is None:
        raise ValueError("messaging service unavailable")
    if action == "start":
        res = state.messaging.start_listeners()
        return await res if hasattr(res, "__await__") else res
    if action == "stop":
        return state.messaging.stop()
    if action == "test":
        sent = await state.messaging.notify("🔔 SkyN3t test notification — messaging is wired.")
        return {"sent": sent}
    raise ValueError(f"unknown action {action!r}")


async def set_integration_credential(
    state: AppState, channel: str, token: str = "", target: str = "", persist: bool = True
) -> dict[str, Any]:
    import os

    env = _CHANNEL_ENV.get((channel or "").lower())
    if env is None:
        raise ValueError(f"unknown channel {channel!r}")
    tok_var, tgt_var = env
    if token:
        os.environ[f"SKYN3T_{tok_var}"] = token.strip()
        if persist:
            _persist_env_var(f"SKYN3T_{tok_var}", token.strip())
    if target:
        os.environ[f"SKYN3T_{tgt_var}"] = target.strip()
        if persist:
            _persist_env_var(f"SKYN3T_{tgt_var}", target.strip())
    return {
        "channel": channel.lower(),
        "configured": bool(token) or bool(os.environ.get(f"SKYN3T_{tok_var}")),
        "target_set": bool(target) or bool(os.environ.get(f"SKYN3T_{tgt_var}")),
    }


async def set_llm_backend(state: AppState, backend: str, persist: bool = True) -> dict[str, Any]:
    backend = (backend or "auto").lower()
    state.settings.llm_backend = backend
    if state.llm_client is not None:
        try:
            state.llm_client.settings = state.settings
        except Exception:  # noqa: BLE001
            pass
    if persist:
        _persist_env_var("SKYN3T_LLM_BACKEND", backend)
    active = state.llm_client.backend if state.llm_client is not None else backend
    return {"requested": backend, "active": active}


# ---------------------------------------------------------------------------
# Compatibility payloads for the SPA's endpoint names.
# ---------------------------------------------------------------------------
async def health_payload(state: AppState) -> dict[str, Any]:
    base = await status_payload(state)
    backend = state.llm_client.backend if state.llm_client is not None else "stub"
    active = sum(1 for b in state.builds.values() if b.status in ("queued", "running"))
    return {**base, "ok": True, "backend": backend, "llm_backend": backend,
            "active_builds": active, "agent_count": base.get("agents", 0)}


async def brain_payload(state: AppState) -> dict[str, Any]:
    agents = len(state.orchestrator.agents) if state.orchestrator else 0
    lessons = 0
    documents = 0
    try:
        if state.memory is not None:
            rows = await state.memory.recent_builds(limit=200)
            documents = len(rows)
    except Exception:  # noqa: BLE001
        pass
    return {
        "activity": state.event_bus.published_count,
        "agents": agents,
        "builds": len(state.builds),
        "documents": documents,
        "lessons": lessons,
        "proposals": len(state.proposals),
    }


async def settings_payload(state: AppState) -> dict[str, Any]:
    s = state.settings
    keys = ("free_only", "no_claude", "execution_backend", "autonomous_builds",
            "approval_gates", "per_build_usd_cap", "daily_usd_cap", "llm_backend")
    return {k: getattr(s, k, None) for k in keys}


# ---------------------------------------------------------------------------
# FastAPI wiring (only constructed when FastAPI is importable).
# ---------------------------------------------------------------------------
def build_router(state: AppState) -> Any:
    """Build and return an ``APIRouter`` bound to ``state``.

    Raises :class:`RuntimeError` if FastAPI is not installed — callers should
    only reach this from within an app whose creation already required FastAPI.
    """
    if not _HAVE_FASTAPI:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "FastAPI is not installed; install 'fastapi' to use the web API router."
        )

    router = APIRouter(prefix="/api")

    async def require_auth(request: Request) -> None:
        client_host = request.client.host if request.client else None
        ok = check_auth(
            state.settings,
            authorization=request.headers.get("authorization"),
            client_host=client_host,
        )
        if not ok:
            raise HTTPException(status_code=401, detail="unauthorized")

    auth = Depends(require_auth)

    @router.get("/status", dependencies=[auth])
    async def _status() -> dict[str, Any]:
        return await status_payload(state)

    @router.get("/agents", dependencies=[auth])
    async def _agents() -> dict[str, Any]:
        return await agents_payload(state)

    @router.get("/llm/backends", dependencies=[auth])
    async def _llm_backends() -> dict[str, Any]:
        return await llm_backends_payload(state)

    @router.get("/budget", dependencies=[auth])
    async def _budget() -> dict[str, Any]:
        return await budget_payload(state)

    # ---- SPA compatibility aliases (frontend endpoint names) ------------
    @router.get("/health", dependencies=[auth])
    async def _health() -> dict[str, Any]:
        return await health_payload(state)

    @router.get("/brain", dependencies=[auth])
    async def _brain() -> dict[str, Any]:
        return await brain_payload(state)

    @router.get("/settings", dependencies=[auth])
    async def _settings() -> dict[str, Any]:
        return await settings_payload(state)

    @router.get("/builds", dependencies=[auth])
    async def _builds_alias(limit: int = Query(default=25, ge=1, le=200)) -> dict[str, Any]:
        return await list_builds(state, limit=limit)

    @router.post("/builds", dependencies=[auth])
    async def _build_alias(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            return await submit_build(
                state,
                brief=str(body.get("brief", "")),
                stack=str(body.get("stack", "")),
                slug=str(body.get("slug", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.get("/cortex/proposals", dependencies=[auth])
    async def _cortex_proposals(status: str = Query(default="")) -> dict[str, Any]:
        return await list_proposals(state, status=status)

    @router.post("/cortex/proposals/{proposal_id}/decide", dependencies=[auth])
    async def _cortex_decide(
        proposal_id: str, body: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        decision = str(body.get("decision", body.get("approved", ""))).lower()
        approved = decision in ("approve", "approved", "accept", "true", "yes", "1")
        return await decide_proposal(
            state, proposal_id=proposal_id, approved=approved,
            reason=str(body.get("reason", "")), decided_by="dashboard",
        )

    @router.get("/llm/secrets", dependencies=[auth])
    async def _llm_secrets() -> dict[str, Any]:
        return await llm_secrets_payload(state)

    @router.post("/llm/key", dependencies=[auth])
    async def _set_llm_key(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            return await set_llm_key(state, str(body.get("provider", "")), str(body.get("key", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.post("/llm/backend", dependencies=[auth])
    async def _set_llm_backend(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return await set_llm_backend(state, str(body.get("backend", "auto")))

    @router.get("/integrations", dependencies=[auth])
    async def _integrations() -> dict[str, Any]:
        return await integrations_payload(state)

    @router.post("/integrations/credential", dependencies=[auth])
    async def _set_integration(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            return await set_integration_credential(
                state, str(body.get("channel", "")),
                token=str(body.get("token", "")), target=str(body.get("target", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.post("/integrations/listener", dependencies=[auth])
    async def _messaging_control(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            return await messaging_control(state, str(body.get("action", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.post("/studio/build", dependencies=[auth])
    async def _build(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            return await submit_build(
                state,
                brief=str(body.get("brief", "")),
                stack=str(body.get("stack", "")),
                slug=str(body.get("slug", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.get("/studio/builds", dependencies=[auth])
    async def _builds(limit: int = Query(default=25, ge=1, le=200)) -> dict[str, Any]:
        return await list_builds(state, limit=limit)

    @router.post("/studio/approve", dependencies=[auth])
    async def _approve(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            return await approve_build(
                state,
                build_id=str(body.get("build_id", "")),
                approved=bool(body.get("approved", True)),
                reason=str(body.get("reason", "")),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="build not found")

    @router.get("/proposals", dependencies=[auth])
    async def _proposals(status: str = Query(default="")) -> dict[str, Any]:
        return await list_proposals(state, status=status)

    @router.post("/proposals/decide", dependencies=[auth])
    async def _decide(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        pid = str(body.get("proposal_id", ""))
        if not pid:
            raise HTTPException(status_code=422, detail="proposal_id is required")
        return await decide_proposal(
            state,
            proposal_id=pid,
            approved=bool(body.get("approved", False)),
            reason=str(body.get("reason", "")),
            decided_by=str(body.get("decided_by", "api")),
        )

    @router.get("/skills", dependencies=[auth])
    async def _skills() -> dict[str, Any]:
        return await list_skills(state)

    @router.get("/knowledge/search", dependencies=[auth])
    async def _knowledge(
        q: str = Query(default=""),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict[str, Any]:
        return await knowledge_search(state, q=q, limit=limit)

    @router.get("/metrics", dependencies=[auth])
    async def _metrics(request: Request) -> Any:
        data = await metrics_payload(state)
        accept = request.headers.get("accept", "")
        if "text/plain" in accept or request.query_params.get("format") == "prometheus":
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(render_prometheus(data))
        return data

    # Trajectory replay / time-travel backend hooks (2.0 backlog P2).
    @router.get("/trajectory", dependencies=[auth])
    async def _trajectory(
        limit: int = Query(default=200, ge=1, le=2000),
        type: str = Query(default=""),
        correlation_id: str = Query(default=""),
        since: float | None = Query(default=None),
        until: float | None = Query(default=None),
    ) -> dict[str, Any]:
        et: EventType | None = None
        if type:
            try:
                et = EventType(type)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"unknown event type: {type}")
        events = state.trajectory(
            limit=limit,
            event_type=et,
            correlation_id=correlation_id or None,
            since=since,
            until=until,
        )
        return {"events": events, "count": len(events)}

    return router
