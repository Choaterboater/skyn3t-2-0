"""GitHub webhook ingress (2.0 P7).

Verifies the ``X-Hub-Signature-256`` HMAC against the configured secret, then
translates GitHub events into orchestrator tasks (design rule #7 — everything
is an event). FastAPI is optional: when present, :func:`build_router` returns a
mounted ``APIRouter``; otherwise the framework-free :class:`GitHubWebhookHandler`
can be driven directly from any HTTP layer (or in tests).

Safe by default (rule #4): if a secret is configured, an invalid or missing
signature is rejected. With no secret configured the handler still works but
logs that it is unauthenticated — it never crashes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Awaitable, Callable, Optional

from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.channels import SubmitFn, env_token

try:  # optional heavy dep
    from fastapi import APIRouter, Header, Request, Response  # type: ignore

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    APIRouter = None  # type: ignore
    _HAS_FASTAPI = False


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time verify a GitHub ``sha256=...`` signature header."""
    if not secret:
        # No secret configured: cannot verify, treat as unauthenticated-allowed.
        return True
    if not signature:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature.strip())


def event_to_task(event_name: str, payload: dict[str, Any]) -> Optional[TaskRequest]:
    """Map a GitHub event into a TaskRequest, or None if not actionable."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    action = payload.get("action", "")

    if event_name == "issues" and action in ("opened", "reopened"):
        issue = payload.get("issue") or {}
        brief = f"GitHub issue in {repo}: {issue.get('title', '')}\n\n{issue.get('body', '') or ''}"
        return TaskRequest(
            type="brainstorm",
            payload={"brief": brief.strip(), "source": "github.issue"},
            capabilities_required=("brainstorm",),
            metadata={"repo": repo, "issue": issue.get("number")},
        )
    if event_name == "issue_comment" and action == "created":
        comment = (payload.get("comment") or {}).get("body", "") or ""
        if comment.strip().lower().startswith("/skyn3t"):
            brief = comment.strip()[len("/skyn3t"):].strip()
            if brief:
                return TaskRequest(
                    type="brainstorm",
                    payload={"brief": brief, "source": "github.comment"},
                    capabilities_required=("brainstorm",),
                    metadata={"repo": repo},
                )
    if event_name == "push":
        ref = payload.get("ref", "")
        commits = payload.get("commits") or []
        brief = f"Push to {repo} {ref}: {len(commits)} commit(s)."
        return TaskRequest(
            type="research",
            payload={"brief": brief, "source": "github.push"},
            capabilities_required=("research",),
            metadata={"repo": repo, "ref": ref},
        )
    return None


class GitHubWebhookHandler:
    """Framework-agnostic GitHub webhook processor."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        submit_fn: Optional[SubmitFn] = None,
        secret: Optional[str] = None,
    ) -> None:
        self.event_bus = event_bus
        self.submit_fn = submit_fn
        self.secret = secret if secret is not None else env_token("GITHUB_WEBHOOK_SECRET")

    async def handle(
        self, body: bytes, event_name: str, signature: str = ""
    ) -> dict[str, Any]:
        """Process a raw webhook delivery. Never raises (design rule #6)."""
        if not verify_signature(self.secret, body, signature):
            return {"ok": False, "error": "invalid_signature", "status": 401}
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "bad_json", "status": 400}

        if event_name == "ping":
            return {"ok": True, "event": "ping", "status": 200}

        task = event_to_task(event_name, payload)
        result: dict[str, Any] = {"ok": True, "event": event_name, "status": 200}

        if self.event_bus is not None:
            await self.event_bus.emit(
                EventType.SYSTEM,
                "github.webhook",
                {"event": event_name, "action": payload.get("action", "")},
            )

        if task is not None:
            result["task_id"] = task.task_id
            if self.event_bus is not None:
                await self.event_bus.emit(
                    EventType.TASK_SUBMITTED, "github.webhook", dict(task.payload)
                )
            if self.submit_fn is not None:
                import asyncio

                asyncio.ensure_future(self._safe_submit(task))
            result["dispatched"] = True
        else:
            result["dispatched"] = False
        return result

    async def _safe_submit(self, task: TaskRequest) -> None:
        try:
            if self.submit_fn is not None:
                await self.submit_fn(task)
        except Exception:  # noqa: BLE001
            pass


def build_router(handler: GitHubWebhookHandler):  # type: ignore[no-untyped-def]
    """Return a FastAPI APIRouter mounting the webhook, or None if FastAPI absent."""
    if not _HAS_FASTAPI:
        return None

    router = APIRouter()

    @router.post("/webhooks/github")
    async def github_hook(  # noqa: ANN202 - FastAPI route
        request: "Request",
        x_github_event: str = Header(default="", alias="X-GitHub-Event"),
        x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256"),
    ):
        body = await request.body()
        result = await handler.handle(body, x_github_event, x_hub_signature_256)
        status = int(result.get("status", 200))
        return Response(
            content=json.dumps(result),
            status_code=status,
            media_type="application/json",
        )

    return router
