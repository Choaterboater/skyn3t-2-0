"""Provider-source truth for LLM and external asset build costs."""

from __future__ import annotations

import asyncio

import skyn3t.adapters.replicate as replicate_module
from skyn3t.adapters.replicate import ReplicateClient
from skyn3t.config.settings import Settings
from skyn3t.observability.cost_tracker import CostTracker
from skyn3t.studio.assets import generate_assets
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner

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


def test_successful_cli_cost_is_evidence_but_exact_dollars_remain_unknown():
    class _Budget:
        def __init__(self):
            self.calls = []

        def reset_build(self):
            return None

    budget = _Budget()
    tracker = CostTracker(budget=budget)
    tracker.start_build("cli-build")
    budget.calls.append(type("Call", (), {
        "backend": "codex_cli",
        "model": "codex-cli",
        "status": "cli_response",
        "generation_id": "",
        "cost_source": "not_reported_by_cli",
        # LLMResult remains numerically additive for BudgetTracker, but the
        # evidence layer must not present this placeholder as an exact zero.
        "cost_usd": 0.0,
        "prompt_tokens": 100,
        "completion_tokens": 25,
        "estimated_exposure_usd": 0.0,
    })())

    report = tracker.end_build("cli-build")
    evidence = report["usage_evidence"]

    assert evidence == [{
        "generation_id": None,
        "model": "codex-cli",
        "backend": "codex_cli",
        "status": "cli_response",
        "cost_usd": None,
        "cost_source": "not_reported_by_cli",
        "max_unconfirmed_exposure_usd": 0.0,
        "cost_usd_known": False,
    }]

    truth = build_summary({
        "extra": {
            "build_cost_usd": report["cost_usd"],
            "llm_usage_evidence": evidence,
        }
    })["cost_truth"]
    assert truth["llm_cost_usd"] is None
    assert truth["llm_known_cost_usd"] == 0.0
    assert truth["llm_cost_known"] is False
    assert truth["llm_cost_classification"] == "unknown"
    assert truth["llm_cost_label"] == "local CLI cost unknown"
    assert truth["combined_total_known"] is False


def test_truncated_cost_evidence_keeps_earlier_cli_cost_unknown():
    class _Budget:
        def __init__(self):
            self.calls = []

        def reset_build(self):
            return None

    def call(*, backend, source, cost):
        return type("Call", (), {
            "backend": backend,
            "model": "test-model",
            "status": "succeeded",
            "generation_id": "",
            "cost_source": source,
            "cost_usd": cost,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "estimated_exposure_usd": 0.0,
        })()

    budget = _Budget()
    tracker = CostTracker(budget=budget)
    tracker.start_build("truncated-cli-build")
    budget.calls.append(call(
        backend="codex_cli",
        source="not_reported_by_cli",
        cost=0.0,
    ))
    budget.calls.extend(
        call(backend="openrouter", source="provider", cost=0.01)
        for _ in range(256)
    )

    runner = object.__new__(StudioRunner)
    runner.cost_tracker = tracker
    manifest = BuildManifest(
        slug="truncated-cli-build",
        brief="verify durable truncated cost truth",
        status="failed",
        verdict="no_go",
    )
    report = runner._settle_build_cost(manifest, "truncated-cli-build")
    truth = build_summary(manifest.to_dict())["cost_truth"]

    assert report["usage_evidence_truncated"] == 1
    assert len(report["usage_evidence"]) == 256
    assert report["usage_evidence_source_counts"] == {
        "not_reported_by_cli": 1,
        "provider": 256,
    }
    assert manifest.extra["llm_usage_source_counts"] == {
        "not_reported_by_cli": 1,
        "provider": 256,
    }
    assert truth["source_counts"] == {
        "not_reported_by_cli": 1,
        "provider": 256,
    }
    assert truth["evidence_count"] == 257
    assert truth["evidence_retained_count"] == 256
    assert truth["evidence_truncated_count"] == 1
    assert truth["evidence_source_counts_complete"] is True
    assert truth["llm_known_cost_usd"] == 2.56
    assert truth["llm_cost_usd"] is None
    assert truth["llm_cost_known"] is False
    assert truth["llm_cost_classification"] == "partial"


def test_complete_provider_aggregate_keeps_truncated_known_cost_known():
    truth = build_summary({
        "extra": {
            "build_cost_usd": 2.57,
            "llm_usage_evidence": [
                {"cost_source": "provider", "cost_usd": 0.01}
                for _ in range(256)
            ],
            "llm_usage_evidence_truncated": 1,
            "llm_usage_source_counts": {"provider": 257},
        }
    })["cost_truth"]

    assert truth["llm_cost_known"] is True
    assert truth["llm_cost_usd"] == 2.57
    assert truth["evidence_source_counts_complete"] is True


def test_truncated_legacy_evidence_without_aggregate_is_conservatively_unknown():
    truth = build_summary({
        "extra": {
            "build_cost_usd": 2.56,
            "llm_usage_evidence": [
                {"cost_source": "provider", "cost_usd": 0.01}
                for _ in range(256)
            ],
            "llm_usage_evidence_truncated": 1,
        }
    })["cost_truth"]

    assert truth["llm_cost_known"] is False
    assert truth["llm_cost_usd"] is None
    assert truth["evidence_source_counts_complete"] is False
    assert truth["llm_cost_classification"] == "partial"


def test_unsuccessful_unsettled_global_cli_cost_is_unknown():
    for status in ("interrupted", "cancelled", "failed"):
        truth = build_summary({
            "status": status,
            "extra": {
                "llm_backend": "codex_cli",
                "build_cost_usd": 0.0,
            },
        })["cost_truth"]

        assert truth["llm_cost_known"] is False
        assert truth["llm_cost_usd"] is None
        assert truth["llm_cost_classification"] == "unknown"
        assert truth["usage_settlement_conclusive"] is False
        assert "not settled" in truth["llm_cost_label"]


def test_unsuccessful_unsettled_codegen_cli_cost_is_unknown():
    truth = build_summary({
        "status": "failed",
        "extra": {
            "llm_backend": "openrouter",
            "codegen_cli_provider": "claude",
            "build_cost_usd": 0.15,
        },
    })["cost_truth"]

    assert truth["llm_known_cost_usd"] == 0.15
    assert truth["llm_cost_usd"] is None
    assert truth["llm_cost_known"] is False


def test_terminal_stub_and_settled_cli_zeroes_remain_known():
    stub = build_summary({
        "status": "failed",
        "extra": {"llm_backend": "stub", "build_cost_usd": 0.0},
    })["cost_truth"]
    settled_cli = build_summary({
        "status": "cancelled",
        "extra": {
            "llm_backend": "codex_cli",
            "build_cost_usd": 0.0,
            "llm_usage_settled": True,
        },
    })["cost_truth"]

    assert stub["llm_cost_known"] is True
    assert stub["llm_cost_usd"] == 0.0
    assert settled_cli["llm_cost_known"] is True
    assert settled_cli["llm_cost_usd"] == 0.0
    assert settled_cli["usage_settlement_conclusive"] is True
