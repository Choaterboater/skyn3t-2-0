"""Price and benchmark comparisons for OpenRouter model-selection UIs.

OpenRouter pricing is per token, so a rate alone is hard to compare.  This
module applies one documented example workload to every model and looks for a
lower-cost peer using the task-relevant benchmark metadata in the live catalog.
The example is informational; it never changes routing or limits usage.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from collections.abc import Iterable
from copy import deepcopy
from typing import Any, TypedDict

EXAMPLE_WORKLOAD: dict[str, Any] = {
    "id": "app_build_turn_20k_8k",
    "label": "example app-build turn",
    "prompt_tokens": 20_000,
    "completion_tokens": 8_000,
    "currency": "USD",
    "is_limit": False,
    "note": (
        "Example cost for 20,000 input tokens and 8,000 output tokens. "
        "A build can make many calls, so its actual total may be higher; this is not a cap."
    ),
}

_AA_WEIGHTS: dict[str, dict[str, float]] = {
    "app_building": {"coding_index": 0.5, "agentic_index": 0.3, "intelligence_index": 0.2},
    "cheap": {"coding_index": 0.5, "agentic_index": 0.3, "intelligence_index": 0.2},
    "ui": {"coding_index": 0.45, "agentic_index": 0.35, "intelligence_index": 0.2},
    "backend": {"coding_index": 0.5, "agentic_index": 0.35, "intelligence_index": 0.15},
    "strong": {"coding_index": 0.4, "agentic_index": 0.3, "intelligence_index": 0.3},
    "docs": {"coding_index": 0.2, "agentic_index": 0.2, "intelligence_index": 0.6},
}

_DESIGN_CATEGORIES: dict[str, frozenset[str]] = {
    "app_building": frozenset(
        {"fullstack", "webapps", "codecategories", "website", "uicomponent", "gamedev"}
    ),
    "cheap": frozenset({"fullstack", "webapps", "codecategories", "website"}),
    "ui": frozenset({"webapps", "website", "uicomponent", "dataviz", "svg", "3d"}),
    "backend": frozenset({"fullstack", "webapps", "codecategories"}),
    "strong": frozenset(
        {"fullstack", "webapps", "codecategories", "website", "uicomponent", "gamedev"}
    ),
    "docs": frozenset({"agentichtmlslides", "agenticslides(html)", "htmlslides"}),
}


class _Observation(TypedDict):
    id: str
    name: str
    record: dict[str, Any]
    cost: float | None
    benchmark: dict[str, Any] | None


class _SharedBenchmark(TypedDict):
    profile: str
    dimensions: list[str]
    selected_score: float
    alternative_score: float
    delta: float


_ANNOTATION_CACHE_MAX = 12
_ANNOTATION_CACHE: OrderedDict[
    tuple[str, str], dict[str, dict[str, Any]]
] = OrderedDict()
_ANNOTATION_CACHE_LOCK = threading.RLock()


def workload_payload() -> dict[str, Any]:
    """Return a copy so an API caller cannot mutate the shared definition."""

    return dict(EXAMPLE_WORKLOAD)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _pricing(record: dict[str, Any]) -> dict[str, float]:
    raw = record.get("pricing")
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, float] = {}
    for key in ("prompt", "input", "completion", "output", "request"):
        value = _number(raw.get(key))
        if value is not None:
            normalized[key] = value
    return normalized


def estimate_example_cost(record: dict[str, Any]) -> float | None:
    """Estimate the shared example workload using OpenRouter's per-token rates."""

    pricing = _pricing(record)
    prompt_rate = pricing.get("prompt", pricing.get("input"))
    completion_rate = pricing.get("completion", pricing.get("output"))
    if prompt_rate is None and completion_rate is None:
        request_rate = pricing.get("request")
        return round(request_rate, 10) if request_rate is not None else None
    if prompt_rate is None or completion_rate is None:
        # A missing side is unknown, not free. Treating it as zero manufactures
        # an understated price comparison.
        return None
    return round(
        EXAMPLE_WORKLOAD["prompt_tokens"] * prompt_rate
        + EXAMPLE_WORKLOAD["completion_tokens"] * completion_rate
        + float(pricing.get("request") or 0.0),
        10,
    )


