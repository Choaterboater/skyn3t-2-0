"""`newest:<family>` auto-resolves to the freshest live model so a pin never ages
into a stale (e.g. 2024) model. OpenRouter rotates models constantly."""
from __future__ import annotations

import json

import skyn3t.core.model_router as mr
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import ModelRouter, Tier, newest_paid_model


def _fake_catalog():
    return [
        {"id": "deepseek/deepseek-chat", "created": 1_700_000_000},        # old (2023)
        {"id": "deepseek/deepseek-v3.1", "created": 1_755_000_000},
        {"id": "deepseek/deepseek-v3.2", "created": 1_765_000_000},        # newest deepseek
        {"id": "deepseek/deepseek-v3.2-exp", "created": 1_766_000_000},    # excluded: -exp
        {"id": "deepseek/deepseek-r1", "created": 1_767_000_000},          # excluded: r1
        {"id": "deepseek/deepseek-v3.2:free", "created": 1_768_000_000},   # excluded: :free
        {"id": "moonshotai/kimi-k2.7-code", "created": 1_780_000_000},     # newest CODER (2026)
    ]


def _benchmark_catalog():
    return [
        {
            "id": "deepseek/deepseek-v3.2",
            "created": 1_765_000_000,
            "context_length": 128_000,
            "pricing": {"prompt": "0.00000020", "completion": "0.00000080"},
            "architecture": {"output_modalities": ["text"]},
            "benchmarks": {
                "design_arena": [
                    {"arena": "models", "category": "website", "elo": 1210, "rank": 30},
                    {"arena": "models", "category": "codecategories", "elo": 1220, "rank": 28},
                ],
                "artificial_analysis": {
                    "coding_index": 50.0,
                    "agentic_index": 35.0,
                    "intelligence_index": 45.0,
                },
            },
        },
        {
            "id": "z-ai/glm-5.2",
            "created": 1_781_000_000,
            "context_length": 1_000_000,
            "pricing": {"prompt": "0.000000742", "completion": "0.000002332"},
            "architecture": {"output_modalities": ["text"]},
            "benchmarks": {
                "design_arena": [
                    {"arena": "models", "category": "website", "elo": 1356, "rank": 1},
                    {"arena": "models", "category": "uicomponent", "elo": 1338, "rank": 5},
                    {"arena": "models", "category": "codecategories", "elo": 1360, "rank": 1},
                    {"arena": "agents", "category": "fullstack", "elo": 1292, "rank": 3},
                    {"arena": "agents", "category": "webapps", "elo": 1284, "rank": 3},
                ],
                "artificial_analysis": {
                    "coding_index": 68.8,
                    "agentic_index": 43.1,
                    "intelligence_index": 51.1,
                },
            },
        },
        {
            "id": "anthropic/claude-fable-5",
            "created": 1_782_000_000,
            "context_length": 1_000_000,
            "pricing": {"prompt": "0.000010", "completion": "0.000050"},
            "architecture": {"output_modalities": ["text"]},
            "benchmarks": {
                "design_arena": [
                    {"arena": "models", "category": "website", "elo": 1341, "rank": 2},
                    {"arena": "models", "category": "uicomponent", "elo": 1414, "rank": 1},
                    {"arena": "models", "category": "codecategories", "elo": 1349, "rank": 2},
                    {"arena": "models", "category": "gamedev", "elo": 1379, "rank": 1},
                ],
                "artificial_analysis": {
                    "coding_index": 76.5,
                    "agentic_index": 52.8,
                    "intelligence_index": 59.9,
                },
            },
        },
        {
            "id": "anthropic/claude-sonnet-5",
            "created": 1_782_500_000,
            "context_length": 1_000_000,
            "pricing": {"prompt": "0.000002", "completion": "0.000010"},
            "architecture": {"output_modalities": ["text"]},
            "benchmarks": {
                "design_arena": [
                    {"arena": "models", "category": "website", "elo": 1334, "rank": 5},
                    {"arena": "models", "category": "uicomponent", "elo": 1331, "rank": 6},
                    {"arena": "models", "category": "codecategories", "elo": 1326, "rank": 8},
                    {"arena": "agents", "category": "fullstack", "elo": 1288, "rank": 4},
                ],
                "artificial_analysis": {
                    "coding_index": 71.5,
                    "agentic_index": 46.7,
                    "intelligence_index": 53.4,
                },
            },
        },
        {
            "id": "openrouter/fusion",
            "created": 1_783_000_000,
            "context_length": 1_000_000,
            "pricing": {"prompt": "-1", "completion": "-1"},
            "architecture": {"output_modalities": ["text"]},
            "benchmarks": {
                "artificial_analysis": {
                    "coding_index": 90.0,
                    "agentic_index": 90.0,
                    "intelligence_index": 90.0,
                },
            },
        },
    ]


