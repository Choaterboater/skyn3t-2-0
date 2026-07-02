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

# Paid defaults used when free_only=0. Kept CURRENT (not the 2024 deepseek-chat) —
# OpenRouter rotates models constantly, so these are last-known-good recent ids;
# the live-catalog resolver (newest_paid_model) supersedes them when reachable.
_PAID_DEFAULTS: dict[Tier, str] = {
    Tier.CHEAP: "deepseek/deepseek-v4-flash",
    Tier.UI: "deepseek/deepseek-v4-flash",
    Tier.BACKEND: "deepseek/deepseek-v4-flash",
    Tier.STRONG: "deepseek/deepseek-v4-flash",
    Tier.DOCS: "deepseek/deepseek-v4-flash",
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


# Full live catalog cache (id + created epoch), for auto-picking the NEWEST model
# in a family so a pin never goes stale (OpenRouter rotates models constantly).
_LIVE_CATALOG: list[dict] | None = None
_LIVE_CATALOG_AT = 0.0


def live_catalog(timeout: float = 8.0) -> list[dict]:
    """Current OpenRouter catalog as ``[{"id","created"}]`` (cached, public endpoint).
    Returns ``[]`` when unreachable so callers keep their static fallback."""
    global _LIVE_CATALOG, _LIVE_CATALOG_AT
    import time as _t
    now = _t.time()
    if _LIVE_CATALOG is not None and (now - _LIVE_CATALOG_AT) < _LIVE_TTL:
        return _LIVE_CATALOG
    fresh: list[dict] = []
    try:
        import json as _j
        import urllib.request as _u
        req = _u.Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": "skyn3t"})
        data = _j.loads(_u.urlopen(req, timeout=timeout).read())["data"]
        fresh = [{"id": m["id"], "created": int(m.get("created", 0) or 0)}
                 for m in data if isinstance(m.get("id"), str)]
    except Exception as exc:  # noqa: BLE001 - offline / API down -> keep fallback
        log.warning("router.catalog_unavailable", error=str(exc)[:120])
    _LIVE_CATALOG = fresh
    _LIVE_CATALOG_AT = now
    return fresh


# Substrings that disqualify a model from "newest" auto-pick: reasoning/base/
# experimental/preview variants aren't general codegen-chat models.
# Previews ARE allowed in the coder pool (opted in) — the cortex tournament learns
# which previews are actually good and down-ranks the rest, so we don't pre-filter them.
_NEWEST_EXCLUDE = ("r1", "distill", "reasoner", "-base", "-exp", "thinking")
# Explicit ids that stay eligible even if they ever match an exclude substring (escape hatch).
_NEWEST_ALLOW = ()

# Strong code-capable families on OpenRouter. ``newest:coder`` picks the freshest
# across ALL of them (so it tracks the genuinely-newest coder, e.g. a 2026 kimi/
# minimax/qwen3-coder, not just the newest deepseek). Keep this list current as
# new strong coders ship.
_CODER_FAMILIES = ("kimi-k2", "qwen3-coder", "deepseek-v3", "minimax-m",
                   "glm-4", "codestral", "grok-code", "hy3")


def newest_paid_model(family: str) -> str | None:
    """The NEWEST live, non-free, non-experimental catalog id matching ``family``.

    ``family="coder"`` -> newest across ALL strong coder families; otherwise a
    substring match (e.g. ``"deepseek-v3"`` -> newest deepseek-v3.x). ``None`` when
    the catalog is unreachable or nothing matches — caller keeps a static fallback."""
    fam = family.lower()
    families = _CODER_FAMILIES if fam == "coder" else (fam,)
    cands = [(m["id"], m["created"]) for m in live_catalog()
             if any(f in m["id"].lower() for f in families) and not m["id"].endswith(":free")
             and (m["id"] in _NEWEST_ALLOW
                  or not any(x in m["id"].lower() for x in _NEWEST_EXCLUDE))]
    return max(cands, key=lambda c: c[1])[0] if cands else None


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

        # ``newest:<family>`` auto-resolves to the freshest matching model from the
        # LIVE catalog (so a pin never ages into a stale model). Falls back to the
        # paid default for the tier when the catalog is unreachable.
        if model.startswith("newest:"):
            family = model.split(":", 1)[1]
            live = newest_paid_model(family)
            if live:
                log.info("router.newest_resolved", family=family, model=live)
                model = live
            else:
                model = _PAID_DEFAULTS.get(tier, "deepseek/deepseek-v4-flash")

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

    def fallback_candidates(self, tier: Tier, primary: str | None = None) -> list[str]:
        """Ordered alternative model ids to try when ``primary`` fails with a
        model-level error (retired :free id, invalid model, no endpoints) or
        persistent transient errors.

        Live-catalog picks come first (prefer a currently-valid model), then the
        static per-tier defaults as an *offline backstop* — so a build always has
        SOMETHING to fall over to even when the router can't reach OpenRouter (the
        exact condition under which the original dead-model incident hid). Policy
        (``free_only`` / ``no_claude``) is applied and ``primary`` is removed.
        Best-effort: never raises."""
        cands: list[str] = []
        try:
            if self.settings.free_only:
                live = live_free_model_ids()  # [] when offline -> static backstop only
                for marker in _FREE_TIER_PREFS.get(tier, ()):  # best-for-tier first
                    for m in live:
                        if marker in m.lower() and m not in cands:
                            cands.append(m)
                for t in Tier:  # static offline backstop across every tier
                    d = _FREE_DEFAULTS[t]
                    if d not in cands:
                        cands.append(d)
            else:
                for fam in _CODER_FAMILIES:  # newest live model per strong-coder family
                    m = newest_paid_model(fam)
                    if m and m not in cands:
                        cands.append(m)
                for t in Tier:
                    d = _PAID_DEFAULTS[t]
                    if d not in cands:
                        cands.append(d)
        except Exception as exc:  # noqa: BLE001 - fallback resolution must never raise
            log.warning("router.fallback_candidates_error", error=str(exc)[:120])
            cands = list((_FREE_DEFAULTS if self.settings.free_only else _PAID_DEFAULTS).values())
        out: list[str] = []
        for m in cands:
            m2 = self._apply_policy(m, tier)  # honor no_claude / free_only
            if m2 and m2 != primary and m2 not in out:
                out.append(m2)
        return out

    def describe(self) -> dict[str, str]:
        return {t.value: self.resolve(t) for t in Tier}
