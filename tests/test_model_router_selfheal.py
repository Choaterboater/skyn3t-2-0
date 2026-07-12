"""Router self-heals stale OpenRouter :free model ids against the live catalog.

OpenRouter retires ":free" model ids ~daily; a hardcoded default that 404s
silently degrades every build to the offline scaffold. The router now swaps a
retired id for a live valid one (preferring the right model per tier), and
leaves things untouched when the live list is unknown (offline) or still valid.
"""

from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.core import model_router as mr
from skyn3t.core.model_router import ModelRouter, Tier


def _router():
    return ModelRouter(Settings(
        llm_backend="openrouter", free_only=True, openrouter_api_key="sk-or-test"
    ))


def test_retired_free_model_is_substituted(monkeypatch):
    monkeypatch.setattr(mr, "live_free_model_ids",
                        lambda timeout=8.0: ["qwen/qwen3-coder:free",
                                             "meta-llama/llama-3.3-70b-instruct:free"])
    r = _router()
    # a retired default -> a live coder model for UI/backend tiers
    assert r._valid_free_model("deepseek/deepseek-chat-v3.1:free", Tier.UI) == "qwen/qwen3-coder:free"
    assert r._valid_free_model("deepseek/deepseek-chat-v3.1:free", Tier.BACKEND) == "qwen/qwen3-coder:free"
    # STRONG prefers the bigger instruct model when no coder-strong is present
    assert r._valid_free_model("deepseek/deepseek-r1:free", Tier.STRONG) == \
        "meta-llama/llama-3.3-70b-instruct:free"


def test_still_valid_model_unchanged(monkeypatch):
    monkeypatch.setattr(mr, "live_free_model_ids", lambda timeout=8.0: ["qwen/qwen3-coder:free"])
    assert _router()._valid_free_model("qwen/qwen3-coder:free", Tier.UI) == "qwen/qwen3-coder:free"


def test_offline_keeps_fallback(monkeypatch):
    # live list unknown (offline) -> never strands the build; keep the configured id
    monkeypatch.setattr(mr, "live_free_model_ids", lambda timeout=8.0: [])
    assert _router()._valid_free_model("deepseek/whatever:free", Tier.UI) == "deepseek/whatever:free"


def test_resolve_uses_live_model_with_key(monkeypatch):
    monkeypatch.setattr(mr, "live_free_model_ids", lambda timeout=8.0: ["qwen/qwen3-coder:free"])
    r = _router()
    assert r.resolve(Tier.UI, "App.jsx", task_type="codegen") == "qwen/qwen3-coder:free"


def test_no_key_skips_live_fetch(monkeypatch):
    # Without an OpenRouter key the backend is claude/stub — model id is unused, so
    # the router must NOT trigger a live fetch (would add latency for nothing).
    called = {"n": 0}

    def _spy(timeout=8.0):
        called["n"] += 1
        return ["qwen/qwen3-coder:free"]

    monkeypatch.setattr(mr, "live_free_model_ids", _spy)
    ModelRouter(Settings(free_only=True, openrouter_api_key="")).resolve(Tier.UI, "App.jsx")
    assert called["n"] == 0


def test_auto_with_a_dormant_key_skips_live_catalog(monkeypatch):
    called = {"n": 0}

    def _spy(timeout=8.0):
        called["n"] += 1
        return ["qwen/qwen3-coder:free"]

    monkeypatch.setattr(mr, "live_free_model_ids", _spy)
    ModelRouter(Settings(
        llm_backend="auto", free_only=True, openrouter_api_key="sk-or-dormant"
    )).resolve(Tier.UI, "App.jsx")
    assert called["n"] == 0
