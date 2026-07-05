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
import math
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
# Use ``newest:<family>`` so each tier can drift toward a different family
# automatically without hardcoding one fixed model to every tier forever.
_PAID_DEFAULTS: dict[Tier, str] = {
    Tier.CHEAP: "newest:qwen",
    Tier.UI: "newest:codestral",
    Tier.BACKEND: "newest:kimi-k2",
    Tier.STRONG: "newest:llama-3.3",
    Tier.DOCS: "newest:deepseek-v3",
}

_PAID_FALLBACK_CACHE = "model_router_paid_fallback.json"

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
    """Current OpenRouter catalog (cached, public endpoint).

    Keep the full model records. OpenRouter now exposes benchmark, architecture,
    context, and pricing metadata in this endpoint; the router uses those fields
    to pick current high-signal models instead of only matching old family names.
    Returns ``[]`` when unreachable so callers keep their static fallback.
    """
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
        fresh = []
        for m in data:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                continue
            item = dict(m)
            item["created"] = int(item.get("created", 0) or 0)
            fresh.append(item)
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

# Models can score well in public aggregate benchmarks while performing poorly
# in this product's build loop. Keep these out of automatic picks; manual pins
# still work because this filter only applies to live catalog scoring.
_AUTO_ROUTE_EXCLUDE = (*_NEWEST_EXCLUDE, "fable")

# Strong code-capable families on OpenRouter. ``newest:coder`` picks the freshest
# across ALL of them (so it tracks the genuinely-newest coder, e.g. a 2026 kimi/
# minimax/qwen3-coder, not just the newest deepseek). Keep this list current as
# new strong coders ship.
_CODER_FAMILIES = ("kimi-k2", "qwen3-coder", "deepseek-v3", "minimax-m",
                   "glm-4", "codestral", "grok-code", "hy3")

_PROFILE_ALIASES = {
    "": "balanced",
    "cheap": "cheap_learned",
    "cheap_learned": "cheap_learned",
    "fast": "fast",
    "balanced": "balanced",
    "best": "best_quality",
    "best_quality": "best_quality",
    "full_app": "best_quality",
    "manual": "balanced",
}

_TIER_ARENA_WEIGHTS: dict[Tier, dict[str, float]] = {
    Tier.CHEAP: {
        "codecategories": 0.8,
        "webapps": 0.5,
        "fullstack": 0.5,
        "website": 0.3,
        "uicomponent": 0.3,
    },
    Tier.UI: {
        "uicomponent": 1.2,
        "website": 1.1,
        "webapps": 0.8,
        "dataviz": 0.5,
        "svg": 0.4,
        "3d": 0.3,
        "codecategories": 0.6,
    },
    Tier.BACKEND: {
        "codecategories": 1.1,
        "fullstack": 1.0,
        "webapps": 0.8,
        "androidnative": 0.25,
        "mobileapps": 0.25,
    },
    Tier.STRONG: {
        "codecategories": 1.0,
        "fullstack": 1.0,
        "webapps": 0.7,
        "gamedev": 0.4,
        "dataviz": 0.3,
        "uicomponent": 0.3,
    },
    Tier.DOCS: {
        "htmlslides": 0.8,
        "agentichtmlslides": 0.8,
        "dataviz": 0.4,
        "website": 0.35,
        "codecategories": 0.3,
    },
}

_TIER_INDEX_WEIGHTS: dict[Tier, dict[str, float]] = {
    Tier.CHEAP: {"coding_index": 0.7, "agentic_index": 0.3, "intelligence_index": 0.2},
    Tier.UI: {"coding_index": 0.7, "agentic_index": 0.35, "intelligence_index": 0.2},
    Tier.BACKEND: {"coding_index": 0.8, "agentic_index": 0.45, "intelligence_index": 0.2},
    Tier.STRONG: {"coding_index": 0.6, "agentic_index": 0.5, "intelligence_index": 0.5},
    Tier.DOCS: {"intelligence_index": 0.65, "agentic_index": 0.25, "coding_index": 0.25},
}

