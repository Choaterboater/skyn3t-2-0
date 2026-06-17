"""DeliveryGateway — best-effort outbound across all channels (2.0 P7).

The gateway is the single egress point the swarm uses to reach humans:
progress reports, approval requests, build results. Its defining property is
that it *never raises* (design rule #6). Sending to an unconfigured or broken
channel returns a failure record, never an exception, so a delivery failure can
never abort a build.

Every delivery is also published on the EventBus (design rule #7) so the
dashboard and replay can see what was sent where.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.channels import Channel, ChannelRegistry


@dataclass(slots=True)
class DeliveryResult:
    channel: str
    target: str
    ok: bool
    error: Optional[str] = None


@dataclass(slots=True)
class DeliveryReport:
    text: str
    results: list[DeliveryResult] = field(default_factory=list)

    @property
    def any_ok(self) -> bool:
        return any(r.ok for r in self.results)

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "any_ok": self.any_ok,
            "all_ok": self.all_ok,
            "results": [
                {"channel": r.channel, "target": r.target, "ok": r.ok, "error": r.error}
                for r in self.results
            ],
        }


class DeliveryGateway:
    """Routes outbound messages to channels, tolerating any failure."""

    def __init__(
        self,
        registry: ChannelRegistry,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.registry = registry
        self.event_bus = event_bus
        #: optional default reply targets per channel name
        self.default_targets: dict[str, str] = {}

    async def send(self, channel_name: str, target: str, text: str) -> DeliveryResult:
        """Send to one channel. Returns a result; never raises."""
        channel = self.registry.get(channel_name)
        if channel is None:
            res = DeliveryResult(channel_name, target, False, "unknown_channel")
        elif not channel.is_available():
            res = DeliveryResult(channel_name, target, False, "unavailable")
        elif not target:
            res = DeliveryResult(channel_name, target, False, "no_target")
        else:
            res = await self._try_send(channel, target, text)
        await self._emit(res, text)
        return res

    async def broadcast(self, text: str, targets: Optional[dict[str, str]] = None) -> DeliveryReport:
        """Send ``text`` to every available channel. Best-effort, never raises.

        ``targets`` maps channel name -> reply target; falls back to
        ``default_targets`` then to the channel config's ``default_target``.
        """
        report = DeliveryReport(text=text)
        targets = targets or {}
        for channel in self.registry.available():
            target = (
                targets.get(channel.name)
                or self.default_targets.get(channel.name)
                or channel.config.get("default_target", "")
            )
            if not target:
                res = DeliveryResult(channel.name, "", False, "no_target")
            else:
                res = await self._try_send(channel, target, text)
            await self._emit(res, text)
            report.results.append(res)
        return report

    async def _try_send(self, channel: Channel, target: str, text: str) -> DeliveryResult:
        try:
            ok = await channel.send(target, text)
            return DeliveryResult(channel.name, target, bool(ok), None if ok else "send_failed")
        except Exception as exc:  # noqa: BLE001 - rule #6, gateway never raises
            return DeliveryResult(channel.name, target, False, str(exc))

    async def _emit(self, res: DeliveryResult, text: str) -> None:
        if self.event_bus is None:
            return
        try:
            await self.event_bus.emit(
                EventType.SYSTEM,
                "delivery.gateway",
                {
                    "channel": res.channel,
                    "target": res.target,
                    "ok": res.ok,
                    "error": res.error,
                    "preview": text[:120],
                },
            )
        except Exception:  # noqa: BLE001
            pass
