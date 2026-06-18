"""Runtime LLM config endpoints — set keys / switch backend from the dashboard.

Handlers are exercised directly with persist=False so no .env is written.
"""

from __future__ import annotations

import pytest

from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings

pytest.importorskip("skyn3t.web.deps")
from skyn3t.web.deps import AppState  # noqa: E402
from skyn3t.web.routes import (  # noqa: E402
    llm_secrets_payload,
    set_llm_backend,
    set_llm_key,
)


def _state(**kw) -> AppState:
    s = Settings(**kw)
    return AppState(settings=s, llm_client=LLMClient(s))


async def test_secrets_payload_shape():
    p = await llm_secrets_payload(_state(llm_backend="stub"))
    assert set(p["providers"]) == {"openrouter", "anthropic", "openai", "kimi"}
    assert p["backend"] == "stub"
    assert p["backend_pref"] == "stub"


async def test_set_key_flips_backend_to_openrouter():
    r = await set_llm_key(_state(llm_backend="auto"), "openrouter", "sk-or-x", persist=False)
    assert r["configured"] is True
    assert r["backend"] == "openrouter"


async def test_clear_key():
    st = _state(llm_backend="auto", openrouter_api_key="sk-or-x")
    r = await set_llm_key(st, "openrouter", "", persist=False)
    assert r["configured"] is False


async def test_switch_backend():
    r = await set_llm_backend(_state(llm_backend="auto"), "stub", persist=False)
    assert r["active"] == "stub"


async def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        await set_llm_key(_state(), "bogus", "x", persist=False)


async def test_integration_credential_config(monkeypatch):
    monkeypatch.delenv("SKYN3T_TELEGRAM_BOT_TOKEN", raising=False)
    from skyn3t.web.routes import integrations_payload, set_integration_credential

    st = _state()
    assert (await integrations_payload(st))["channels"]["telegram"]["configured"] is False
    r = await set_integration_credential(st, "telegram", token="123:ABC", target="-100", persist=False)
    assert r["configured"] is True and r["target_set"] is True
    assert (await integrations_payload(st))["channels"]["telegram"]["configured"] is True


async def test_integration_unknown_channel_rejected():
    from skyn3t.web.routes import set_integration_credential

    with pytest.raises(ValueError):
        await set_integration_credential(_state(), "bogus", token="x", persist=False)