_PROFILE_WEIGHTS = {
    "cheap_learned": {"quality": 0.62, "recency": 0.20, "context": 0.06, "cost": 1.15},
    "fast": {"quality": 0.58, "recency": 0.18, "context": 0.04, "cost": 0.95},
    "balanced": {"quality": 1.00, "recency": 0.22, "context": 0.09, "cost": 0.38},
    "best_quality": {"quality": 1.15, "recency": 0.14, "context": 0.12, "cost": 0.04},
}

_FAST_MARKERS = ("flash", "mini", "lite", "fast", "turbo", "small")


def _normalized_profile(profile: str = "balanced") -> str:
    return _PROFILE_ALIASES.get(str(profile or "").strip().lower().replace("-", "_"), "balanced")


def _parse_price(raw: object) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _model_token_cost(model: dict) -> float | None:
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = _parse_price(pricing.get("prompt") or pricing.get("input"))
    completion = _parse_price(pricing.get("completion") or pricing.get("output"))
    if prompt is None and completion is None:
        request = _parse_price(pricing.get("request"))
        return request
    return float(prompt or 0.0) * 0.55 + float(completion or 0.0) * 0.45


def _model_supports_text(model: dict) -> bool:
    arch = model.get("architecture")
    if not isinstance(arch, dict):
        return True
    outputs = arch.get("output_modalities")
    if not isinstance(outputs, list):
        return True
    return "text" in {str(o).lower() for o in outputs}


def _arena_quality(model: dict, tier: Tier) -> float:
    benchmarks = model.get("benchmarks")
    if not isinstance(benchmarks, dict):
        return 0.0
    rows = benchmarks.get("design_arena")
    if not isinstance(rows, list):
        return 0.0
    weights = _TIER_ARENA_WEIGHTS.get(tier, {})
    weighted = 0.0
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "").lower()
        weight = weights.get(category)
        if not weight:
            continue
        try:
            elo = float(row.get("elo", 0) or 0)
        except (TypeError, ValueError):
            continue
        if elo <= 0:
            continue
        # OpenRouter arena ELOs currently cluster around 1150-1420; this maps
        # them into roughly the same 0-100 range as artificial_analysis indexes.
        score = max(0.0, min(100.0, (elo - 1000.0) / 4.5))
        rank = row.get("rank")
        try:
            rank_n = int(rank)
        except (TypeError, ValueError):
            rank_n = 0
        if rank_n > 0:
            score += max(0.0, 10.0 - min(10.0, (rank_n - 1) * 0.75))
        weighted += score * weight
        total += weight
    return weighted / total if total else 0.0


