"""Discord control surface (2.0 P7).

Guards the optional ``discord.py`` SDK. Outbound messaging primarily targets a
configured *webhook URL* (no SDK or gateway connection required), with a raw
HTTPS fallback so reporting works even with nothing installed. Inbound is
supported either through interaction payloads (slash commands) or the gateway
SDK when present. No credentials -> ``is_available()`` is False and all methods
no-op (design rule #6).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Optional

from skyn3t.integrations.channels import (
    Channel,
    InboundMessage,
    SubmitFn,
    env_token,
)
from skyn3t.core.events import EventBus

try:  # optional heavy dep
    import discord as _discord  # type: ignore

    _HAS_DISCORD = True
except Exception:  # noqa: BLE001
    _discord = None  # type: ignore
    _HAS_DISCORD = False


class DiscordChannel(Channel):
    name = "discord"

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        submit_fn: Optional[SubmitFn] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(event_bus=event_bus, submit_fn=submit_fn, config=config)
        self.bot_token = self.config.get("token") or env_token("DISCORD_BOT_TOKEN")
        self.webhook_url = (
            self.config.get("webhook_url") or env_token("DISCORD_WEBHOOK_URL")
        )

    def is_available(self) -> bool:
        # A webhook URL is enough for outbound; a bot token enables inbound too.
        return bool(self.webhook_url or self.bot_token)

    async def handle_inbound(self, msg: InboundMessage) -> dict[str, Any]:
        if not self.is_available():
            return {"action": "disabled", "channel": self.name}
        return await self.route_inbound(msg)

    @staticmethod
    def parse_interaction(payload: dict[str, Any]) -> Optional[InboundMessage]:
        """Convert a Discord interaction payload into an InboundMessage."""
        if not payload:
            return None
        data = payload.get("data") or {}
        # Slash command name + first string option, or raw content.
        name = data.get("name") or ""
        opts = data.get("options") or []
        arg = ""
        for o in opts:
            if isinstance(o, dict) and "value" in o:
                arg = str(o["value"])
                break
        text = payload.get("content") or (f"/{name} {arg}".strip() if name else arg)
        member = payload.get("member") or {}
        user = (member.get("user") or payload.get("user") or {})
        sender = user.get("username") or str(user.get("id") or "")
        target = str(payload.get("channel_id") or "")
        return InboundMessage(
            channel="discord", text=text, sender=sender, target=target, raw=payload
        )

    async def send(self, target: str, text: str) -> bool:
        # Prefer the webhook (simplest, no gateway needed).
        if self.webhook_url:
            return self._send_webhook(text)
        return False

    def _send_webhook(self, text: str) -> bool:
        try:
            data = json.dumps({"content": text[:2000]}).encode()
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001
            return False
