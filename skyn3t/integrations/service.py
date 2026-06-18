"""Live messaging service — the bot that connects the swarm to chat.

* **Inbound** (opt-in, start/stop from the dashboard): a Telegram long-poll loop
  (``getUpdates`` — no public URL needed) that turns chat messages into briefs
  and runs them through the studio (``/build <idea>``, ``/approve <id>``,
  ``/ping``, or bare text).
* **Outbound** (always on): build-completion notifications pushed to each
  configured channel's default target.

Everything is credential-gated and degrades to a no-op when a channel isn't
configured (design rule #6). Telegram is the live inbound path; Discord/Slack
are outbound-only here (their inbound needs a gateway/socket connection).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog

from skyn3t.core.events import Event, EventBus, EventType

log = structlog.get_logger(__name__)

_TARGET_ENV = {
    "telegram": "TELEGRAM_CHAT_ID",
    "discord": "DISCORD_CHANNEL_ID",
    "slack": "SLACK_CHANNEL",
}


class MessagingService:
    def __init__(self, event_bus: EventBus, settings: Any, studio: Any | None = None) -> None:
        self.event_bus = event_bus
        self.settings = settings
        self.studio = studio
        self.channels: dict[str, Any] = {}
        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._build_channels()
        # Outbound notifications are always wired (they no-op if no channel).
        self._unsubs = [
            event_bus.subscribe(EventType.BUILD_COMPLETED, self._on_build),
            event_bus.subscribe(EventType.BUILD_FAILED, self._on_build),
        ]

    # ---- channel construction -------------------------------------------
    def _build_channels(self) -> None:
        async def _run_build(task: Any) -> None:
            brief = (getattr(task, "payload", {}) or {}).get("brief", "")
            if brief and self.studio is not None:
                asyncio.ensure_future(self._safe_build(brief))

        self.channels = {}
        for mod, cls in (("telegram", "TelegramChannel"),
                         ("discord", "DiscordChannel"), ("slack", "SlackChannel")):
            try:
                m = __import__(f"skyn3t.integrations.{mod}", fromlist=[cls])
                ch = getattr(m, cls)(event_bus=self.event_bus, submit_fn=_run_build)
                if ch.is_available():
                    self.channels[mod] = ch
            except Exception:  # noqa: BLE001 - a broken channel never blocks the rest
                continue

    async def _safe_build(self, brief: str) -> None:
        try:
            await self.studio.start(brief, extra={"source": "messaging"})
        except Exception as exc:  # noqa: BLE001
            log.warning("messaging.build_failed", error=str(exc))

    # ---- outbound notifications -----------------------------------------
    async def _on_build(self, ev: Event) -> None:
        p = ev.payload or {}
        slug = p.get("slug", "build")
        verdict = p.get("verdict", "")
        score = p.get("score")
        if ev.type == EventType.BUILD_FAILED:
            text = f"🛑 SkyN3t build '{slug}' failed."
        else:
            ok = verdict == "go" and p.get("status") == "completed"
            text = f"{'✅' if ok else '⚠️'} SkyN3t build '{slug}': {verdict or 'done'} · score {score}"
        await self.notify(text)

    async def notify(self, text: str) -> int:
        sent = 0
        for name, ch in self.channels.items():
            target = self._target(name)
            if not target:
                continue
            try:
                if await ch.send(target, text):
                    sent += 1
            except Exception:  # noqa: BLE001
                pass
        return sent

    @staticmethod
    def _target(name: str) -> str | None:
        var = _TARGET_ENV.get(name)
        if not var:
            return None
        return os.environ.get(f"SKYN3T_{var}") or os.environ.get(var)

    # ---- inbound listener lifecycle -------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "channels": {n: True for n in self.channels},
            "outbound_targets": {n: bool(self._target(n)) for n in _TARGET_ENV},
        }

    async def start_listeners(self) -> dict[str, Any]:
        self._build_channels()  # pick up tokens configured since construction
        tg = self.channels.get("telegram")
        if tg is None:
            return {"running": False, "error": "no telegram bot token configured"}
        if self._running:
            return self.status()
        self._running = True
        self._poll_task = asyncio.ensure_future(self._poll_telegram(tg))
        log.info("messaging.listener_started", channel="telegram")
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        log.info("messaging.listener_stopped")
        return self.status()

    async def _poll_telegram(self, channel: Any) -> None:
        try:
            import httpx
        except Exception:  # noqa: BLE001
            self._running = False
            return
        offset: int | None = None
        url = f"https://api.telegram.org/bot{channel.token}/getUpdates"
        async with httpx.AsyncClient(timeout=35.0) as client:
            while self._running:
                try:
                    params: dict[str, Any] = {"timeout": 25}
                    if offset is not None:
                        params["offset"] = offset
                    resp = await client.get(url, params=params)
                    for upd in resp.json().get("result", []):
                        offset = int(upd["update_id"]) + 1
                        msg = channel.parse_update(upd)
                        if msg:
                            await channel.handle_inbound(msg)
                except asyncio.CancelledError:  # pragma: no cover - shutdown
                    break
                except Exception as exc:  # noqa: BLE001 - keep polling through errors
                    log.warning("telegram.poll_error", error=str(exc)[:160])
                    await asyncio.sleep(3.0)