def _weighted_average(values: dict[str, float], weights: dict[str, float]) -> float | None:
    present = [(values[key], weight) for key, weight in weights.items() if key in values]
    total_weight = sum(weight for _, weight in present)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in present) / total_weight


def benchmark_summary(record: dict[str, Any], profile: str = "app_building") -> dict[str, Any] | None:
    """Build a transparent 0-100 score from OpenRouter's app-relevant benchmarks."""

    normalized_profile = profile if profile in _AA_WEIGHTS else "app_building"
    raw = record.get("benchmarks")
    if not isinstance(raw, dict):
        return None

    components: dict[str, float] = {}
    aa_raw = raw.get("artificial_analysis")
    if isinstance(aa_raw, dict):
        aa_values: dict[str, float] = {}
        for key in ("coding_index", "agentic_index", "intelligence_index"):
            value = _number(aa_raw.get(key))
            if value is not None:
                aa_values[key] = min(100.0, value)
                components[f"artificial_analysis.{key}"] = round(min(100.0, value), 2)
        aa_score = _weighted_average(aa_values, _AA_WEIGHTS[normalized_profile])
    else:
        aa_score = None

    categories = _DESIGN_CATEGORIES[normalized_profile]
    design_values: list[float] = []
    design_raw = raw.get("design_arena")
    if isinstance(design_raw, list):
        for row in design_raw:
            if not isinstance(row, dict):
                continue
            category = str(row.get("category") or "").strip().lower()
            if category not in categories:
                continue
            value = _number(row.get("win_rate"))
            metric = "win_rate"
            if value is None:
                elo = _number(row.get("elo"))
                value = None if elo is None else max(0.0, min(100.0, (elo - 1000.0) / 5.0))
                metric = "elo_normalized"
            if value is not None:
                value = min(100.0, value)
                design_values.append(value)
                arena = str(row.get("arena") or "unspecified").strip().lower()
                components[f"design_arena.{arena}.{category}.{metric}"] = round(value, 2)
    design_score = sum(design_values) / len(design_values) if design_values else None

    if aa_score is not None and design_score is not None:
        score = aa_score * 0.65 + design_score * 0.35
    elif aa_score is not None:
        score = aa_score
    elif design_score is not None:
        score = design_score
    else:
        return None

    return {
        "profile": normalized_profile,
        "score": round(score, 2),
        "scale": "0-100",
        "source": "OpenRouter catalog benchmarks",
        "components": components,
    }


def _shared_benchmark_comparison(
    selected: dict[str, Any] | None,
    alternative: dict[str, Any] | None,
    profile: str,
) -> _SharedBenchmark | None:
    """Compare only dimensions observed for both models.

    Two aggregate catalog scores are not comparable when one is based on coding
    indexes and the other only on a design-arena category. Recompute each score
    over their shared evidence so a benchmark delta always means like-for-like.
    """
    if selected is None or alternative is None:
        return None
    selected_components = selected.get("components")
    alternative_components = alternative.get("components")
    if not isinstance(selected_components, dict) or not isinstance(alternative_components, dict):
        return None
    dimensions = sorted(set(selected_components) & set(alternative_components))
    if not dimensions:
        return None

    normalized_profile = profile if profile in _AA_WEIGHTS else "app_building"
    aa_dimensions = [key for key in dimensions if key.startswith("artificial_analysis.")]
    design_dimensions = [key for key in dimensions if key.startswith("design_arena.")]

    def family_score(components: dict[str, Any], keys: list[str], *, aa: bool) -> float | None:
        weighted = 0.0
        total = 0.0
        for key in keys:
            value = _number(components.get(key))
            if value is None:
                continue
            suffix = key.split(".", 1)[1]
            weight = _AA_WEIGHTS[normalized_profile].get(suffix, 1.0) if aa else 1.0
            weighted += value * weight
            total += weight
        return weighted / total if total else None

    selected_aa = family_score(selected_components, aa_dimensions, aa=True)
    alternative_aa = family_score(alternative_components, aa_dimensions, aa=True)
    selected_design = family_score(selected_components, design_dimensions, aa=False)
    alternative_design = family_score(alternative_components, design_dimensions, aa=False)

    if selected_aa is not None and alternative_aa is not None:
        if selected_design is not None and alternative_design is not None:
            selected_score = selected_aa * 0.65 + selected_design * 0.35
            alternative_score = alternative_aa * 0.65 + alternative_design * 0.35
        else:
            selected_score, alternative_score = selected_aa, alternative_aa
    elif selected_design is not None and alternative_design is not None:
        selected_score, alternative_score = selected_design, alternative_design
    else:
        return None

    delta = alternative_score - selected_score
    return _SharedBenchmark(
        profile=normalized_profile,
        dimensions=dimensions,
        selected_score=round(selected_score, 2),
        alternative_score=round(alternative_score, 2),
        delta=round(delta, 2),
    )


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("id") or "").strip()


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip().lower() for item in value if str(item).strip()}


