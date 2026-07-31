"""Messaging control surfaces — the Channel abstraction (2.0 P7).

A *channel* is a bidirectional human-in-the-loop surface: Telegram, Discord,
Slack, a GitHub webhook, the CLI, etc. Inbound messages become *briefs*
submitted to the orchestrator (design rule #7 — everything is an event), and
outbound messages let the swarm report progress or ask for approval.

Design posture:
  * Missing credentials never crash. A channel with no token reports
    ``is_available() == False`` and simply does nothing (design rule #6).
  * Heavy SDKs (telegram/discord/slack_sdk/fastapi) are imported lazily inside
    each concrete channel, guarded by try/except, so this module — and the
    registry — always import even with zero optional deps installed.
  * Everything is an event: an inbound brief is published on the EventBus and
    (when wired) submitted to the orchestrator as a TaskRequest.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import time
from typing import Any

from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus, EventType

# A submit callable mirrors Orchestrator.submit (async (TaskRequest) -> TaskResult)
SubmitFn = Callable[[TaskRequest], Awaitable[Any]]


@dataclass(slots=True)
class InboundMessage:
    """A normalized message arriving from any channel."""

    channel: str
    text: str
    sender: str = "unknown"
    target: str = ""  # reply address (chat id / channel id / thread ts)
    raw: dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time)

    @property
    def is_command(self) -> bool:
        return self.text.strip().startswith("/")

    def command(self) -> tuple[str, str]:
        """Split a slash command into (name, remainder). ('' , text) if none."""
        s = self.text.strip()
        if not s.startswith("/"):
            return "", s
        head, _, rest = s[1:].partition(" ")
        return head.lower(), rest.strip()


@dataclass(slots=True)
class OutboundMessage:
    target: str
    text: str
    channel: str = ""


def env_token(*names: str) -> str:
    """Return the first non-empty environment variable among ``names``.

    Channels look in the environment for credentials so the pinned Settings
    contract need not carry per-channel secrets. Both ``SKYN3T_``-prefixed and
    bare variants are accepted — SKYN3T_ FIRST, matching every other credential
    chain in the system: the GUI persists the prefixed name, so bare-first let
    a stale ambient TELEGRAM_BOT_TOKEN silently shadow the GUI-saved token for
    sends while the status payload reported the SKYN3T one (audit M23).
    """
    for name in names:
        val = os.environ.get(f"SKYN3T_{name}") or os.environ.get(name)
        if val:
            return val.strip()
    return ""


class Channel(ABC):
    """Abstract bidirectional messaging surface."""

    #: short stable identifier, e.g. "telegram"
    name: str = "channel"

    def __init__(
        self,
        event_bus: EventBus | None = None,
        submit_fn: SubmitFn | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.submit_fn = submit_fn
        self.config: dict[str, Any] = config or {}

    # ---- contract --------------------------------------------------------
    @abstractmethod
    def is_available(self) -> bool:
        """True only when this channel has the credentials/deps to operate."""

    @abstractmethod
    async def handle_inbound(self, msg: InboundMessage) -> dict[str, Any]:
        """Process an inbound message. Must never raise (design rule #6)."""

    @abstractmethod
    async def send(self, target: str, text: str) -> bool:
        """Best-effort outbound. Returns False instead of raising on failure."""

    # ---- shared inbound -> brief flow ------------------------------------
    async def route_inbound(self, msg: InboundMessage) -> dict[str, Any]:
        """Default inbound handling: turn a message into a brief or an action.

        Commands:
          /build <text>   -> submit a build brief
          /approve <id>   -> publish a PROPOSAL_DECIDED(approve) event
          /reject <id>    -> publish a PROPOSAL_DECIDED(reject) event
          /ping           -> health echo
        Bare text         -> treated as a build brief.
        """
        cmd, rest = msg.command()
        try:
            if cmd in ("", "build", "new") or not msg.is_command:
                brief = rest if cmd else msg.text.strip()
                if not brief:
                    return {"action": "noop", "reason": "empty"}
                return await self.submit_brief(brief, msg)
            if cmd in ("approve", "reject", "deny"):
                decision = "approve" if cmd == "approve" else "reject"
                return await self._decide(rest.strip(), decision, msg)
            if cmd == "ping":
                await self.send(msg.target, "pong")
                return {"action": "ping"}
            # Unknown command -> echo guidance, still no crash.
            await self.send(
                msg.target,
                "commands: /build <idea>, /approve <id>, /reject <id>, /ping",
            )
            return {"action": "help"}
        except Exception as exc:  # noqa: BLE001 - rule #6, never crash a channel
            await self._emit_error(exc, msg)
            return {"action": "error", "error": str(exc)}

    async def submit_brief(self, brief: str, msg: InboundMessage) -> dict[str, Any]:
        """Publish + submit a build brief sourced from a channel message."""
        payload = {
            "brief": brief,
            "channel": self.name,
            "sender": msg.sender,
            "reply_target": msg.target,
        }
        if self.event_bus is not None:
            await self.event_bus.emit(
                EventType.TASK_SUBMITTED, f"channel.{self.name}", payload
            )
        task_id = None
        if self.submit_fn is not None:
            task = TaskRequest(
                type="brainstorm",
                payload={"brief": brief, "source": f"channel.{self.name}"},
                capabilities_required=("brainstorm",),
                metadata={"channel": self.name, "reply_target": msg.target},
            )
            task_id = task.task_id
            # Fire and forget on the orchestrator; do not block the channel.
            import asyncio

            asyncio.ensure_future(self._safe_submit(task))
        await self.send(msg.target, "Got it — queued your brief.")
        return {"action": "brief_submitted", "task_id": task_id, "brief": brief}

    async def _safe_submit(self, task: TaskRequest) -> None:
        try:
            if self.submit_fn is not None:
                await self.submit_fn(task)
        except Exception:  # noqa: BLE001 - background task must not crash loop
            pass

    async def _decide(
        self, proposal_id: str, decision: str, msg: InboundMessage
    ) -> dict[str, Any]:
        if self.event_bus is not None:
            await self.event_bus.emit(
                EventType.PROPOSAL_DECIDED,
                f"channel.{self.name}",
                {
                    "proposal_id": proposal_id,
                    # Emit the canonical `approved` bool the AppState handler reads
                    # (web/deps.py). A bare `decision` string matched no handler
                    # shape, so channel approvals (Telegram/Discord/Slack) were
                    # silently dropped.
                    "approved": str(decision).strip().lower() in ("approve", "approved", "yes"),
                    "decision": decision,
                    "by": msg.sender,
                    "channel": self.name,
                },
            )
        await self.send(msg.target, f"Recorded {decision} for {proposal_id or '?'}.")
        return {"action": "decision", "decision": decision, "proposal_id": proposal_id}

    async def _emit_error(self, exc: Exception, msg: InboundMessage) -> None:
        if self.event_bus is not None:
            try:
                await self.event_bus.emit(
                    EventType.SYSTEM,
                    f"channel.{self.name}",
                    {"error": str(exc), "where": "handle_inbound"},
                )
            except Exception:  # noqa: BLE001
                pass


class ChannelRegistry:
    """Holds the configured channels and exposes available ones."""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}

    def register(self, channel: Channel) -> Channel:
        self._channels[channel.name] = channel
        return channel

    def unregister(self, name: str) -> None:
        self._channels.pop(name, None)

    def get(self, name: str) -> Channel | None:
        return self._channels.get(name)

    def all(self) -> list[Channel]:
        return list(self._channels.values())

    def available(self) -> list[Channel]:
        return [c for c in self._channels.values() if c.is_available()]

    def names(self) -> list[str]:
        return list(self._channels.keys())

    def describe(self) -> dict[str, bool]:
        return {name: c.is_available() for name, c in self._channels.items()}

    def __len__(self) -> int:
        return len(self._channels)

    def __contains__(self, name: object) -> bool:
        return name in self._channels


def build_default_registry(
    event_bus: EventBus | None = None,
    submit_fn: SubmitFn | None = None,
    config: dict[str, Any] | None = None,
) -> ChannelRegistry:
    """Construct a registry with every known channel.

    Each concrete channel self-reports availability based on its credentials
    and SDK presence, so this is safe to call with nothing installed.
    """
    reg = ChannelRegistry()
    # Imported lazily to keep this module dependency-free at import.
    from skyn3t.integrations.discord import DiscordChannel
    from skyn3t.integrations.slack import SlackChannel
    from skyn3t.integrations.telegram import TelegramChannel

    for cls in (TelegramChannel, DiscordChannel, SlackChannel):
        try:
            reg.register(cls(event_bus=event_bus, submit_fn=submit_fn, config=config))
        except Exception:  # noqa: BLE001 - a broken channel never blocks others
            pass
    return reg
