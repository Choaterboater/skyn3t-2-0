"""Learned-routing guard pins: free-only ids, CLI labels, paid text modality."""

from __future__ import annotations

import skyn3t.core.model_router as mr
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import ModelRouter, Tier
from skyn3t.intelligence.model_tournament import ModelTournament
from skyn3t.intelligence.routing_recommendations import LearnedModelRouter, RoutingRecommender


def _learned(tmp_path, winner: str, *, free_only: bool) -> tuple[LearnedModelRouter, Settings]:
    t = ModelTournament(tmp_path / "t.json")
    bucket = t.bucket_key(Tier.STRONG, "")
    for _ in range(6):  # clears min_plays(5) + min_win_rate(0.55)
        t.record_win(bucket, winner, ["loser/model:free"])
    s = Settings(free_only=free_only, llm_backend="stub", data_dir=tmp_path)
    return LearnedModelRouter(RoutingRecommender(t), settings=s), s


def test_free_only_rejects_paid_model_with_free_in_name(tmp_path):
    # 'free' as a name substring is not the ':free' suffix cost contract.
    learned, s = _learned(tmp_path, "vendor/freedom-premium", free_only=True)
    assert learned.resolve(Tier.STRONG) == ModelRouter(s).resolve(Tier.STRONG)


def test_free_only_still_allows_openrouter_free_router(tmp_path):
    learned, _ = _learned(tmp_path, "openrouter/free", free_only=True)
    assert learned.resolve(Tier.STRONG) == "openrouter/free"


def test_cli_transport_label_never_routes_to_openrouter(tmp_path, monkeypatch):
    # CLI wins enter the tournament as '<provider>-cli' labels; those are
    # legitimate leaderboard citizens but not routable vendor/model ids.
    monkeypatch.setattr(mr, "live_catalog", lambda: [])
    learned, s = _learned(tmp_path, "codex-cli", free_only=False)
    assert learned.resolve(Tier.STRONG) == ModelRouter(s).resolve(Tier.STRONG)


def _paid_record(model_id: str, *, inputs: list[str], coding: float, created: int) -> dict:
    return {
        "id": model_id,
        "created": created,
        "context_length": 500_000,
        "pricing": {"prompt": 0.000002, "completion": 0.000006},
        "architecture": {"input_modalities": inputs, "output_modalities": ["text"]},
        "benchmarks": {
            "artificial_analysis": {
                "coding_index": coding,
                "agentic_index": coding,
                "intelligence_index": coding,
            },
        },
    }


def test_paid_catalog_route_requires_text_input_modality(tmp_path, monkeypatch):
    # The higher-scoring model only ACCEPTS images; a text route must not pick it.
    monkeypatch.setattr(
        mr,
        "live_catalog",
        lambda: [
            _paid_record("vendor/pixels-only", inputs=["image"], coding=99, created=200),
            _paid_record("vendor/coder", inputs=["text"], coding=70, created=100),
        ],
    )
    router = ModelRouter(Settings(free_only=False, llm_backend="stub", data_dir=tmp_path))

    assert router.resolve(Tier.STRONG, profile="balanced") == "vendor/coder"
