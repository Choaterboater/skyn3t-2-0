"""`newest:<family>` auto-resolves to the freshest live model so a pin never ages
into a stale (e.g. 2024) model. OpenRouter rotates models constantly."""
from __future__ import annotations

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
    assert model.startswith("deepseek/") and "2024" not in model and not model.startswith("newest:")
