"""Tiered, cost-aware model routing.

Resolves a *stage tier* (and optional per-file hint) to a concrete model id.
Defaults to free OpenRouter models; honors the free-only / no-claude policy
and manual per-tier locks in ``data/model_tier_overrides.json``.

This is the routing *interface* + a deterministic default policy. The learned
router (2.0 backlog P2) plugs in by overriding :meth:`resolve`.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
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

_OPENROUTER_FREE_ROUTER = "openrouter/free"
_FREE_FAILOVER_LIMIT = 18


def is_free_model_id(model: str) -> bool:
    """Whether a model id is allowed under the free-only cost guard."""
    low = (model or "").strip().lower()
    return low.endswith(":free") or low == _OPENROUTER_FREE_ROUTER

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

# Concrete PAID offline backstop — the paid-mode twin of ``_FREE_DEFAULTS``. This is
# a LAST RESORT only: the primary path is the live OpenRouter catalog, which the
# router refreshes hourly (``best_paid_model`` / ``newest:<family>``) so normal
# operation always tracks the newest models as the catalog changes day to day. These
# hardcoded ids are reached ONLY when the live catalog is unreachable AND no per-tier
# paid model has been cached yet; they are current (2026) non-free, non-Claude models
# that approximate what the benchmark-scored online router would pick for each tier,
# so the offline path never drops to a stale generation or (the bug this fixes) a
# ``:free`` id for a paid user. ``fallback_candidates`` re-selects at runtime if one
# is ever retired. NOTE: the UI entry stays within the codestral/qwen/kimi/llama
# family because the offline UI fallback is family-guarded by
# tests/test_model_router_newest.py::test_resolve_newest_falls_back_to_paid_default_offline.
# When the live catalog is reachable this table is never consulted — keep it roughly
# current, but the router self-heals from the catalog regardless.
_PAID_OFFLINE_DEFAULTS: dict[Tier, str] = {
    Tier.CHEAP: "qwen/qwen3-coder",
    Tier.UI: "mistralai/codestral-2501",
    Tier.BACKEND: "moonshotai/kimi-k2.7-code",
    Tier.STRONG: "z-ai/glm-5.2",
    Tier.DOCS: "deepseek/deepseek-v4-pro",
}

_PAID_FALLBACK_CACHE = "model_router_paid_fallback.json"

# When a configured :free model is no longer in the live catalog, substitute a
# valid one preferring these markers per tier (best-for-purpose first).
_FREE_TIER_PREFS: dict[Tier, tuple[str, ...]] = {
    Tier.CHEAP: (
        "qwen3-coder", "north-mini-code", "laguna-xs-2.1", "laguna",
        "hy3", "nemotron-3-ultra", "nemotron-3-super", "qwen3-next",
        "gpt-oss-120b", "gemma-4", "llama-3.3", "deepseek", "qwen",
    ),
    Tier.UI: (
        "qwen3-coder", "north-mini-code", "laguna-xs-2.1", "laguna",
        "hy3", "nemotron-3-ultra", "gemma-4", "qwen3-next",
        "gpt-oss-120b", "llama-3.3", "deepseek", "qwen",
    ),
    Tier.BACKEND: (
        "qwen3-coder", "north-mini-code", "laguna-xs-2.1", "laguna",
        "hy3", "nemotron-3-ultra", "nemotron-3-super", "qwen3-next",
        "gpt-oss-120b", "deepseek", "llama-3.3", "qwen",
    ),
    Tier.DOCS: (
        "hy3", "qwen3-coder", "north-mini-code", "qwen3-next",
        "gpt-oss-120b", "gemma-4", "llama-3.3", "deepseek", "qwen",
    ),
    Tier.STRONG: (
        "hy3", "qwen3-next", "nemotron-3-ultra", "nemotron-3-super",
        "gpt-oss-120b", "llama-3.3-70b", "qwen3-coder",
        "north-mini-code", "laguna", "deepseek-r1", "qwen3", "deepseek", "qwen",
    ),
}

_FREE_FAILOVER_EXCLUDE = (
    "content-safety",
    "moderation",
    "guard",
    "embedding",
)

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
        for m in data:
            model_id = str(m.get("id") or "").strip() if isinstance(m, dict) else ""
            if not model_id or not is_free_model_id(model_id):
                continue
            if not _model_accepts_text(m) or not _model_supports_text(m):
                continue
            if any(marker in model_id.lower() for marker in _FREE_FAILOVER_EXCLUDE):
                continue
            if model_id not in fresh:
                fresh.append(model_id)
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


def prime_live_catalog(records: list[dict], fetched_at: float | None = None) -> None:
    """Share an already-fetched OpenRouter catalog with the core router.

    The web API and core router historically maintained independent caches and
    could issue two identical network requests during one routing preview. This
    accepts only normal catalog records, refuses to replace a newer snapshot,
    and primes both paid and free lookup paths without exposing mutable caller
    objects as the cache itself.
    """
    global _LIVE_CATALOG, _LIVE_CATALOG_AT, _LIVE_FREE_AT, _LIVE_FREE_IDS
    import time as _t

    try:
        stamp = float(fetched_at) if fetched_at is not None else _t.time()
    except (TypeError, ValueError):
        stamp = _t.time()
    if not math.isfinite(stamp) or stamp <= 0:
        stamp = _t.time()
    if _LIVE_CATALOG is not None and stamp < _LIVE_CATALOG_AT:
        return

    catalog: list[dict] = []
    free_ids: list[str] = []
    for raw in records:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        item = deepcopy(raw)
        item["created"] = int(item.get("created", 0) or 0)
        catalog.append(item)
        model_id = str(item.get("id") or "").strip()
        if (
            model_id
            and is_free_model_id(model_id)
            and _model_accepts_text(item)
            and _model_supports_text(item)
            and not any(marker in model_id.lower() for marker in _FREE_FAILOVER_EXCLUDE)
            and model_id not in free_ids
        ):
            free_ids.append(model_id)

    _LIVE_CATALOG = catalog
    _LIVE_CATALOG_AT = stamp
    _LIVE_FREE_IDS = free_ids
    _LIVE_FREE_AT = stamp


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

# ``cheap`` must be an enforceable price class, not merely a label on a soft
# ranking weight. OpenRouter catalog prices are USD/token, so these are $1/M
# input and $3/M output. They constrain only automatic model selection; an
# operator's explicit model pin remains authoritative and generation itself is
# never token/file/feature limited by this policy.
_CHEAP_MAX_INPUT_USD_PER_TOKEN = 1.0 / 1_000_000.0
_CHEAP_MAX_OUTPUT_USD_PER_TOKEN = 3.0 / 1_000_000.0

# Grok pricing varies by model, but the operator has explicitly classified the
# family as premium. Keep it out of automatic economy routes even when a single
# catalog entry currently falls below the numeric price ceiling. Explicit model
# pins remain authoritative.
_CHEAP_AUTO_EXCLUDED_PREFIXES = ("x-ai/grok",)

# Last-known cheap paid backstop for an offline catalog. Automatic cheap routing
# deliberately does not consume the generic paid cache: that cache may contain a
# previously selected balanced/best-quality model whose current price is unknown.
_CHEAP_PAID_OFFLINE_DEFAULTS: dict[Tier, str] = {
    Tier.CHEAP: "deepseek/deepseek-v4-flash",
    Tier.UI: "qwen/qwen3-coder-next",
    Tier.BACKEND: "z-ai/glm-5.2",
    Tier.STRONG: "z-ai/glm-5.2",
    Tier.DOCS: "deepseek/deepseek-v4-flash",
}


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


def _price_field(pricing: dict, primary: str, alternate: str) -> float | None:
    raw = pricing.get(primary)
    if raw is None:
        raw = pricing.get(alternate)
    return _parse_price(raw)


def _model_token_cost(model: dict) -> float | None:
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = _price_field(pricing, "prompt", "input")
    completion = _price_field(pricing, "completion", "output")
    if prompt is None and completion is None:
        request = _parse_price(pricing.get("request"))
        return request
    return float(prompt or 0.0) * 0.55 + float(completion or 0.0) * 0.45


def model_price_info(model: dict, model_id: str = "") -> dict[str, float | str | bool | None]:
    """Normalized input/output pricing and the automatic-cheap classification.

    OpenRouter exposes prices per token, while operators reason about dollars per
    million tokens. Keep both forms in one public helper so routing, telemetry,
    and API/UI presentation cannot disagree about whether a model is cheap.
    Request-only or incomplete paid pricing is ``unknown`` and is not eligible
    for automatic cheap routing; an explicit pin can still use it.
    """
    resolved_id = str(model_id or model.get("id") or "").strip()
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    prompt = _price_field(pricing, "prompt", "input")
    completion = _price_field(pricing, "completion", "output")
    request = _parse_price(pricing.get("request"))
    free = is_free_model_id(resolved_id) or (
        prompt == 0.0 and completion == 0.0 and request in (None, 0.0)
    )
    known = free or (prompt is not None and completion is not None)
    cheap = bool(
        free
        or (
            prompt is not None
            and completion is not None
            and prompt <= _CHEAP_MAX_INPUT_USD_PER_TOKEN
            and completion <= _CHEAP_MAX_OUTPUT_USD_PER_TOKEN
            and request in (None, 0.0)
        )
    )
    price_class = "free" if free else "cheap" if cheap else "expensive" if known else "unknown"
    return {
        "model": resolved_id,
        "price_class": price_class,
        "cheap_eligible": cheap,
        "prompt_usd_per_token": prompt,
        "completion_usd_per_token": completion,
        "request_usd": request,
        "prompt_usd_per_million": None if prompt is None else prompt * 1_000_000.0,
        "completion_usd_per_million": None if completion is None else completion * 1_000_000.0,
    }


def _requires_cheap_price(tier: Tier, profile: str) -> bool:
    """Whether this automatic route promises cheap per-token pricing."""
    # ``fast`` optimizes latency/throughput and may select a premium fast model;
    # it is not a price promise. Tier.CHEAP and Cheap+learned are price promises.
    return tier is Tier.CHEAP or _normalized_profile(profile) == "cheap_learned"


def _cheap_family_allowed(model_id: str) -> bool:
    low = str(model_id or "").strip().lower()
    return not any(low.startswith(prefix) for prefix in _CHEAP_AUTO_EXCLUDED_PREFIXES)


def _cheap_catalog_model(model: dict) -> bool:
    return bool(model_price_info(model).get("cheap_eligible"))


def _model_supports_text(model: dict) -> bool:
    arch = model.get("architecture")
    if not isinstance(arch, dict):
        return True
    outputs = arch.get("output_modalities")
    if not isinstance(outputs, list):
        return True
    return "text" in {str(o).lower() for o in outputs}


def _model_accepts_text(model: dict) -> bool:
    arch = model.get("architecture")
    if not isinstance(arch, dict):
        return True
    inputs = arch.get("input_modalities")
    if not isinstance(inputs, list):
        return True
    return "text" in {str(i).lower() for i in inputs}


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
            rank_n = int(rank) if rank is not None else 0
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


def _free_marker_quality(model_id: str, tier: Tier) -> float:
    low = model_id.lower()
    prefs = _FREE_TIER_PREFS.get(tier, ())
    for idx, marker in enumerate(prefs):
        if marker in low:
            return max(42.0, 112.0 - idx * 5.0)
    if low == _OPENROUTER_FREE_ROUTER:
        return 74.0
    if "coder" in low or "code" in low:
        return 62.0
    if "instruct" in low:
        return 50.0
    return 40.0


def _free_catalog_score(model: dict, tier: Tier, newest_created: int) -> float | None:
    model_id = str(model.get("id") or "").strip()
    if not model_id or not is_free_model_id(model_id):
        return None
    low = model_id.lower()
    if any(marker in low for marker in _FREE_FAILOVER_EXCLUDE):
        return None
    if not _model_accepts_text(model) or not _model_supports_text(model):
        return None

    created = int(model.get("created", 0) or 0)
    recency = 0.0 if not newest_created or not created else max(
        0.0,
        min(100.0, 100.0 - ((newest_created - created) / 86400.0) * 0.6),
    )
    context = int(model.get("context_length", 0) or 0)
    context_score = min(100.0, math.log10(max(context, 1)) * 18.0)
    quality = max(_arena_quality(model, tier), _index_quality(model, tier), _heuristic_quality(model_id, tier))
    return (
        _free_marker_quality(model_id, tier)
        + quality * 0.24
        + recency * 0.18
        + context_score * 0.08
    )


def ranked_live_free_model_ids(tier: Tier, limit: int = _FREE_FAILOVER_LIMIT) -> list[str]:
    """Current free OpenRouter models ranked for this tier.

    This intentionally uses the full live catalog, not only the handful of
    hardcoded defaults, so newly-released free models become fallback candidates
    without a code change. ``openrouter/free`` is kept in the pool as a free
    router after stronger named code models.
    """
    limit = max(0, int(limit or 0))
    if limit <= 0:
        return []
    catalog = live_catalog()
    newest_created = max((int(m.get("created", 0) or 0) for m in catalog), default=0)
    scored: list[tuple[float, int, int, str]] = []
    for m in catalog:
        if not isinstance(m, dict):
            continue
        score = _free_catalog_score(m, tier, newest_created)
        if score is None:
            continue
        scored.append((
            score,
            int(m.get("created", 0) or 0),
            int(m.get("context_length", 0) or 0),
            str(m.get("id") or "").strip(),
        ))
    scored.sort(reverse=True)
    out: list[str] = []
    for _, _, _, model_id in scored:
        if model_id and model_id not in out:
            out.append(model_id)
            if len(out) >= limit:
                return out

    # A populated full catalog is authoritative, including the absence of named
    # free models. Only hit the legacy free-list endpoint when the full catalog
    # itself is unavailable; otherwise this would duplicate the same fetch.
    if not catalog:
        for model_id in live_free_model_ids():
            if model_id not in out:
                out.append(model_id)
                if len(out) >= limit:
                    break
    return out


def _catalog_model_score(model: dict, tier: Tier, profile: str, newest_created: int) -> float | None:
    model_id = str(model.get("id") or "").strip()
    if not model_id or is_free_model_id(model_id):
        return None
    low = model_id.lower()
    if model_id.startswith("~") or any(x in low for x in _AUTO_ROUTE_EXCLUDE):
        return None
    if not _model_supports_text(model):
        return None
    # A soft cost penalty can always be overwhelmed by a newly released model's
    # benchmark/recency score. Cheap routes instead have a hard price-class
    # boundary based on both normalized input and output rates.
    if _requires_cheap_price(tier, profile):
        if not _cheap_family_allowed(model_id) or not _cheap_catalog_model(model):
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


def ranked_paid_models(
    tier: Tier,
    profile: str = "balanced",
    *,
    no_claude: bool = False,
    limit: int = _FREE_FAILOVER_LIMIT,
) -> list[str]:
    """Current paid catalog models ranked for a tier/profile."""
    limit = max(0, int(limit or 0))
    if limit <= 0:
        return []
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
    scored.sort(reverse=True)
    return [model_id for _score, _created, model_id in scored[:limit]]


def best_paid_model(tier: Tier, profile: str = "balanced", *, no_claude: bool = False) -> str | None:
    """Best current paid model for ``tier`` from OpenRouter's live catalog.

    This is benchmark-aware, not family-name-only. It prefers current models that
    score well for the tier's job, while profile weights tune cost/quality bias.
    Returns ``None`` when the live catalog is unavailable or too sparse; callers
    keep the older newest-family fallback in that case.
    """
    ranked = ranked_paid_models(tier, profile=profile, no_claude=no_claude, limit=1)
    return ranked[0] if ranked else None


def newest_paid_model(family: str) -> str | None:
    """The NEWEST live, non-free, non-experimental catalog id matching ``family``.

    ``family="coder"`` -> newest across ALL strong coder families; otherwise a
    substring match (e.g. ``"deepseek-v3"`` -> newest deepseek-v3.x). ``None`` when
    the catalog is unreachable or nothing matches — caller keeps a static fallback."""
    fam = family.lower()
    families = _CODER_FAMILIES if fam == "coder" else (fam,)
    cands = [(m["id"], m["created"]) for m in live_catalog()
             if any(f in m["id"].lower() for f in families) and not is_free_model_id(m["id"])
             and (m["id"] in _NEWEST_ALLOW
                  or not any(x in m["id"].lower() for x in _NEWEST_EXCLUDE))]
    return max(cands, key=lambda c: c[1])[0] if cands else None


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
            path.write_text(
                json.dumps(self._paid_fallback_cache, indent=2),
                encoding="utf-8",
            )
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

        # Offline last resort: a CONCRETE paid default, never a :free id. This branch
        # is only reachable in PAID mode (free_only rewrites happen later, in
        # _apply_policy), so dropping to :free here would silently downgrade a paying
        # user — the regression guarded by test_no_claude_paid_mode_uses_paid_default.
        fallback = _PAID_OFFLINE_DEFAULTS[tier]
        log.info("router.newest_offline_fallback", family=family, model=fallback, tier=tier.value)
        return fallback

    def _default_model(self, tier: Tier, profile: str = "balanced") -> str:
        if self.settings.free_only:
            return _FREE_DEFAULTS[tier]
        catalog_available = bool(live_catalog())
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
        if _requires_cheap_price(tier, profile):
            # A populated catalog that contains no eligible paid model is proof
            # that stale offline assumptions must not be used. Route through the
            # live free router instead. Last-known paid defaults are only an
            # offline availability backstop when there is no catalog evidence.
            fallback = (
                _OPENROUTER_FREE_ROUTER
                if catalog_available
                else _CHEAP_PAID_OFFLINE_DEFAULTS[tier]
            )
            log.info(
                (
                    "router.cheap_no_eligible_paid"
                    if catalog_available
                    else "router.cheap_offline_fallback"
                ),
                profile=_normalized_profile(profile),
                model=fallback,
                tier=tier.value,
            )
            return fallback
        return self._resolve_newest_model(tier, _PAID_DEFAULTS[tier])

    @staticmethod
    def _tier_for_file(tier: Tier, file_hint: str | None = None) -> Tier:
        if file_hint:
            ext = Path(file_hint).suffix.lower()
            if ext in {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"}:
                return Tier.UI
            if ext in {".py", ".go", ".rs", ".java", ".rb", ".sql"}:
                return Tier.BACKEND
        return tier

    def _has_explicit_tier_pin(self, tier: Tier) -> bool:
        runtime_pin = str(getattr(self.settings, f"model_{tier.value}", "") or "").strip()
        return bool(runtime_pin or self._overrides.get(tier.value))

    def model_cost_info(self, model_id: str) -> dict[str, float | str | bool | None]:
        """Live normalized pricing/classification for one model id."""
        model_id = str(model_id or "").strip()
        if is_free_model_id(model_id):
            return model_price_info({"id": model_id, "pricing": {"prompt": 0, "completion": 0}})
        record = next(
            (m for m in live_catalog() if str(m.get("id") or "").strip() == model_id),
            None,
        )
        return model_price_info(record or {}, model_id=model_id)

    def auto_model_allowed(self, model_id: str, tier: Tier, profile: str = "balanced") -> bool:
        """Whether an automatically learned/cached model may serve this route."""
        if not _requires_cheap_price(tier, profile):
            return True
        if not _cheap_family_allowed(model_id):
            return False
        info = self.model_cost_info(model_id)
        if bool(info.get("cheap_eligible")):
            return True
        # The live catalog is unavailable, not evidence that these last-known
        # cheap backstops became expensive. Keep offline failover functional while
        # refusing arbitrary unpriced tournament/cache entries.
        return (
            info.get("price_class") == "unknown"
            and model_id in set(_CHEAP_PAID_OFFLINE_DEFAULTS.values())
            and not live_catalog()
        )

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
        tier = self._tier_for_file(tier, file_hint)

        # Runtime/env lock wins over persisted tuning, except free_only remains
        # the hard cost guard and rewrites paid pins below.
        runtime_pin = str(getattr(self.settings, f"model_{tier.value}", "") or "").strip()
        runtime_locked = bool(runtime_pin)
        explicit_source = "runtime" if runtime_locked else ""
        if runtime_locked:
            model = runtime_pin
        # Persisted manual lock wins over defaults.
        elif tier.value in self._overrides:
            model = self._overrides[tier.value]
            explicit_source = "persisted"
        else:
            model = self._default_model(tier, profile=profile)

        # ``newest:<family>`` auto-resolves to the freshest matching model from the
        # LIVE catalog (so a pin never ages into a stale model). Falls back to a
        # dynamic free-family substitute when live paid resolution is unavailable.
        if model.startswith("newest:"):
            model = self._resolve_newest_model(tier, model)

        if explicit_source:
            try:
                info = self.model_cost_info(model)
                log.info(
                    "router.explicit_model_pin",
                    source=explicit_source,
                    tier=tier.value,
                    profile=_normalized_profile(profile),
                    model=model,
                    price_class=info.get("price_class"),
                    input_per_million=info.get("prompt_usd_per_million"),
                    output_per_million=info.get("completion_usd_per_million"),
                )
            except Exception as exc:  # noqa: BLE001 - classification never blocks a pin
                log.debug(
                    "router.explicit_model_pin_unclassified",
                    model=model,
                    error=str(exc)[:120],
                )

        if runtime_locked and not self.settings.free_only:
            return model

        model = self._apply_policy(model, tier, profile=profile)
        # Self-heal a retired :free id against OpenRouter's LIVE catalog (only
        # worth a fetch when an OpenRouter key is configured — otherwise the
        # backend is claude/stub and the model id is unused).
        if (self.settings.free_only and is_free_model_id(model)
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

    def _apply_policy(self, model: str, tier: Tier, profile: str = "balanced") -> str:
        low = model.lower()
        # no-claude: rewrite Claude picks to a non-Claude tier default — staying
        # in the user's PAID catalog when they're paid (free_only=False). Forcing
        # them onto :free would be a silent downgrade; no_claude means "avoid
        # Claude", not "drop to free".
        if self.settings.no_claude and any(m in low for m in _CLAUDE_MARKERS):
            model = (
                _FREE_DEFAULTS[tier]
                if self.settings.free_only
                else self._default_model(tier, profile=profile)
            )
            log.info("router.rewrote_claude", tier=tier.value, to=model)
        # free-only: force a :free model.
        if self.settings.free_only and not is_free_model_id(model):
            model = _FREE_DEFAULTS[tier]
        return model

    def fallback_candidates(
        self,
        tier: Tier,
        primary: str | None = None,
        profile: str = "balanced",
    ) -> list[str]:
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
                live = ranked_live_free_model_ids(tier)
                for m in live:
                    if m not in cands:
                        cands.append(m)
                for t in Tier:  # static offline backstop across every tier
                    d = _FREE_DEFAULTS[t]
                    if d not in cands:
                        cands.append(d)
            elif _requires_cheap_price(tier, profile):
                catalog_available = bool(live_catalog())
                for m in ranked_paid_models(
                    tier,
                    profile=profile,
                    no_claude=bool(getattr(self.settings, "no_claude", False)),
                ):
                    if m not in cands:
                        cands.append(m)
                if catalog_available:
                    for m in ranked_live_free_model_ids(tier):
                        if m not in cands:
                            cands.append(m)
                    if _OPENROUTER_FREE_ROUTER not in cands:
                        cands.append(_OPENROUTER_FREE_ROUTER)
                else:
                    for t in Tier:
                        d = _CHEAP_PAID_OFFLINE_DEFAULTS[t]
                        if d not in cands:
                            cands.append(d)
            else:
                for m in ranked_paid_models(
                    tier,
                    profile=profile,
                    no_claude=bool(getattr(self.settings, "no_claude", False)),
                ):
                    if m not in cands:
                        cands.append(m)
                for fam in _CODER_FAMILIES:  # newest live model per strong-coder family
                    live_model = newest_paid_model(fam)
                    if live_model and live_model not in cands:
                        cands.append(live_model)
                for t in Tier:
                    d = self._default_model(t, profile=profile)
                    if d not in cands:
                        cands.append(d)
        except Exception as exc:  # noqa: BLE001 - fallback resolution must never raise
            log.warning("router.fallback_candidates_error", error=str(exc)[:120])
            if self.settings.free_only:
                cands = list(_FREE_DEFAULTS.values())
            else:
                cands = [self._default_model(t, profile=profile) for t in Tier]
        out: list[str] = []
        for m in cands:
            m2 = self._apply_policy(m, tier, profile=profile)  # honor policy + profile
            if (
                m2
                and m2 != primary
                and m2 not in out
                and self.auto_model_allowed(m2, tier, profile)
            ):
                out.append(m2)
        return out

    def describe(self) -> dict[str, str]:
        return {t.value: self.resolve(t) for t in Tier}