def _architecture_modalities(record: dict[str, Any], key: str) -> set[str]:
    architecture = record.get("architecture")
    if not isinstance(architecture, dict):
        return set()
    return _string_set(architecture.get(key))


def _context_length(record: dict[str, Any]) -> int | None:
    value = _number(record.get("context_length"))
    return int(value) if value is not None else None


def _capability_compatible(selected: dict[str, Any], candidate: dict[str, Any]) -> bool:
    selected_inputs = _architecture_modalities(selected, "input_modalities")
    candidate_inputs = _architecture_modalities(candidate, "input_modalities")
    if selected_inputs and not selected_inputs.issubset(candidate_inputs):
        return False

    selected_outputs = _architecture_modalities(selected, "output_modalities")
    candidate_outputs = _architecture_modalities(candidate, "output_modalities")
    if selected_outputs and not selected_outputs.issubset(candidate_outputs):
        return False

    selected_params = _string_set(selected.get("supported_parameters"))
    candidate_params = _string_set(candidate.get("supported_parameters"))
    for capability in ("tools", "structured_outputs", "reasoning"):
        if capability in selected_params and capability not in candidate_params:
            return False

    selected_context = _context_length(selected)
    candidate_context = _context_length(candidate)
    if selected_context is not None:
        minimum_context = min(200_000, max(32_000, int(selected_context * 0.4)))
        if candidate_context is None or candidate_context < minimum_context:
            return False
    return True


def _price_band(cost: float | None, known_positive_costs: list[float]) -> str:
    if cost is None:
        return "unknown"
    if cost <= 0:
        return "free"
    if not known_positive_costs:
        return "priced"
    position = sum(1 for value in known_positive_costs if value <= cost) / len(known_positive_costs)
    if position >= 0.75:
        return "high"
    if position <= 0.25:
        return "low"
    return "typical"


def _observations(
    records: Iterable[dict[str, Any]],
    profile: str,
) -> list[_Observation]:
    return [
        _Observation(
            id=_record_id(record),
            name=str(record.get("name") or _record_id(record)),
            record=record,
            cost=estimate_example_cost(record),
            benchmark=benchmark_summary(record, profile),
        )
        for record in records
        if isinstance(record, dict) and _record_id(record)
    ]


def _annotation(
    selected: _Observation,
    observations: list[_Observation],
    profile: str,
    positive_costs: list[float],
) -> dict[str, Any]:
    cost = selected["cost"]
    return {
        "example_cost_usd": cost,
        "price_example": {
            "workload_id": EXAMPLE_WORKLOAD["id"],
            "usd": cost,
            "known": cost is not None,
        },
        "relative_price_band": _price_band(cost, positive_costs),
        "benchmark": selected["benchmark"],
        "value_alternative": _choose_alternative(selected["record"], observations, profile),
    }


