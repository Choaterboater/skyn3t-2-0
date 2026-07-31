"""Cheap routing is a price contract, independent of build completeness."""

from __future__ import annotations

import asyncio
import time

import pytest

import skyn3t.core.model_router as mr
from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings
from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import ModelRouter, Tier, model_price_info
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.model_tournament import ModelTournament
from skyn3t.intelligence.routing_recommendations import LearnedModelRouter, RoutingRecommender
from skyn3t.studio.runner import StudioRunner

GROK = "x-ai/grok-4.5"
GROK_BUILD = "x-ai/grok-build-0.1"
VALUE = "z-ai/glm-5.2"


def _record(
    model_id: str,
    *,
    prompt: float,
    completion: float,
    coding: float,
    created: int,
) -> dict:
    return {
        "id": model_id,
        "created": created,
        "context_length": 500_000,
        "pricing": {"prompt": prompt, "completion": completion},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "benchmarks": {
            "artificial_analysis": {
                "coding_index": coding,
                "agentic_index": coding,
                "intelligence_index": coding,
            },
        },
    }


def _catalog() -> list[dict]:
    return [
        _record(
            GROK,
            prompt=0.000002,
            completion=0.000006,
            coding=99,
            created=200,
        ),
        _record(
            VALUE,
            prompt=0.00000049,
            completion=0.00000154,
            coding=70,
            created=100,
        ),
    ]


def _settings(tmp_path, **updates) -> Settings:
    return Settings(
        free_only=False,
        llm_backend="stub",
        data_dir=tmp_path,
        projects_dir=tmp_path / "projects",
        logs_dir=tmp_path / "logs",
        **updates,
    )


def test_grok_is_expensive_at_current_normalized_rates() -> None:
    info = model_price_info(_catalog()[0])

    assert info["price_class"] == "expensive"
    assert info["cheap_eligible"] is False
    assert info["prompt_usd_per_million"] == pytest.approx(2.0)
    assert info["completion_usd_per_million"] == pytest.approx(6.0)


def test_cheap_profile_hard_filters_raw_benchmark_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mr, "live_catalog", _catalog)
    router = ModelRouter(_settings(tmp_path))

    assert router.resolve(Tier.STRONG, profile="balanced") == GROK
    assert router.resolve(Tier.STRONG, profile="cheap_learned") == VALUE
    # A CHEAP stage carries the same price promise even in another build profile.
    assert router.resolve(Tier.CHEAP, profile="best_quality") == VALUE


def test_explicit_expensive_pin_is_honored_and_classified(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mr, "live_catalog", _catalog)
    router = ModelRouter(_settings(tmp_path, model_strong=GROK))

    assert router.resolve(Tier.STRONG, profile="cheap_learned") == GROK
    assert router.model_cost_info(GROK)["price_class"] == "expensive"


def test_cheap_profile_rejects_expensive_learned_tournament_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mr, "live_catalog", _catalog)
    monkeypatch.setattr(ModelRouter, "_default_model", lambda self, tier, profile="balanced": VALUE)
    tournament = ModelTournament(path=None)
    bucket = tournament.bucket_key(Tier.STRONG, "codegen")
    for _ in range(5):
        tournament.record_win(bucket, winner=GROK, losers=[])
    router = LearnedModelRouter(RoutingRecommender(tournament), settings=_settings(tmp_path))

    assert router.resolve(Tier.STRONG, task_type="codegen", profile="balanced") == GROK
    assert router.resolve(Tier.STRONG, task_type="codegen", profile="cheap_learned") == VALUE


def test_expensive_balanced_cache_cannot_leak_into_offline_cheap(tmp_path, monkeypatch) -> None:
    (tmp_path / "model_router_paid_fallback.json").write_text(
        '{"docs": "x-ai/grok-4.5"}', encoding="utf-8"
    )
    monkeypatch.setattr(mr, "live_catalog", lambda: [])
    router = ModelRouter(_settings(tmp_path))

    assert router.resolve(Tier.DOCS, profile="cheap_learned") == "deepseek/deepseek-v4-flash"


def test_live_catalog_with_no_eligible_paid_model_uses_honest_free_route(
    tmp_path, monkeypatch
) -> None:
    repriced_offline_default = _record(
        "deepseek/deepseek-v4-flash",
        prompt=0.000005,
        completion=0.000010,
        coding=99,
        created=300,
    )
    monkeypatch.setattr(mr, "live_catalog", lambda: [repriced_offline_default])
    router = ModelRouter(_settings(tmp_path))

    assert router.resolve(Tier.STRONG, profile="cheap_learned") == "openrouter/free"
    fallbacks = router.fallback_candidates(
        Tier.STRONG,
        primary="vendor/failed",
        profile="cheap_learned",
    )
    assert fallbacks == ["openrouter/free"]
    assert "deepseek/deepseek-v4-flash" not in fallbacks