def _index_quality(model: dict, tier: Tier) -> float:
    benchmarks = model.get("benchmarks")
    if not isinstance(benchmarks, dict):
        return 0.0
    analysis = benchmarks.get("artificial_analysis")
    if not isinstance(analysis, dict):
        return 0.0
    weighted = 0.0
    total = 0.0
    for key, weight in _TIER_INDEX_WEIGHTS.get(tier, {}).items():
        try:
            value = float(analysis.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        weighted += max(0.0, min(100.0, value)) * weight
        total += weight
    return weighted / total if total else 0.0


def _heuristic_quality(model_id: str, tier: Tier) -> float:
    low = model_id.lower()
    score = 0.0
    if "glm-5" in low:
        score += 46
    if "claude" in low or "sonnet" in low or "fable" in low:
        score += 48
    if "kimi-k2" in low:
        score += 42
    if "qwen3-coder" in low:
        score += 38
    if "poolside" in low or "laguna" in low or "north-mini-code" in low:
        score += 34
    if "deepseek-v3" in low:
        score += 30
    if tier is Tier.UI and any(x in low for x in ("design", "ui", "frontend")):
        score += 8
    if tier is Tier.BACKEND and any(x in low for x in ("code", "coder", "glm", "kimi")):
        score += 6
    return min(score, 58.0)


def _catalog_model_score(model: dict, tier: Tier, profile: str, newest_created: int) -> float | None:
    model_id = str(model.get("id") or "").strip()
    if not model_id or model_id.endswith(":free"):
        return None
    low = model_id.lower()
    if model_id.startswith("~") or any(x in low for x in _AUTO_ROUTE_EXCLUDE):
        return None
    if not _model_supports_text(model):
        return None
    token_cost = _model_token_cost(model)
    if token_cost is None:
        return None

    arena = _arena_quality(model, tier)
    indexes = _index_quality(model, tier)
    heuristic = _heuristic_quality(model_id, tier)
    quality = max(arena, indexes, heuristic)
    if quality <= 0:
        return None

    created = int(model.get("created", 0) or 0)
    recency = 0.0 if not newest_created or not created else max(
        0.0,
        min(100.0, 100.0 - ((newest_created - created) / 86400.0) * 0.8),
    )
    context = int(model.get("context_length", 0) or 0)
    context_score = min(100.0, math.log10(max(context, 1)) * 18.0)
    cost_penalty = math.log10((token_cost * 1_000_000.0) + 1.0) * 22.0
    weights = _PROFILE_WEIGHTS[_normalized_profile(profile)]
    score = (
        quality * weights["quality"]
        + recency * weights["recency"]
        + context_score * weights["context"]
        - cost_penalty * weights["cost"]
    )
    if _normalized_profile(profile) == "fast" and any(m in low for m in _FAST_MARKERS):
        score += 8.0
    return score


def best_paid_model(tier: Tier, profile: str = "balanced", *, no_claude: bool = False) -> str | None:
    """Best current paid model for ``tier`` from OpenRouter's live catalog.

    This is benchmark-aware, not family-name-only. It prefers current models that
    score well for the tier's job, while profile weights tune cost/quality bias.
    Returns ``None`` when the live catalog is unavailable or too sparse; callers
    keep the older newest-family fallback in that case.
    """
    catalog = live_catalog()
    newest_created = max((int(m.get("created", 0) or 0) for m in catalog), default=0)
    scored: list[tuple[float, int, str]] = []
    for m in catalog:
        model_id = str(m.get("id") or "").strip()
        if no_claude and any(marker in model_id.lower() for marker in _CLAUDE_MARKERS):
            continue
        score = _catalog_model_score(m, tier, profile, newest_created)
        if score is None:
            continue
        scored.append((score, int(m.get("created", 0) or 0), model_id))
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2]


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


def _fallback_free_model(tier: Tier) -> str:
    """Best-effort :free model substitute used when paid resolution fails."""
    live = live_free_model_ids()
    if not live:
        return _FREE_DEFAULTS[tier]
    for marker in _FREE_TIER_PREFS.get(tier, ()):
        for model in live:
            if marker in model.lower():
                return model
    return live[0]


_CLAUDE_MARKERS = ("claude", "anthropic")