def _choose_alternative(
    selected: dict[str, Any],
    observations: list[_Observation],
    profile: str,
) -> dict[str, Any] | None:
    selected_id = _record_id(selected)
    selected_cost = estimate_example_cost(selected)
    if not selected_id or selected_cost is None or selected_cost <= 0:
        return None

    selected_benchmark = benchmark_summary(selected, profile)
    benchmark_candidates: list[tuple[_Observation, _SharedBenchmark]] = []
    capability_candidates: list[_Observation] = []
    for observation in observations:
        candidate = observation["record"]
        candidate_id = observation["id"]
        candidate_cost = observation["cost"]
        if candidate_id == selected_id or candidate_cost is None:
            continue
        # Require a meaningful saving, not a rounding-level difference.
        if candidate_cost > selected_cost * 0.8:
            continue
        if not _capability_compatible(selected, candidate):
            continue
        shared = _shared_benchmark_comparison(
            selected_benchmark,
            observation["benchmark"],
            profile,
        )
        if shared is not None:
            tolerance = max(6.0, shared["selected_score"] * 0.10)
            if shared["alternative_score"] >= shared["selected_score"] - tolerance:
                benchmark_candidates.append((observation, shared))
            # Shared evidence says the alternative is materially weaker: do not
            # relabel it capability-only to evade that evidence.
            continue
        capability_candidates.append(observation)

    if not benchmark_candidates and not capability_candidates:
        return None

    shared_comparison: _SharedBenchmark | None = None
    if benchmark_candidates:
        def ranked(
            item: tuple[_Observation, _SharedBenchmark],
        ) -> tuple[float, float, float, str]:
            observation, shared = item
            score = shared["alternative_score"]
            raw_cost = observation["cost"]
            assert raw_cost is not None
            cost = float(raw_cost)
            value = score / max(cost, 0.000001)
            gap = abs(shared["selected_score"] - score)
            return (-value, gap, cost, str(observation["id"]))

        alternative, shared_comparison = min(benchmark_candidates, key=ranked)
        basis = "task_benchmark"
    else:
        # No shared benchmark dimensions means there is no defensible
        # performance comparison. Pick a capable cheaper option, but label it as
        # capability-only even when either model has unrelated benchmark data.
        def capability_ranked(observation: _Observation) -> tuple[float, str]:
            raw_cost = observation["cost"]
            assert raw_cost is not None
            return (float(raw_cost), str(observation["id"]))

        alternative = min(capability_candidates, key=capability_ranked)
        basis = "capability_match"

    raw_alternative_cost = alternative["cost"]
    assert raw_alternative_cost is not None
    alternative_cost = float(raw_alternative_cost)
    savings = max(0.0, selected_cost - alternative_cost)
    savings_percent = (savings / selected_cost) * 100.0
    alternative_benchmark = alternative["benchmark"]
    if shared_comparison is not None:
        benchmark_delta: float | None = shared_comparison["delta"]
        shared_delta = shared_comparison["delta"]
        comparison = (
            f"benchmark {abs(shared_delta):.1f} points "
            f"{'higher' if shared_delta >= 0 else 'lower'} across "
            f"{len(shared_comparison['dimensions'])} shared dimension(s)"
        )
    else:
        benchmark_delta = None
        comparison = "capability-only match; performance not compared (no shared benchmark dimensions)"

    return {
        "id": alternative["id"],
        "name": alternative["name"],
        "example_cost_usd": round(alternative_cost, 10),
        "savings_usd": round(savings, 10),
        "savings_percent": round(savings_percent, 1),
        "benchmark": alternative_benchmark,
        "benchmark_delta": benchmark_delta,
        "comparison_basis": basis,
        "shared_benchmark": shared_comparison,
        "performance_label": (
            "shared benchmark comparison"
            if shared_comparison is not None
            else "capability-only; performance not compared"
        ),
        "reason": (
            f"{savings_percent:.0f}% cheaper for the example workload; {comparison}. "
            "The original model remains selectable."
        ),
    }