def test_fast_profile_can_select_fast_premium_model(tmp_path, monkeypatch) -> None:
    premium_fast = _record(
        "vendor/turbo-premium",
        prompt=0.000002,
        completion=0.000006,
        coding=99,
        created=300,
    )
    cheap_value = _record(
        VALUE,
        prompt=0.00000049,
        completion=0.00000154,
        coding=55,
        created=200,
    )
    monkeypatch.setattr(mr, "live_catalog", lambda: [premium_fast, cheap_value])
    router = ModelRouter(_settings(tmp_path))

    assert router.resolve(Tier.STRONG, profile="fast") == "vendor/turbo-premium"
    assert router.resolve(Tier.STRONG, profile="cheap_learned") == VALUE
    assert router.fallback_candidates(
        Tier.STRONG,
        primary="vendor/failed",
        profile="fast",
    )[0] == "vendor/turbo-premium"


def test_no_claude_rewrite_preserves_cheap_profile(tmp_path, monkeypatch) -> None:
    claude = _record(
        "anthropic/claude-premium",
        prompt=0.000010,
        completion=0.000050,
        coding=100,
        created=400,
    )
    expensive_non_claude = _record(
        GROK,
        prompt=0.000002,
        completion=0.000006,
        coding=99,
        created=300,
    )
    value = _record(
        VALUE,
        prompt=0.00000049,
        completion=0.00000154,
        coding=70,
        created=200,
    )
    monkeypatch.setattr(mr, "live_catalog", lambda: [claude, expensive_non_claude, value])
    monkeypatch.setattr(ModelRouter, "_load_overrides", lambda self: {"strong": claude["id"]})
    router = ModelRouter(_settings(tmp_path, no_claude=True))

    assert router.resolve(Tier.STRONG, profile="cheap_learned") == VALUE


def test_cheap_fallback_pool_excludes_grok(tmp_path, monkeypatch) -> None:
    catalog = _catalog() + [
        _record(
            GROK_BUILD,
            prompt=0.000001,
            completion=0.000002,
            coding=98,
            created=300,
        )
    ]
    monkeypatch.setattr(mr, "live_catalog", lambda: catalog)
    router = ModelRouter(_settings(tmp_path))

    candidates = router.fallback_candidates(
        Tier.STRONG,
        primary=VALUE,
        profile="cheap_learned",
    )

    assert GROK not in candidates
    assert GROK_BUILD not in candidates
    assert router.resolve(Tier.BACKEND, profile="cheap_learned") == VALUE


def test_explicit_cheap_priced_grok_family_pin_remains_authoritative(
    tmp_path, monkeypatch
) -> None:
    catalog = _catalog() + [
        _record(
            GROK_BUILD,
            prompt=0.000001,
            completion=0.000002,
            coding=98,
            created=300,
        )
    ]
    monkeypatch.setattr(mr, "live_catalog", lambda: catalog)
    router = ModelRouter(_settings(tmp_path, model_backend=GROK_BUILD))

    assert router.resolve(Tier.BACKEND, profile="cheap_learned") == GROK_BUILD


def test_build_profile_reaches_shared_llm_router(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mr, "live_catalog", _catalog)

    class _Agent(BaseAgent):
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def execute(self, task: TaskRequest) -> TaskResult:
            result = await self.llm.complete("route me", tier=Tier.STRONG, task_type="codegen")
            return TaskResult(task_id=task.task_id, success=True, output={"model": result.model})

    agent = _Agent("priced", "codegen", "stub", EventBus())
    agent.llm = LLMClient(_settings(tmp_path))
    result = asyncio.run(
        agent.run(
            TaskRequest(
                type="codegen",
                payload={"extra": {"build_profile": "cheap_learned"}},
            )
        )
    )

    assert result.output["model"] == VALUE


def test_normal_build_injects_one_economy_profile_before_any_stage(tmp_path) -> None:
    bus = EventBus()
    runner = StudioRunner(
        bus,
        Orchestrator(bus),
        settings=_settings(tmp_path),
        memory=None,
    )
    captured: dict = {}
    sentinel = object()

    async def fake_start_build(brief, slug=None, extra=None):
        captured.update(brief=brief, slug=slug, extra=dict(extra or {}))
        return sentinel

    runner._start_build = fake_start_build  # type: ignore[method-assign]

    result = asyncio.run(runner.start("a complete app"))

    assert result is sentinel
    assert captured["extra"]["build_profile"] == "cheap_learned"


def test_openrouter_accounting_prefers_exact_cost_then_catalog_rates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mr, "live_catalog", _catalog)
    client = LLMClient(_settings(tmp_path))

    assert client._openrouter_cost(GROK, {"cost": 1.2345}, 1_000_000, 1_000_000) == 1.2345
    assert client._openrouter_cost(GROK, {}, 1_000_000, 1_000_000) == pytest.approx(8.0)


