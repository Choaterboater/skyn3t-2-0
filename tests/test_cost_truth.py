"""Provider-source truth for LLM and external asset build costs."""

from __future__ import annotations

import asyncio

import skyn3t.adapters.replicate as replicate_module
from skyn3t.adapters.replicate import ReplicateClient
from skyn3t.config.settings import Settings
from skyn3t.observability.cost_tracker import CostTracker
from skyn3t.studio.assets import generate_assets
from skyn3t.studio.build_summary import build_summary

_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


class _Response:
    status_code = 201
    headers = {}

    def __init__(self, payload=None, content=b""):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_replicate_returns_provider_prediction_identity_and_timing(monkeypatch):
    prediction = {
        "id": "pred-exact-123",
        "model": "black-forest-labs/flux-schnell",
        "status": "succeeded",
        "output": ["https://replicate.delivery/image.png"],
        "metrics": {"predict_time": 1.25, "total_time": 1.75},
    }

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return _Response(prediction)

        async def get(self, url, **_kwargs):
            assert url == "https://replicate.delivery/image.png"
            return _Response(content=_PNG)

    monkeypatch.setattr(replicate_module.httpx, "AsyncClient", _Client)
    client = ReplicateClient(Settings(replicate_api_token="test-token"))

    images, evidence = asyncio.run(client.generate_images_with_evidence("a course"))

    assert images == [_PNG]
    assert evidence == [{
        "prediction_id": "pred-exact-123",
        "model": "black-forest-labs/flux-schnell",
        "status": "succeeded",
        "predict_time_seconds": 1.25,
        "total_time_seconds": 1.75,
    }]


class _EvidenceAssetClient:
    available = True

    async def generate_images_with_evidence(self, prompt, n=1, *, model=None, timeout=None):
        del prompt, n, timeout
        return [_PNG], [{
            "prediction_id": "asset-prediction-1",
            "model": model,
            "status": "succeeded",
            "predict_time_seconds": 2.0,
            "total_time_seconds": 2.5,
        }]


def test_asset_manifest_and_summary_separate_replicate_from_llm_dollars(tmp_path):
    settings = Settings(replicate_api_token="test-token", asset_gen=True)
    assets = asyncio.run(generate_assets(
        str(tmp_path),
        "an HVAC company website with a technician photo",
        settings=settings,
        client=_EvidenceAssetClient(),
        max_assets=1,
        stack="nextjs",
    ))

    usage = assets["usage"]
    assert usage["prediction_count"] == 1
    assert usage["predictions"][0]["prediction_id"] == "asset-prediction-1"
    assert usage["cost_usd"] is None
    assert usage["cost_source"] == "not_provided_by_replicate_api"

    summary = build_summary({
        "extra": {
            "build_cost_usd": 0.75,
            "max_unconfirmed_exposure_usd": 0.2,
            "llm_usage_evidence": [
                {"cost_source": "provider", "cost_usd": 0.5},
                {"cost_source": "catalog_estimate", "cost_usd": 0.25},
                {"cost_source": "unconfirmed", "cost_usd": 0.0},
            ],
            "assets": assets,
        }
    })
    truth = summary["cost_truth"]

    assert truth["llm_cost_usd"] == 0.75
    assert truth["llm_cost_classification"] == "mixed"
    assert truth["max_unconfirmed_exposure_usd"] == 0.2
    assert truth["external_asset_usage"]["prediction_count"] == 1
    assert truth["external_asset_usage"]["predict_time_seconds"] == 2.0
    assert truth["combined_total_known"] is False
    assert truth["combined_total_cost_usd"] is None


def test_historical_cost_without_generation_evidence_is_labeled_estimate():
    summary = build_summary({"extra": {"build_cost_usd": 7.586062}})

    truth = summary["cost_truth"]
    assert truth["llm_cost_usd"] == 7.586062
    assert truth["llm_cost_classification"] == "estimate"
    assert "source unavailable" in truth["llm_cost_label"]


def test_historical_replicate_assets_never_claim_zero_external_cost():
    summary = build_summary({
        "extra": {
            "build_cost_usd": 7.586062,
            "assets": {
                "generated": 2,
                "model": "black-forest-labs/flux-1.1-pro",
                "assets": [
                    {"subject": "course", "file": "/assets/course.webp"},
                    {"subject": "clubhouse", "file": "/assets/clubhouse.webp"},
                ],
            },
        }
    })

    truth = summary["cost_truth"]
    external = truth["external_asset_usage"]
    assert external["prediction_count"] == 0
    assert external["historical_generated_asset_count"] == 2
    assert external["provider_evidence_available"] is False
    assert external["dollar_cost_known"] is False
    assert external["cost_usd"] is None
    assert external["cost_source"] == "historical_provider_evidence_unavailable"
    assert truth["combined_total_known"] is False
    assert truth["combined_total_cost_usd"] is None


def test_successful_openrouter_cost_source_survives_missing_generation_id():
    class _Budget:
        def __init__(self):
            self.calls = []

        def reset_build(self):
            return None

    budget = _Budget()
    tracker = CostTracker(budget=budget)
    tracker.start_build("build-without-id")
    budget.calls.append(type("Call", (), {
        "backend": "openrouter",
        "model": "provider/model",
        "status": "succeeded",
        "generation_id": "",
        "cost_source": "provider",
        "cost_usd": 0.125,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "estimated_exposure_usd": 0.0,
    })())

    report = tracker.end_build("build-without-id")

    assert report["usage_evidence"] == [{
        "generation_id": None,
        "model": "provider/model",
        "backend": "openrouter",
        "status": "succeeded",
        "cost_usd": 0.125,
        "cost_source": "provider",
        "max_unconfirmed_exposure_usd": 0.0,
    }]
