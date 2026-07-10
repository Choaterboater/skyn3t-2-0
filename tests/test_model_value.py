from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import skyn3t.web.model_value as model_value
from skyn3t.web.model_value import (
    EXAMPLE_WORKLOAD,
    benchmark_summary,
    catalog_value_annotations,
    estimate_example_cost,
)


def _model(
    model_id: str,
    *,
    prompt: float,
    completion: float,
    coding: float | None,
    agentic: float | None = None,
    intelligence: float | None = None,
    context_length: int = 500_000,
) -> dict:
    benchmarks = None
    if coding is not None:
        benchmarks = {
            "artificial_analysis": {
                "coding_index": coding,
                "agentic_index": agentic if agentic is not None else coding,
                "intelligence_index": intelligence if intelligence is not None else coding,
            },
            "design_arena": [
                {
                    "category": "webapps",
                    "win_rate": coding,
                    "elo": 1300,
                }
            ],
        }
    record = {
        "id": model_id,
        "name": model_id,
        "pricing": {"prompt": prompt, "completion": completion},
        "context_length": context_length,
        "architecture": {
            "input_modalities": ["text", "image", "file"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["tools", "structured_outputs", "reasoning"],
    }
    if benchmarks is not None:
        record["benchmarks"] = benchmarks
    return record


def test_example_workload_is_an_estimate_not_a_spend_limit():
    assert EXAMPLE_WORKLOAD["prompt_tokens"] == 20_000
    assert EXAMPLE_WORKLOAD["completion_tokens"] == 8_000
    assert EXAMPLE_WORKLOAD["is_limit"] is False
    assert "not a cap" in EXAMPLE_WORKLOAD["note"]


def test_example_cost_requires_both_token_rates_and_adds_request_fee() -> None:
    prompt_only = {"pricing": {"prompt": 0.000001}}
    completion_only = {"pricing": {"completion": 0.000002}}
    request_only = {"pricing": {"request": 0.0125}}
    token_and_request = {
        "pricing": {
            "prompt": 0.000001,
            "completion": 0.000002,
            "request": 0.01,
        }
    }

    assert estimate_example_cost(prompt_only) is None
    assert estimate_example_cost(completion_only) is None
    assert estimate_example_cost(request_only) == 0.0125
    assert estimate_example_cost(token_and_request) == 0.046


def test_fable_surfaces_a_task_near_cheaper_value_peer():
    fable = _model(
        "anthropic/claude-fable-5",
        prompt=0.000010,
        completion=0.000050,
        coding=76.5,
        agentic=52.8,
        intelligence=59.9,
        context_length=1_000_000,
    )
    grok = _model(
        "x-ai/grok-4.5",
        prompt=0.000002,
        completion=0.000006,
        coding=72.4,
        agentic=45.7,
        intelligence=53.8,
    )
    weak = _model(
        "vendor/tiny",
        prompt=0.0,
        completion=0.0,
        coding=25.0,
    )

    annotations = catalog_value_annotations([fable, grok, weak])
    value = annotations[fable["id"]]

    assert estimate_example_cost(fable) == 0.6
    assert value["example_cost_usd"] == 0.6
    assert value["relative_price_band"] == "high"
    assert value["benchmark"]["source"] == "OpenRouter catalog benchmarks"
    assert value["value_alternative"]["id"] == "x-ai/grok-4.5"
    assert value["value_alternative"]["example_cost_usd"] == 0.088
    assert value["value_alternative"]["savings_percent"] == 85.3
    assert value["value_alternative"]["comparison_basis"] == "task_benchmark"
    assert "original model remains selectable" in value["value_alternative"]["reason"].lower()


def test_benchmark_profile_is_task_relevant_and_transparent():
    record = _model(
        "vendor/model",
        prompt=0.000001,
        completion=0.000002,
        coding=80.0,
        agentic=60.0,
        intelligence=40.0,
    )

    backend = benchmark_summary(record, "backend")
    docs = benchmark_summary(record, "docs")

    assert backend is not None and docs is not None
    assert backend["profile"] == "backend"
    assert docs["profile"] == "docs"
    assert backend["score"] > docs["score"]
    assert "artificial_analysis.coding_index" in backend["components"]


def test_missing_selected_benchmark_never_claims_similar_performance():
    selected = _model(
        "vendor/unbenchmarked",
        prompt=0.000010,
        completion=0.000030,
        coding=None,
    )
    peer = _model(
        "vendor/benchmarked-peer",
        prompt=0.000001,
        completion=0.000003,
        coding=70.0,
    )

    alternative = catalog_value_annotations([selected, peer])[selected["id"]][
        "value_alternative"
    ]

    assert alternative["comparison_basis"] == "capability_match"
    assert alternative["benchmark_delta"] is None
    assert alternative["performance_label"] == "capability-only; performance not compared"
    assert "no shared benchmark dimensions" in alternative["reason"]


def test_disjoint_benchmarks_downgrade_to_capability_only() -> None:
    selected = _model(
        "vendor/index-only",
        prompt=0.000010,
        completion=0.000030,
        coding=None,
    )
    selected["benchmarks"] = {
        "artificial_analysis": {"coding_index": 80.0},
    }
    peer = _model(
        "vendor/arena-only",
        prompt=0.000001,
        completion=0.000003,
        coding=None,
    )
    peer["benchmarks"] = {
        "design_arena": [{"category": "webapps", "win_rate": 79.0}],
    }

    alternative = catalog_value_annotations([selected, peer])[selected["id"]][
        "value_alternative"
    ]

    assert alternative["comparison_basis"] == "capability_match"
    assert alternative["shared_benchmark"] is None
    assert alternative["benchmark_delta"] is None
    assert "performance not compared" in alternative["reason"]


def test_alternative_delta_uses_only_shared_benchmark_dimensions() -> None:
    selected = _model(
        "vendor/selected",
        prompt=0.000010,
        completion=0.000030,
        coding=None,
    )
    selected["benchmarks"] = {
        "artificial_analysis": {"coding_index": 80.0, "intelligence_index": 20.0},
    }
    peer = _model(
        "vendor/peer",
        prompt=0.000001,
        completion=0.000003,
        coding=None,
    )
    peer["benchmarks"] = {
        "artificial_analysis": {"coding_index": 76.0, "agentic_index": 100.0},
    }

    alternative = catalog_value_annotations([selected, peer], profile="backend")[
        selected["id"]
    ]["value_alternative"]

    assert alternative["comparison_basis"] == "task_benchmark"
    assert alternative["benchmark_delta"] == -4.0
    assert alternative["shared_benchmark"]["dimensions"] == [
        "artificial_analysis.coding_index"
    ]
    assert alternative["performance_label"] == "shared benchmark comparison"


def test_catalog_annotations_cache_by_fingerprint_and_profile(monkeypatch) -> None:
    records = [
        _model("vendor/a", prompt=0.000010, completion=0.000030, coding=80.0),
        _model("vendor/b", prompt=0.000001, completion=0.000003, coding=76.0),
    ]
    model_value.clear_value_annotation_cache()
    original = model_value._compute_annotations
    calls = 0

    def counted(catalog, profile):
        nonlocal calls
        calls += 1
        return original(catalog, profile)

    monkeypatch.setattr(model_value, "_compute_annotations", counted)
    first = catalog_value_annotations(records, profile="backend")
    reordered = catalog_value_annotations(list(reversed(records)), profile="backend")
    assert calls == 1
    assert first == reordered

    reordered_fields = deepcopy(records)
    for record in reordered_fields:
        record["supported_parameters"].reverse()
        record["architecture"]["input_modalities"].reverse()
    assert catalog_value_annotations(reordered_fields, profile="backend") == reordered
    assert calls == 1

    # Callers receive copies; mutating one response cannot poison the cache.
    first["vendor/a"]["relative_price_band"] = "mutated"
    assert catalog_value_annotations(records, profile="backend")["vendor/a"][
        "relative_price_band"
    ] != "mutated"
    assert calls == 1

    catalog_value_annotations(records, profile="docs")
    assert calls == 2
    changed = [dict(records[0]), dict(records[1])]
    changed[0]["pricing"] = {"prompt": 0.000020, "completion": 0.000060}
    catalog_value_annotations(changed, profile="backend")
    assert calls == 3


async def test_cached_web_catalog_primes_core_router_without_another_fetch(monkeypatch) -> None:
    import skyn3t.web.routes as routes

    catalog = [_model("vendor/a", prompt=0.000001, completion=0.000002, coding=70.0)]
    monkeypatch.setitem(routes._MODELS_CACHE, "catalog", catalog)
    monkeypatch.setitem(routes._MODELS_CACHE, "models", ["vendor/a"])
    monkeypatch.setitem(routes._MODELS_CACHE, "note", "ok")
    monkeypatch.setitem(routes._MODELS_CACHE, "ts", 9_999_999_999.0)
    primed = {}

    def prime(records, fetched_at=None):
        primed["records"] = records
        primed["fetched_at"] = fetched_at

    monkeypatch.setattr(routes, "prime_live_catalog", prime)

    loaded, note = await routes._load_openrouter_catalog(
        SimpleNamespace(settings=SimpleNamespace())
    )

    assert loaded is catalog
    assert note == "ok"
    assert primed == {"records": catalog, "fetched_at": 9_999_999_999.0}


async def test_models_bootstrap_stays_compact_and_skips_full_annotations(monkeypatch) -> None:
    import skyn3t.web.routes as routes

    catalog = [
        _model(
            f"vendor/model-{index}",
            prompt=0.000001,
            completion=0.000002,
            coding=70.0,
        )
        for index in range(500)
    ]

    async def fake_catalog(_state, _refresh=False):
        return catalog, "ok"

    monkeypatch.setattr(routes, "_load_openrouter_catalog", fake_catalog)
    monkeypatch.setattr(
        routes,
        "catalog_value_annotations",
        lambda _catalog: (_ for _ in ()).throw(
            AssertionError("compact endpoint must not build full annotations")
        ),
    )

    payload = await routes.list_openrouter_models(
        SimpleNamespace(settings=SimpleNamespace())
    )

    assert payload["count"] == 500
    assert "items" not in payload
    assert payload["details_endpoint"] == "/models/catalog"
    assert len(json.dumps(payload)) < 20_000


async def test_catalog_api_returns_structured_price_and_value_comparison(monkeypatch):
    import skyn3t.web.routes as routes

    fable = _model(
        "anthropic/claude-fable-5",
        prompt=0.000010,
        completion=0.000050,
        coding=76.5,
        agentic=52.8,
        intelligence=59.9,
        context_length=1_000_000,
    )
    peer = _model(
        "x-ai/grok-4.5",
        prompt=0.000002,
        completion=0.000006,
        coding=72.4,
        agentic=45.7,
        intelligence=53.8,
    )

    async def fake_catalog(_state, _refresh=False):
        return [fable, peer], "ok"

    monkeypatch.setattr(routes, "_load_openrouter_catalog", fake_catalog)
    payload = await routes.list_openrouter_model_catalog(
        SimpleNamespace(settings=SimpleNamespace()),
        query="fable",
        limit=10,
    )

    assert payload["example_workload"] == EXAMPLE_WORKLOAD
    assert payload["example_workload"]["is_limit"] is False
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["example_cost_usd"] == 0.6
    assert item["price_example"]["workload_id"] == EXAMPLE_WORKLOAD["id"]
    assert item["benchmark"]["profile"] == "app_building"
    assert item["value_alternative"]["id"] == "x-ai/grok-4.5"
    assert item["value_alternative"]["savings_percent"] == 85.3
