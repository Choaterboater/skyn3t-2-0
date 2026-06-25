"""Prometheus metrics with a no-op fallback.

If ``prometheus_client`` is installed, real Counter/Gauge/Histogram objects are
created against a private registry. If it is absent (rule #3), drop-in no-op
shims keep every call site working — ``.inc()``, ``.set()``, ``.observe()``,
``.labels()`` all succeed silently and ``render()`` returns an empty payload.

Import has no side effects and starts no HTTP server.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

try:  # pragma: no cover - import guard
    from prometheus_client import (  # type: ignore
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    from prometheus_client.exposition import CONTENT_TYPE_LATEST  # type: ignore
    _HAS_PROM = True
except ImportError:  # pragma: no cover
    _HAS_PROM = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


# ---- no-op shims --------------------------------------------------------
class _NoopMetric:
    """Accepts any metric call and does nothing."""

    def labels(self, *args: Any, **kwargs: Any) -> _NoopMetric:
        return self

    def inc(self, amount: float = 1.0) -> None:
        return None

    def dec(self, amount: float = 1.0) -> None:
        return None

    def set(self, value: float) -> None:
        return None

    def observe(self, value: float) -> None:
        return None


class MetricsRegistry:
    """Facade over prometheus_client or a no-op fallback.

    The standard SkyN3t metrics are created up front so call sites can rely on
    them existing whether or not prometheus is installed.
    """

    def __init__(self, namespace: str = "skyn3t") -> None:
        self.enabled = _HAS_PROM
        self._ns = namespace
        self.registry: Any = CollectorRegistry() if _HAS_PROM else None
        self._metrics: dict[str, Any] = {}
        self._build_standard()

    # ---- factory helpers -------------------------------------------------
    def counter(self, name: str, doc: str, labels: tuple[str, ...] = ()) -> Any:
        return self._get_or_create("counter", name, doc, labels)

    def gauge(self, name: str, doc: str, labels: tuple[str, ...] = ()) -> Any:
        return self._get_or_create("gauge", name, doc, labels)

    def histogram(self, name: str, doc: str, labels: tuple[str, ...] = ()) -> Any:
        return self._get_or_create("histogram", name, doc, labels)

    def _get_or_create(self, kind: str, name: str, doc: str, labels: tuple[str, ...]) -> Any:
        key = f"{kind}:{name}"
        if key in self._metrics:
            return self._metrics[key]
        if not _HAS_PROM:
            metric: Any = _NoopMetric()
        else:
            full = f"{self._ns}_{name}"
            ctor = {"counter": Counter, "gauge": Gauge, "histogram": Histogram}[kind]
            try:
                metric = ctor(full, doc, labelnames=labels, registry=self.registry)
            except ValueError:
                # Duplicate registration — return the shim to stay safe.
                metric = _NoopMetric()
        self._metrics[key] = metric
        return metric

    # ---- standard set ----------------------------------------------------
    def _build_standard(self) -> None:
        self.tasks_total = self.counter("tasks_total", "Tasks processed", ("type", "outcome"))
        self.task_duration = self.histogram("task_duration_seconds", "Task duration", ("type",))
        self.builds_total = self.counter("builds_total", "Builds completed", ("status",))
        self.agents_active = self.gauge("agents_active", "Active agents")
        self.llm_calls_total = self.counter("llm_calls_total", "LLM calls", ("backend", "model"))
        self.llm_tokens_total = self.counter("llm_tokens_total", "LLM tokens", ("kind",))
        self.llm_cost_usd = self.counter("llm_cost_usd_total", "Estimated LLM spend (USD)")
        self.budget_remaining = self.gauge("budget_remaining_usd", "Remaining daily budget (USD)")
        self.events_total = self.counter("events_total", "Events published", ("type",))
        self.sandbox_runs_total = self.counter("sandbox_runs_total", "Sandbox runs", ("backend", "outcome"))

    # ---- export ----------------------------------------------------------
    def render(self) -> bytes:
        """Return the Prometheus text exposition (empty bytes if disabled)."""
        if not _HAS_PROM or self.registry is None:
            return b""
        return generate_latest(self.registry)

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE_LATEST


# Process-wide default registry, created lazily (no import-time side effects).
_default: MetricsRegistry | None = None


def get_metrics() -> MetricsRegistry:
    global _default
    if _default is None:
        _default = MetricsRegistry()
    return _default
