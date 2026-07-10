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
    set_asset_gen,
    set_llm_backend,
    set_llm_key,
    set_llm_routing,
    set_replicate_token,
    submit_build,
)


def _state(**kw) -> AppState:
    s = Settings(**kw)
    return AppState(settings=s, llm_client=LLMClient(s))


async def test_secrets_payload_shape():
    p = await llm_secrets_payload(_state(llm_backend="stub"))
    assert set(p["providers"]) == {"openrouter", "anthropic", "openai", "kimi"}
    assert p["backend"] == "stub"
    assert p["backend_pref"] == "stub"
    assert p["free_only"] is True
    assert "routing" in p
    assert "model_pins" in p


async def test_set_key_flips_backend_to_openrouter():
    r = await set_llm_key(_state(llm_backend="auto"), "openrouter", "sk-or-x", persist=False)
    assert r["configured"] is True
    assert r["backend"] == "openrouter"


async def test_set_key_persist_false_does_not_mutate_env(monkeypatch):
    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    st = _state(llm_backend="auto")

    r = await set_llm_key(st, "openrouter", "sk-or-x", persist=False)

    import os
    assert r["configured"] is True
    assert st.settings.openrouter_api_key == "sk-or-x"
    assert "SKYN3T_OPENROUTER_API_KEY" not in os.environ