class ModelRouter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._overrides = self._load_overrides()
        self._paid_fallback_cache = self._load_paid_fallback_cache()

    def _load_paid_fallback_cache(self) -> dict[str, str]:
        path = self.settings.data_dir / _PAID_FALLBACK_CACHE
        try:
            if not path.exists():
                return {}
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict):
                return {}
            out: dict[str, str] = {}
            for k, v in raw.items():
                if k in {t.value for t in Tier} and isinstance(v, str) and v.strip():
                    out[str(k)] = v.strip()
            return out
        except Exception as exc:  # noqa: BLE001 - cache can fail safely
            log.warning("router.paid_cache_read_error", error=str(exc)[:120])
            return {}

    def _save_paid_fallback_cache(self) -> None:
        path = self.settings.data_dir / _PAID_FALLBACK_CACHE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._paid_fallback_cache, indent=2))
        except Exception as exc:  # noqa: BLE001 - cache writes must never break routing
            log.warning("router.paid_cache_write_error", error=str(exc)[:120])

    def _cache_paid_model(self, tier: Tier, model: str) -> None:
        if not model or model.startswith("newest:"):
            return
        norm = (model or "").strip()
        if not norm:
            return
        self._paid_fallback_cache[tier.value] = norm
        self._save_paid_fallback_cache()

    def _resolve_newest_model(self, tier: Tier, model: str) -> str:
        if not model.startswith("newest:"):
            return model

        family = model.split(":", 1)[1]
        live = newest_paid_model(family)
        if live:
            log.info("router.newest_resolved", family=family, model=live, tier=tier.value)
            self._cache_paid_model(tier, live)
            return live

        cached = self._paid_fallback_cache.get(tier.value)
        if cached:
            log.info("router.newest_cache_fallback", family=family, model=cached, tier=tier.value)
            return cached

        fallback = _fallback_free_model(tier)
        log.info("router.newest_offline_fallback", family=family, model=fallback, tier=tier.value)
        return fallback

    def _default_model(self, tier: Tier, profile: str = "balanced") -> str:
        if self.settings.free_only:
            return _FREE_DEFAULTS[tier]
        live = best_paid_model(
            tier,
            profile=profile,
            no_claude=bool(getattr(self.settings, "no_claude", False)),
        )
        if live:
            log.info(
                "router.best_live_resolved",
                profile=_normalized_profile(profile),
                model=live,
                tier=tier.value,
            )
            self._cache_paid_model(tier, live)
            return live
        return self._resolve_newest_model(tier, _PAID_DEFAULTS[tier])

    def _load_overrides(self) -> dict[str, str]:
        path = self.settings.data_dir / "model_tier_overrides.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception as exc:  # noqa: BLE001
                log.warning("router.overrides_unreadable", error=str(exc))
        return {}

    def resolve(
        self,
        tier: Tier,
        file_hint: str | None = None,
        task_type: str = "",
        profile: str = "balanced",
    ) -> str:
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

        # Runtime/env lock wins over persisted tuning, except free_only remains
        # the hard cost guard and rewrites paid pins below.
        runtime_pin = str(getattr(self.settings, f"model_{tier.value}", "") or "").strip()
        runtime_locked = bool(runtime_pin)
        if runtime_locked:
            model = runtime_pin
        # Persisted manual lock wins over defaults.
        elif tier.value in self._overrides:
            model = self._overrides[tier.value]
        else:
            model = self._default_model(tier, profile=profile)

        # ``newest:<family>`` auto-resolves to the freshest matching model from the
        # LIVE catalog (so a pin never ages into a stale model). Falls back to a
        # dynamic free-family substitute when live paid resolution is unavailable.
        if model.startswith("newest:"):
            model = self._resolve_newest_model(tier, model)

        if runtime_locked and not self.settings.free_only:
            return model

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
            model = (
                _FREE_DEFAULTS[tier]
                if self.settings.free_only
                else self._default_model(tier)
            )
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
                    d = self._default_model(t)
                    if d not in cands:
                        cands.append(d)
        except Exception as exc:  # noqa: BLE001 - fallback resolution must never raise
            log.warning("router.fallback_candidates_error", error=str(exc)[:120])
            if self.settings.free_only:
                cands = list(_FREE_DEFAULTS.values())
            else:
                cands = [self._default_model(t) for t in Tier]
        out: list[str] = []
        for m in cands:
            m2 = self._apply_policy(m, tier)  # honor no_claude / free_only
            if m2 and m2 != primary and m2 not in out:
                out.append(m2)
        return out

    def describe(self) -> dict[str, str]:
        return {t.value: self.resolve(t) for t in Tier}