def _compute_annotations(
    records: list[dict[str, Any]],
    profile: str,
) -> dict[str, dict[str, Any]]:
    observations = _observations(records, profile)
    positive_costs = sorted(
        float(observation["cost"])
        for observation in observations
        if observation["cost"] is not None and float(observation["cost"]) > 0
    )

    annotations: dict[str, dict[str, Any]] = {}
    for observation in observations:
        annotations[observation["id"]] = _annotation(
            observation,
            observations,
            profile,
            positive_costs,
        )
    return annotations


def _catalog_fingerprint(records: list[dict[str, Any]]) -> str:
    """Deterministic digest of only fields that affect value annotations."""
    projected: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not _record_id(record):
            continue
        architecture = record.get("architecture")
        architecture = architecture if isinstance(architecture, dict) else {}
        benchmarks = record.get("benchmarks")
        benchmarks = benchmarks if isinstance(benchmarks, dict) else {}
        design_arena = benchmarks.get("design_arena")
        design_rows = [row for row in design_arena if isinstance(row, dict)] if isinstance(
            design_arena, list
        ) else []
        design_rows.sort(
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
        )
        projected.append(
            {
                "id": record.get("id"),
                "name": record.get("name"),
                "pricing": record.get("pricing"),
                "context_length": record.get("context_length"),
                "input_modalities": sorted(
                    _string_set(architecture.get("input_modalities"))
                ),
                "output_modalities": sorted(
                    _string_set(architecture.get("output_modalities"))
                ),
                "supported_parameters": sorted(_string_set(record.get("supported_parameters"))),
                "artificial_analysis": benchmarks.get("artificial_analysis"),
                "design_arena": design_rows,
            }
        )
    projected.sort(
        key=lambda item: (
            str(item.get("id") or ""),
            json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
        )
    )
    payload = json.dumps(
        projected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def clear_value_annotation_cache() -> None:
    """Clear process-local annotations (primarily for tests/forced refreshes)."""
    with _ANNOTATION_CACHE_LOCK:
        _ANNOTATION_CACHE.clear()


def _cached_annotations(
    records: list[dict[str, Any]],
    profile: str,
) -> dict[str, dict[str, Any]]:
    normalized_profile = profile if profile in _AA_WEIGHTS else "app_building"
    key = (_catalog_fingerprint(records), normalized_profile)
    with _ANNOTATION_CACHE_LOCK:
        cached = _ANNOTATION_CACHE.get(key)
        if cached is not None:
            _ANNOTATION_CACHE.move_to_end(key)
            return cached
        annotations = _compute_annotations(records, normalized_profile)
        _ANNOTATION_CACHE[key] = annotations
        _ANNOTATION_CACHE.move_to_end(key)
        while len(_ANNOTATION_CACHE) > _ANNOTATION_CACHE_MAX:
            _ANNOTATION_CACHE.popitem(last=False)
        return annotations


def catalog_value_annotations(
    records: Iterable[dict[str, Any]],
    profile: str = "app_building",
) -> dict[str, dict[str, Any]]:
    """Return cached price/benchmark/value annotations keyed by model id."""
    materialized = [record for record in records if isinstance(record, dict)]
    return deepcopy(_cached_annotations(materialized, profile))


def model_value_annotation(
    records: Iterable[dict[str, Any]],
    model_id: str,
    profile: str = "app_building",
) -> dict[str, Any]:
    """Return one selected model's annotation without exposing internal records."""

    materialized = [record for record in records if isinstance(record, dict)]
    selected = _cached_annotations(materialized, profile).get(model_id)
    if selected is not None:
        return deepcopy(selected)
    return {
        "example_cost_usd": None,
        "price_example": {
            "workload_id": EXAMPLE_WORKLOAD["id"],
            "usd": None,
            "known": False,
        },
        "relative_price_band": "unknown",
        "benchmark": None,
        "value_alternative": None,
    }
