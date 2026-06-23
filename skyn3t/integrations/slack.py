"""Slack control surface (2.0 P7).

Guards the optional ``slack_sdk``. Outbound uses the Web API ``chat.postMessage``
via the SDK when present, else a raw HTTPS fallback (urllib) so reporting works
without the SDK. Inbound supports Slack Events API and slash-command payloads.
No bot token -> ``is_available()`` is False and methods no-op (design rule #6).
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
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
    from slack_sdk.web.async_client import AsyncWebClient as _SlackClient  # type: ignore

    _HAS_SLACK = True
except Exception:  # noqa: BLE001
    _SlackClient = None  # type: ignore
    _HAS_SLACK = False


class SlackChannel(Channel):
    name = "slack"

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        submit_fn: Optional[SubmitFn] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(event_bus=event_bus, submit_fn=submit_fn, config=config)
        self.bot_token = self.config.get("token") or env_token(
            "SLACK_BOT_TOKEN", "SLACK_TOKEN"
        )
        self._client = None
        if self.bot_token and _HAS_SLACK:
            try:
                self._client = _SlackClient(token=self.bot_token)  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                self._client = None

    def is_available(self) -> bool:
        return bool(self.bot_token)

    async def handle_inbound(self, msg: InboundMessage) -> dict[str, Any]:
        if not self.is_available():
            return {"action": "disabled", "channel": self.name}
        return await self.route_inbound(msg)

    @staticmethod
    def parse_event(payload: dict[str, Any]) -> Optional[InboundMessage]:
        """Convert a Slack Events API or slash-command payload to InboundMessage."""
        if not payload:
            return None
        # Slash command (application/x-www-form-urlencoded -> dict)
        if "command" in payload and "channel_id" in payload:
            text = f"{payload.get('command', '')} {payload.get('text', '')}".strip()
            return InboundMessage(
                channel="slack",
                text=text,
                sender=payload.get("user_name") or payload.get("user_id") or "",
                target=payload.get("channel_id") or "",
                raw=payload,
            )
        # Events API message
        event = payload.get("event") or {}
        if event.get("type") in ("message", "app_mention"):
            if event.get("bot_id"):  # ignore our own / other bots
                return None
            return InboundMessage(
                channel="slack",
                text=event.get("text") or "",
                sender=event.get("user") or "",
                target=event.get("channel") or "",
                raw=payload,
            )
        return None

    async def send(self, target: str, text: str) -> bool:
        if not self.bot_token or not target:
            return False
        if self._client is not None:
            try:
                await self._client.chat_postMessage(channel=target, text=text)
                return True
            except Exception:  # noqa: BLE001 - fall through to HTTP
                pass
        # Offload the blocking urllib call so a 10s timeout can't freeze the loop.
        return await asyncio.to_thread(self._send_http, target, text)

    def _send_http(self, target: str, text: str) -> bool:
        try:
            data = urllib.parse.urlencode({"channel": target, "text": text}).encode()
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=data,
                headers={"Authorization": f"Bearer {self.bot_token}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", "replace")
                try:
                    return bool(json.loads(body).get("ok"))
                except Exception:  # noqa: BLE001
                    return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001
            return False