def test_live_scored_router_prefers_benchmark_quality_over_old_family(monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", _benchmark_catalog)
    monkeypatch.setattr(ModelRouter, "_load_overrides", lambda self: {})
    r = ModelRouter(Settings(free_only=False, no_claude=True))

    assert r.resolve(Tier.UI) == "z-ai/glm-5.2"
    assert r.resolve(Tier.BACKEND) == "z-ai/glm-5.2"
    assert r.resolve(Tier.STRONG) == "z-ai/glm-5.2"


def test_best_quality_profile_uses_top_non_fable_model(monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", _benchmark_catalog)
    monkeypatch.setattr(ModelRouter, "_load_overrides", lambda self: {})
    r = ModelRouter(Settings(free_only=False, no_claude=False))

    assert r.resolve(Tier.UI, profile="best_quality") == "z-ai/glm-5.2"


def test_fable_is_excluded_from_auto_routing_even_when_benchmarks_are_high(monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", _benchmark_catalog)
    monkeypatch.setattr(ModelRouter, "_load_overrides", lambda self: {})
    r = ModelRouter(Settings(free_only=False, no_claude=False))

    assert r.resolve(Tier.UI, profile="best_quality") != "anthropic/claude-fable-5"


def test_newest_coder_picks_freshest_across_families(monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", _fake_catalog)
    # 'coder' spans all strong coder families -> the 2026 kimi beats the deepseek-v3.x
    assert newest_paid_model("coder") == "moonshotai/kimi-k2.7-code"


def test_newest_picks_freshest_non_experimental(monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", _fake_catalog)
    assert newest_paid_model("deepseek-v3") == "deepseek/deepseek-v3.2"


def test_newest_none_when_catalog_unreachable(monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", lambda *a, **k: [])
    assert newest_paid_model("deepseek-v3") is None


def test_resolve_newest_sentinel(monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", _fake_catalog)
    monkeypatch.setattr(ModelRouter, "_load_overrides", lambda self: {"ui": "newest:deepseek-v3"})
    r = ModelRouter(Settings(free_only=False, no_claude=False))
    assert r.resolve(Tier.UI) == "deepseek/deepseek-v3.2"


def test_resolve_newest_falls_back_to_paid_default_offline(monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", lambda *a, **k: [])
    monkeypatch.setattr(ModelRouter, "_load_overrides", lambda self: {"ui": "newest:deepseek-v3"})
    r = ModelRouter(Settings(free_only=False, no_claude=False))
    model = r.resolve(Tier.UI)
    assert model and not model.startswith("newest:")
    assert "2024" not in model
    assert "qwen/" in model or "kimi" in model or "llama" in model or "codestral" in model


def test_resolve_uses_cached_paid_model_when_catalog_unavailable(tmp_path, monkeypatch):
    cache = tmp_path / "model_router_paid_fallback.json"
    cache.write_text(json.dumps({"ui": "openrouter/cached-model:latest"}))
    monkeypatch.setattr(mr, "live_catalog", lambda *a, **k: [])
    monkeypatch.setattr(ModelRouter, "_load_overrides", lambda self: {})
    r = ModelRouter(Settings(free_only=False, data_dir=tmp_path, no_claude=False))
    model = r.resolve(Tier.UI)
    assert model == "openrouter/cached-model:latest"


def test_resolve_writes_cached_paid_model_for_future_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "live_catalog", _fake_catalog)
    settings = Settings(free_only=False, data_dir=tmp_path, no_claude=False)
    r = ModelRouter(settings)
    model = r.resolve(Tier.BACKEND)
    assert model.startswith("moonshotai/") and model.endswith("-code")
    data = json.loads((tmp_path / "model_router_paid_fallback.json").read_text())
    assert data.get("backend") == model