def test_agentic_usage_cost_is_recorded_exactly_once_per_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mr, "live_catalog", _catalog)
    client = LLMClient(_settings(tmp_path))
    message = {"content": "done"}

    client._record_openrouter_agentic_usage(
        GROK,
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.25}},
        [],
        message,
    )
    client._record_openrouter_agentic_usage(
        GROK,
        {"usage": {"prompt_tokens": 20, "completion_tokens": 8, "cost": 0.40}},
        [],
        message,
    )

    assert client.budget.spent_build == pytest.approx(0.65)
    assert [call.cost_usd for call in client.budget.calls] == [0.25, 0.40]


def test_timeout_records_unconfirmed_exposure_without_claiming_charge(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mr, "live_catalog", _catalog)
    client = LLMClient(_settings(tmp_path))

    client._record_failed_openrouter_attempt(
        GROK,
        TimeoutError("response deadline"),
        prompt_tokens=1_000,
        max_completion_tokens=500,
    )

    attempt = client.budget.calls[-1]
    assert attempt.cost_usd == 0.0
    assert attempt.status == "failed_transient"
    assert attempt.estimated_exposure_usd > 0


def test_primed_web_catalog_prevents_duplicate_core_fetch(monkeypatch) -> None:
    free = {
        "id": "vendor/value:free",
        "created": 201,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }
    catalog = [*_catalog(), free]
    monkeypatch.setattr(mr, "_LIVE_CATALOG", None)
    monkeypatch.setattr(mr, "_LIVE_CATALOG_AT", 0.0)
    monkeypatch.setattr(mr, "_LIVE_FREE_IDS", None)
    monkeypatch.setattr(mr, "_LIVE_FREE_AT", 0.0)
    mr.prime_live_catalog(catalog, fetched_at=time.time())

    def duplicate_fetch(*_args, **_kwargs):
        raise AssertionError("catalog should have been reused")

    monkeypatch.setattr("urllib.request.urlopen", duplicate_fetch)

    assert [item["id"] for item in mr.live_catalog()] == [item["id"] for item in catalog]
    assert mr.live_free_model_ids() == [free["id"]]


def test_free_ids_derive_from_fresh_catalog_without_second_fetch(monkeypatch) -> None:
    """An expired free-id cache next to a FRESH catalog snapshot must derive from
    the catalog instead of re-fetching the identical endpoint (the duplicate-cache
    double fetch that stacked two blocking 8s calls per degraded window)."""
    free = {
        "id": "vendor/value:free",
        "created": 201,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }
    monkeypatch.setattr(mr, "_LIVE_CATALOG", [*_catalog(), free])
    monkeypatch.setattr(mr, "_LIVE_CATALOG_AT", time.time())
    monkeypatch.setattr(mr, "_LIVE_FREE_IDS", None)
    monkeypatch.setattr(mr, "_LIVE_FREE_AT", 0.0)
    monkeypatch.setattr(mr, "_LIVE_FREE_ERROR_AT", 0.0)

    def no_fetch(*_args, **_kwargs):
        raise AssertionError("free ids should derive from the fresh catalog")

    monkeypatch.setattr("urllib.request.urlopen", no_fetch)

    assert mr.live_free_model_ids() == [free["id"]]
    assert mr.live_free_model_ids() == [free["id"]]  # now served from its own cache


def test_catalog_fetch_refreshes_free_ids_cache(monkeypatch) -> None:
    """One successful live_catalog() fetch must feed BOTH caches, so the free-id
    path never fires a second network call for the same endpoint in-window."""
    import json as _json

    free = {
        "id": "vendor/value:free",
        "created": 201,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }
    catalog = [*_catalog(), free]
    calls = {"n": 0}

    class _Payload:
        def read(self):
            return _json.dumps({"data": catalog}).encode()

    def fake_urlopen(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError("the single catalog fetch must feed both caches")
        return _Payload()

    monkeypatch.setattr(mr, "_LIVE_CATALOG", None)
    monkeypatch.setattr(mr, "_LIVE_CATALOG_AT", 0.0)
    monkeypatch.setattr(mr, "_LIVE_CATALOG_ERROR_AT", 0.0)
    monkeypatch.setattr(mr, "_LIVE_FREE_IDS", None)
    monkeypatch.setattr(mr, "_LIVE_FREE_AT", 0.0)
    monkeypatch.setattr(mr, "_LIVE_FREE_ERROR_AT", 0.0)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert [item["id"] for item in mr.live_catalog()] == [item["id"] for item in catalog]
    assert mr.live_free_model_ids() == [free["id"]]
    assert calls["n"] == 1