async def test_secrets_payload_reports_openrouter_env_key(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_API_KEY", "sk-or-env")
    p = await llm_secrets_payload(_state(llm_backend="auto"))
    assert p["providers"]["openrouter"] is True


async def test_secrets_payload_routes_plain_openrouter_env_key(monkeypatch):
    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    p = await llm_secrets_payload(_state(llm_backend="auto"))
    assert p["providers"]["openrouter"] is True
    assert p["backend"] == "openrouter"
    assert p["routing"]["openrouter_configured"] is True


async def test_clear_key():
    st = _state(llm_backend="auto", openrouter_api_key="sk-or-x")
    r = await set_llm_key(st, "openrouter", "", persist=False)
    assert r["configured"] is False


async def test_switch_backend():
    r = await set_llm_backend(_state(llm_backend="auto"), "stub", persist=False)
    assert r["active"] == "stub"
    assert r["routing"]["requested"] == "stub"


async def test_switch_backend_accepts_codex_and_reports_missing_cli_without_openrouter():
    st = _state(llm_backend="openrouter", openrouter_api_key="sk-or-x")
    r = await set_llm_backend(st, "codex_cli", persist=False)
    assert st.settings.llm_backend == "codex_cli"
    assert r["requested"] == "codex_cli"
    assert r["active"] == "stub"
    assert r["routing"]["state"] == "cli_missing"


async def test_switch_backend_rejects_unknown_value():
    st = _state(llm_backend="stub")
    with pytest.raises(ValueError, match="Unsupported LLM backend"):
        await set_llm_backend(st, "arbitrary_cli", persist=False)
    assert st.settings.llm_backend == "stub"


async def test_explicit_openrouter_missing_key_surfaces_routing_state():
    p = await llm_secrets_payload(_state(llm_backend="openrouter"))
    assert p["backend"] == "stub"
    assert p["routing"]["requested"] == "openrouter"
    assert p["routing"]["state"] == "missing_key"


async def test_submit_build_blocks_unavailable_selected_backend_before_queueing():
    st = _state(llm_backend="openrouter")

    with pytest.raises(ValueError, match="OpenRouter was explicitly selected"):
        await submit_build(st, brief="must not become an offline prototype")

    assert st.builds == {}


async def test_submit_build_blocks_unavailable_codegen_cli_before_paid_stages():
    st = _state(
        llm_backend="openrouter",
        openrouter_api_key="sk-or-configured",
        codegen_cli_provider="codex",
    )

    with pytest.raises(ValueError, match="codegen_cli_provider='codex'.*unavailable"):
        await submit_build(st, brief="must not spend before codegen can run")

    assert st.builds == {}


async def test_set_llm_routing_updates_codegen_and_model_pins():
    st = _state(llm_backend="openrouter", openrouter_api_key="sk-or-x", free_only=False)
    r = await set_llm_routing(
        st,
        codegen_cli_provider="",
        codegen_cli_model="",
        openrouter_codegen_model="provider/codegen",
        model_pins={"backend": "provider/backend", "ui": "provider/ui"},
        persist=False,
    )
    assert st.settings.openrouter_codegen_model == "provider/codegen"
    assert st.settings.model_backend == "provider/backend"
    assert r["tiers"]["backend"] == "provider/backend"
    p = await llm_secrets_payload(st)
    assert p["openrouter_codegen_model"] == "provider/codegen"
    assert p["model_pins"]["ui"] == "provider/ui"


async def test_set_llm_routing_compacts_model_inputs():
    st = _state(llm_backend="openrouter", openrouter_api_key="sk-or-x")
    await set_llm_routing(
        st,
        codegen_cli_provider="",
        codegen_cli_model=" provider / model\ncli ",
        openrouter_codegen_model=" provider  /  chosen ",
        model_pins={
            "cheap": " provider / cheap ",
            "ui": "  provider/ui  ",
            "backend": "provider/backend",
            "strong": "",
            "docs": "   ",
        },
        persist=False,
    )
    assert st.settings.codegen_cli_model == "provider/modelcli"
    assert st.settings.openrouter_codegen_model == "provider/chosen"
    assert st.settings.model_cheap == "provider/cheap"
    assert st.settings.model_ui == "provider/ui"


async def test_set_llm_routing_rejects_unknown_codegen_provider():
    st = _state(llm_backend="openrouter", openrouter_api_key="sk-or-x")
    with pytest.raises(ValueError, match="Unsupported codegen_cli_provider"):
        await set_llm_routing(
            st,
            codegen_cli_provider="definitely-not-a-cli",
            persist=False,
        )
    assert st.settings.codegen_cli_provider == ""


async def test_set_llm_routing_accepts_codex_codegen_provider():
    st = _state(llm_backend="stub")
    await set_llm_routing(st, codegen_cli_provider="codex", persist=False)
    assert st.settings.codegen_cli_provider == "codex"


async def test_set_llm_routing_persist_false_does_not_mutate_env(monkeypatch):
    monkeypatch.delenv("SKYN3T_OPENROUTER_CODEGEN_MODEL", raising=False)
    monkeypatch.delenv("SKYN3T_MODEL_UI", raising=False)
    st = _state(llm_backend="openrouter", openrouter_api_key="sk-or-x")

    await set_llm_routing(
        st,
        openrouter_codegen_model="provider/codegen",
        model_pins={"ui": "provider/ui"},
        persist=False,
    )

    import os
    assert "SKYN3T_OPENROUTER_CODEGEN_MODEL" not in os.environ
    assert "SKYN3T_MODEL_UI" not in os.environ
    assert st.settings.openrouter_codegen_model == "provider/codegen"
    assert st.settings.model_ui == "provider/ui"


async def test_set_llm_routing_updates_free_only_without_clearing_pins(monkeypatch):
    monkeypatch.delenv("SKYN3T_FREE_ONLY", raising=False)
    st = _state(
        llm_backend="openrouter",
        openrouter_api_key="sk-or-x",
        free_only=True,
        openrouter_codegen_model="provider/codegen",
        model_ui="provider/ui",
    )

    r = await set_llm_routing(st, free_only=False, persist=False)

    import os
    assert st.settings.free_only is False
    assert st.settings.openrouter_codegen_model == "provider/codegen"
    assert st.settings.model_ui == "provider/ui"
    assert r["free_only"] is False
    assert "SKYN3T_FREE_ONLY" not in os.environ
    p = await llm_secrets_payload(st)
    assert p["free_only"] is False


async def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        await set_llm_key(_state(), "bogus", "x", persist=False)


async def test_set_asset_gen_toggles_flag():
    st = _state(llm_backend="stub", replicate_api_token="r8_x")
    assert st.settings.asset_gen is False  # default off
    on = await set_asset_gen(st, True, persist=False)
    assert on["asset_gen"] is True
    assert st.settings.asset_gen is True
    off = await set_asset_gen(st, False, persist=False)
    assert off["asset_gen"] is False
    assert st.settings.asset_gen is False


async def test_asset_gen_surfaced_in_secrets_payload():
    st = _state(llm_backend="stub", replicate_api_token="r8_x")
    await set_asset_gen(st, True, persist=False)
    p = await llm_secrets_payload(st)
    assert p["asset_gen"] is True


async def test_secrets_payload_reports_replicate_presence():
    off = await llm_secrets_payload(_state(llm_backend="stub"))
    assert off["replicate"] is False
    on = await llm_secrets_payload(_state(llm_backend="stub", replicate_api_token="r8_x"))
    assert on["replicate"] is True
    # The token VALUE is never exposed — only presence + the (non-secret) model.
    assert "r8_x" not in str(on)


async def test_set_replicate_token_persist_false(monkeypatch):
    monkeypatch.delenv("SKYN3T_REPLICATE_API_TOKEN", raising=False)
    st = _state(llm_backend="stub")
    r = await set_replicate_token(st, "r8_secret", model="owner/model", persist=False)
    assert r["configured"] is True
    assert r["model"] == "owner/model"
    assert st.settings.replicate_api_token == "r8_secret"
    assert st.settings.replicate_model == "owner/model"


async def test_clear_replicate_token(monkeypatch):
    monkeypatch.setenv("SKYN3T_REPLICATE_API_TOKEN", "r8_x")
    st = _state(llm_backend="stub", replicate_api_token="r8_x")
    r = await set_replicate_token(st, "", persist=False)
    assert r["configured"] is False
    assert st.settings.replicate_api_token == ""
    import os
    assert "SKYN3T_REPLICATE_API_TOKEN" not in os.environ


async def test_set_replicate_empty_model_keeps_existing(monkeypatch):
    monkeypatch.delenv("SKYN3T_REPLICATE_API_TOKEN", raising=False)
    st = _state(llm_backend="stub", replicate_model="owner/keep")
    await set_replicate_token(st, "r8_x", model="", persist=False)
    assert st.settings.replicate_model == "owner/keep"


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
