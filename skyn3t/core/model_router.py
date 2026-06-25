"""Tiered, cost-aware model routing.

Resolves a *stage tier* (and optional per-file hint) to a concrete model id.
Defaults to free OpenRouter models; honors the free-only / no-claude policy
and manual per-tier locks in ``data/model_tier_overrides.json``.

This is the routing *interface* + a deterministic default policy. The learned
router (2.0 backlog P2) plugs in by overriding :meth:`resolve`.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

import structlog

from skyn3t.config.settings import Settings, get_settings

log = structlog.get_logger(__name__)


class Tier(StrEnum):
    CHEAP = "cheap"      # brainstorm, research fan-out, boilerplate
    UI = "ui"            # frontend files
    BACKEND = "backend"  # server/API files
    STRONG = "strong"    # architecture, review, hard reasoning
    DOCS = "docs"        # writer / documentation


# Free defaults — a FALLBACK only. OpenRouter retires ":free" model IDs ~daily
# (the old deepseek-*:free defaults started 404-ing, which silently degraded every
# build to the offline scaffold). The router now self-heals from the LIVE catalog
# (see _valid_free_model); these are the last-known-good ids used when the live
# fetch is unavailable (offline).
_FREE_DEFAULTS: dict[Tier, str] = {
    Tier.CHEAP: "qwen/qwen3-coder:free",
    Tier.UI: "qwen/qwen3-coder:free",
    Tier.BACKEND: "qwen/qwen3-coder:free",
    Tier.STRONG: "qwen/qwen3-next-80b-a3b-instruct:free",
    Tier.DOCS: "qwen/qwen3-coder:free",
}

# Paid defaults used when free_only=0.
_PAID_DEFAULTS: dict[Tier, str] = {
    Tier.CHEAP: "deepseek/deepseek-chat",
    Tier.UI: "deepseek/deepseek-chat",
    Tier.BACKEND: "deepseek/deepseek-chat",
    Tier.STRONG: "deepseek/deepseek-r1",
    Tier.DOCS: "deepseek/deepseek-chat",
}

# When a configured :free model is no longer in the live catalog, substitute a
# valid one preferring these markers per tier (best-for-purpose first).
_FREE_TIER_PREFS: dict[Tier, tuple[str, ...]] = {
    Tier.CHEAP: ("qwen3-coder", "coder", "qwen3", "llama-3.3", "deepseek", "qwen"),
    Tier.UI: ("qwen3-coder", "coder", "qwen3", "llama-3.3", "deepseek", "qwen"),
    Tier.BACKEND: ("qwen3-coder", "coder", "qwen3", "deepseek", "llama-3.3", "qwen"),
    Tier.DOCS: ("qwen3-coder", "qwen3", "llama-3.3", "deepseek", "qwen"),
    Tier.STRONG: ("qwen3-next", "llama-3.3-70b", "deepseek-r1", "qwen3", "deepseek", "qwen"),
}

# Live ":free" catalog cache (id list), refreshed at most every _LIVE_TTL seconds.
_LIVE_FREE_IDS: list[str] | None = None
_LIVE_FREE_AT = 0.0
_LIVE_TTL = 3600.0


def live_free_model_ids(timeout: float = 8.0) -> list[str]:
    """Current OpenRouter ``:free`` model ids (cached). [] when unreachable so the
    caller keeps the hardcoded fallback. Public endpoint — no API key needed."""
    global _LIVE_FREE_AT, _LIVE_FREE_IDS
    import time as _t
    now = _t.time()
    if _LIVE_FREE_IDS is not None and (now - _LIVE_FREE_AT) < _LIVE_TTL:
        return _LIVE_FREE_IDS
    fresh: list[str] = []
    try:
        import json as _j
        import urllib.request as _u
        req = _u.Request("https://openrouter.ai/api/v1/models",
                         headers={"User-Agent": "skyn3t"})
        data = _j.loads(_u.urlopen(req, timeout=timeout).read())["data"]
        fresh = [m["id"] for m in data
                 if isinstance(m.get("id"), str) and m["id"].endswith(":free")]
    except Exception as exc:  # noqa: BLE001 - offline / API down -> keep fallback
        log.warning("router.live_models_unavailable", error=str(exc)[:120])
    _LIVE_FREE_IDS = fresh
    _LIVE_FREE_AT = now
    return fresh

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

        model = self._apply_policy(model, tier)
        # Self-heal a retired :free id against OpenRouter's LIVE catalog (only
        # worth a fetch when an OpenRouter key is configured — otherwise the
        # backend is claude/stub and the model id is unused).
        if (self.settings.free_only and model.endswith(":free")
                and str(getattr(self.settings, "openrouter_api_key", "") or "").strip()):
            model = self._valid_free_model(model, tier)
        return model

    def _valid_free_model(self, model: str, tier: Tier) -> str:
        """Substitute a live valid :free model when ``model`` was retired from the
        catalog (OpenRouter rotates these ~daily). Returns ``model`` unchanged when
        the live list is unknown (offline) or still contains it."""
        live = live_free_model_ids()
        if not live or model in live:
            return model
        for marker in _FREE_TIER_PREFS.get(tier, ()):  # best-for-tier first
            for m in live:
                if marker in m.lower():
                    log.info("router.free_model_substituted", tier=tier.value, was=model, now=m)
                    return m
        return live[0]

    def _apply_policy(self, model: str, tier: Tier) -> str:
        low = model.lower()
        # no-claude: rewrite Claude picks to a non-Claude tier default — staying
        # in the user's PAID catalog when they're paid (free_only=False). Forcing
        # them onto :free would be a silent downgrade; no_claude means "avoid
        # Claude", not "drop to free".
        if self.settings.no_claude and any(m in low for m in _CLAUDE_MARKERS):
            model = _FREE_DEFAULTS[tier] if self.settings.free_only else _PAID_DEFAULTS[tier]
            log.info("router.rewrote_claude", tier=tier.value, to=model)
        # free-only: force a :free model.
        if self.settings.free_only and not model.endswith(":free"):
            model = _FREE_DEFAULTS[tier]
        return model

    def describe(self) -> dict[str, str]:
        return {t.value: self.resolve(t) for t in Tier}
