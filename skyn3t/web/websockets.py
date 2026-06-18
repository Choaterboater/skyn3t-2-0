"""WebSocket endpoints + the event-bus -> socket bridge.

Three channels:
  * ``/ws``           — every event on the bus (the full trajectory stream).
  * ``/ws/swarm``     — agent lifecycle / heartbeat / health events only.
  * ``/ws/proposals`` — cortex proposal create/decide events only.

FastAPI/Starlette is a guarded optional dependency. The :class:`ConnectionHub`
is framework-agnostic (it talks to any object exposing async ``send_text``),
so it is fully unit-testable without FastAPI or a live socket.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.web.deps import AppState, check_auth

try:  # pragma: no cover - exercised only when fastapi present
    from fastapi import APIRouter, WebSocket, WebSocketDisconnect
    _HAVE_FASTAPI = True
except Exception:  # noqa: BLE001
    APIRouter = WebSocket = WebSocketDisconnect = None  # type: ignore[assignment,misc]
    _HAVE_FASTAPI = False


# Channel -> the event types it forwards (None => all events).
_SWARM_TYPES = {
    EventType.AGENT_REGISTERED,
    EventType.AGENT_UNREGISTERED,
    EventType.AGENT_HEARTBEAT,
    EventType.AGENT_FAILED,
    EventType.AGENT_RECOVERED,
    EventType.TASK_STARTED,
    EventType.TASK_COMPLETED,
    EventType.TASK_FAILED,
}
_PROPOSAL_TYPES = {
    EventType.PROPOSAL_CREATED,
    EventType.PROPOSAL_DECIDED,
}


def _channel_match(channel: str, event: Event) -> bool:
    if channel == "swarm":
        return event.type in _SWARM_TYPES
    if channel == "proposals":
        return event.type in _PROPOSAL_TYPES
    return True  # "all"


class ConnectionHub:
    """Tracks live sockets per channel and fans out events to them.

    Subscribes once to ``EventType.ALL`` on the bus; each inbound event is
    pushed to the sockets whose channel matches. A failed send drops only that
    socket (design rule #6).
    """

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._conns: dict[str, set[Any]] = {"all": set(), "swarm": set(), "proposals": set()}
        self._lock = asyncio.Lock()
        self._unsub = event_bus.subscribe(EventType.ALL, self._on_event)

    async def _on_event(self, event: Event) -> None:
        message = json.dumps({"event": event.to_dict()})
        async with self._lock:
            targets: list[tuple[str, Any]] = []
            for channel, conns in self._conns.items():
                if not conns:
                    continue
                if _channel_match(channel, event):
                    targets.extend((channel, ws) for ws in conns)
        dead: list[tuple[str, Any]] = []
        for channel, ws in targets:
            try:
                await ws.send_text(message)
            except Exception:  # noqa: BLE001
                dead.append((channel, ws))
        if dead:
            async with self._lock:
                for channel, ws in dead:
                    self._conns.get(channel, set()).discard(ws)

    async def add(self, channel: str, ws: Any) -> None:
        async with self._lock:
            self._conns.setdefault(channel, set()).add(ws)

    async def remove(self, channel: str, ws: Any) -> None:
        async with self._lock:
            self._conns.get(channel, set()).discard(ws)

    def count(self, channel: str | None = None) -> int:
        if channel is not None:
            return len(self._conns.get(channel, set()))
        return sum(len(s) for s in self._conns.values())

    def close(self) -> None:
        """Detach from the event bus (idempotent)."""
        try:
            self._unsub()
        except Exception:  # noqa: BLE001
            pass


def _ws_authorized(state: AppState, ws: Any) -> bool:
    """Authorize a websocket handshake using header or ?token= query param."""
    headers = {k.decode().lower(): v.decode() for k, v in getattr(ws, "headers", {}).raw} if hasattr(getattr(ws, "headers", None), "raw") else dict(getattr(ws, "headers", {}))
    authorization = headers.get("authorization")
    token = state.settings.auth_token.strip()
    if token and not authorization:
        # Allow token via query param for browser WebSocket clients.
        qp_token = None
        try:
            qp_token = ws.query_params.get("token")
        except Exception:  # noqa: BLE001
            qp_token = None
        if qp_token:
            authorization = f"Bearer {qp_token}"
    client_host = None
    client = getattr(ws, "client", None)
    if client is not None:
        client_host = getattr(client, "host", None)
    return check_auth(state.settings, authorization=authorization, client_host=client_host)


def build_ws_router(state: AppState, hub: ConnectionHub) -> Any:
    """Build the websocket router. Requires FastAPI."""
    if not _HAVE_FASTAPI:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "FastAPI is not installed; install 'fastapi' to use the websocket router."
        )

    router = APIRouter()

    async def _serve(ws: "WebSocket", channel: str) -> None:
        if not _ws_authorized(state, ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        # Prime with recent trajectory BEFORE registering as a fan-out target,
        # so the hub never sends concurrently with the priming loop on the same
        # socket (Starlette WebSockets don't support concurrent sends) and the
        # client doesn't receive duplicate (replayed + live) frames.
        try:
            for ev in state.event_bus.history(limit=50):
                if _channel_match(channel, ev):
                    await ws.send_text(json.dumps({"event": ev.to_dict()}))
            await hub.add(channel, ws)
            while True:
                # Keep the socket open; ignore inbound (clients are receivers).
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            await hub.remove(channel, ws)

    @router.websocket("/ws")
    async def _ws_all(ws: "WebSocket") -> None:
        await _serve(ws, "all")

    @router.websocket("/ws/swarm")
    async def _ws_swarm(ws: "WebSocket") -> None:
        await _serve(ws, "swarm")

    @router.websocket("/ws/proposals")
    async def _ws_proposals(ws: "WebSocket") -> None:
        await _serve(ws, "proposals")

    return router
