"""Telegram control surface (2.0 P7).

Guards the optional ``python-telegram-bot`` SDK. With no bot token, or with the
SDK absent, ``is_available()`` returns False and every method degrades to a
no-op instead of crashing (design rule #6).

Outbound ``send`` falls back to a dependency-free raw HTTPS call (urllib) when
the SDK is missing but a token is present, so reporting still works in lean
environments. Inbound messages are normalized and routed through the shared
``Channel.route_inbound`` brief/approval flow.
"""

from __future__ import annotations

import asyncio
import urllib.parse
import urllib.request
from typing import Any

from skyn3t.core.events import EventBus
from skyn3t.integrations.channels import (
    Channel,
    InboundMessage,
    SubmitFn,
    env_token,
)

try:  # optional heavy dep
    import telegram as _telegram  # type: ignore

    _HAS_TELEGRAM = True
except Exception:  # noqa: BLE001 - ImportError or partial install
    _telegram = None  # type: ignore
    _HAS_TELEGRAM = False


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(
        self,
        event_bus: EventBus | None = None,
        submit_fn: SubmitFn | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(event_bus=event_bus, submit_fn=submit_fn, config=config)
        self.token = (self.config.get("token") or env_token("TELEGRAM_BOT_TOKEN"))
        self._bot = None
        if self.token and _HAS_TELEGRAM:
            try:
                self._bot = _telegram.Bot(token=self.token)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                self._bot = None

    def is_available(self) -> bool:
        # Available if we have a token; SDK is optional thanks to HTTP fallback.
        return bool(self.token)

    async def handle_inbound(self, msg: InboundMessage) -> dict[str, Any]:
        if not self.is_available():
            return {"action": "disabled", "channel": self.name}
        return await self.route_inbound(msg)

    @staticmethod
    def parse_update(update: dict[str, Any]) -> InboundMessage | None:
        """Convert a Telegram webhook/poll update dict into an InboundMessage."""
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
        )
        if not message:
            return None
        text = message.get("text") or message.get("caption") or ""
        chat = message.get("chat") or {}
        frm = message.get("from") or {}
        sender = frm.get("username") or str(frm.get("id") or chat.get("id") or "")
        return InboundMessage(
            channel="telegram",
            text=text,
            sender=sender,
            target=str(chat.get("id") or ""),
            raw=update,
        )

    async def send(self, target: str, text: str) -> bool:
        if not self.token or not target:
            return False
        if self._bot is not None:
            try:
                await _maybe_await(self._bot.send_message(chat_id=target, text=text))
                return True
            except Exception:  # noqa: BLE001 - fall through to HTTP
                pass
        # Offload the blocking urllib call so a 10s timeout can't freeze the loop.
        return await asyncio.to_thread(self._send_http, target, text)

    def _send_http(self, target: str, text: str) -> bool:
        """Dependency-free outbound via the Bot API. Best-effort, never raises."""
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": target, "text": text}).encode()
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001
            return False


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable (PTB returns coroutines)."""
    if hasattr(value, "__await__"):
        return await value
    return value
