"""Tiered, cost-aware model routing.

Resolves a *stage tier* (and optional per-file hint) to a concrete model id.
Defaults to free OpenRouter models; honors the free-only / no-claude policy
and manual per-tier locks in ``data/model_tier_overrides.json``.

This is the routing *interface* + a deterministic default policy. The learned
router (2.0 backlog P2) plugs in by overriding :meth:`resolve`.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

import structlog

from skyn3t.config.settings import Settings, get_settings

log = structlog.get_logger(__name__)


class Tier(str, Enum):
    CHEAP = "cheap"      # brainstorm, research fan-out, boilerplate
    UI = "ui"            # frontend files
    BACKEND = "backend"  # server/API files
    STRONG = "strong"    # architecture, review, hard reasoning
    DOCS = "docs"        # writer / documentation


# Sensible free defaults (OpenRouter ":free" catalog).
_FREE_DEFAULTS: dict[Tier, str] = {
    Tier.CHEAP: "deepseek/deepseek-chat-v3.1:free",
    Tier.UI: "deepseek/deepseek-chat-v3.1:free",
    Tier.BACKEND: "deepseek/deepseek-chat-v3.1:free",
    Tier.STRONG: "deepseek/deepseek-r1:free",
    Tier.DOCS: "deepseek/deepseek-chat-v3.1:free",
}

# Paid defaults used when free_only=0.
_PAID_DEFAULTS: dict[Tier, str] = {
    Tier.CHEAP: "deepseek/deepseek-chat",
    Tier.UI: "deepseek/deepseek-chat",
    Tier.BACKEND: "deepseek/deepseek-chat",
    Tier.STRONG: "deepseek/deepseek-r1",
    Tier.DOCS: "deepseek/deepseek-chat-v3.1:free",
}

_CLAUDE_MARKERS = ("claude", "anthropic")


class ModelRouter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._overrides = self._load_overrides()

    def _load_overrides(self) -> dict[str, str]:
        path = self.settings.data_dir / "model_tier_overrides.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception as exc:  # noqa: BLE001
                log.warning("router.overrides_unreadable", error=str(exc))
        return {}

    def resolve(self, tier: Tier, file_hint: str | None = None, task_type: str = "") -> str:
        """Return the concrete model id for a tier (+ optional file hint).

        ``task_type`` is accepted (and ignored) here so callers can pass it
        uniformly; only the LearnedModelRouter subclass uses it."""
        # Per-file specialization: route UI vs backend by extension.
        if file_hint:
            ext = Path(file_hint).suffix.lower()
            if ext in {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"}:
                tier = Tier.UI
            elif ext in {".py", ".go", ".rs", ".java", ".rb", ".sql"}:
                tier = Tier.BACKEND

        # Manual lock wins.
        if tier.value in self._overrides:
            model = self._overrides[tier.value]
        else:
            base = _FREE_DEFAULTS if self.settings.free_only else _PAID_DEFAULTS
            model = base[tier]

        return self._apply_policy(model, tier)

    def _apply_policy(self, model: str, tier: Tier) -> str:
        low = model.lower()
        # no-claude: rewrite Claude picks to a non-Claude tier default.
        if self.settings.no_claude and any(m in low for m in _CLAUDE_MARKERS):
            model = _FREE_DEFAULTS[tier]
            log.info("router.rewrote_claude", tier=tier.value, to=model)
        # free-only: force a :free model.
        if self.settings.free_only and not model.endswith(":free"):
            model = _FREE_DEFAULTS[tier]
        return model

    def describe(self) -> dict[str, str]:
        return {t.value: self.resolve(t) for t in Tier}
