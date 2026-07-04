"""GUI model selection: a `preferred_model` pins the OpenRouter model skyn3t uses
(empty = auto / learned-router routing). The dropdown is fed by a live /models
list and sets the pin via /settings/model.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings


async def test_preferred_model_pins_the_model_used():
    c = LLMClient(Settings(llm_backend="stub", preferred_model="openai/gpt-4o"))
    res = await c.complete("hi")
    assert res.model == "openai/gpt-4o"


async def test_empty_preferred_model_falls_back_to_the_router():
    # default preferred_model="" -> the router picks (auto), not a crash / not empty
    c = LLMClient(Settings(llm_backend="stub"))
    res = await c.complete("hi")
    assert res.model  # router resolved *something*
    assert res.model != ""


async def test_explicit_override_still_beats_the_pin():
    # a per-call model_override (e.g. best-of-N sampling) wins over the GUI pin
    c = LLMClient(Settings(llm_backend="stub", preferred_model="openai/gpt-4o"))
    res = await c.complete("hi", model_override="anthropic/claude-sonnet")
    assert res.model == "anthropic/claude-sonnet"


async def test_list_models_needs_a_key(monkeypatch):
    from skyn3t.web.routes import list_openrouter_models

    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = SimpleNamespace(settings=SimpleNamespace(openrouter_api_key=""))
    res = await list_openrouter_models(state)
    assert res["models"] == []
    assert "note" in res


async def test_list_models_uses_plain_openrouter_env_key(monkeypatch):
    import httpx

    from skyn3t.web import routes

    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    routes._MODELS_CACHE.update(ts=0.0, models=None)
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "provider/model-b"}, {"id": "provider/model-a"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, headers=None):
            seen["authorization"] = (headers or {}).get("Authorization")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    state = SimpleNamespace(settings=SimpleNamespace(openrouter_api_key=""))

    res = await routes.list_openrouter_models(state)

    assert res["models"] == ["provider/model-a", "provider/model-b"]
    assert seen["authorization"] == "Bearer sk-or-env"


async def test_set_preferred_model_updates_settings(monkeypatch):
    import skyn3t.web.routes as routes

    monkeypatch.setattr(routes, "_persist_env_var", lambda *a, **k: None)
    monkeypatch.setenv("SKYN3T_PREFERRED_MODEL", "")  # restore point for teardown
    state = SimpleNamespace(settings=Settings())
    res = await routes.set_preferred_model(state, "  deepseek/deepseek-chat  ")
    assert res["preferred_model"] == "deepseek/deepseek-chat"  # trimmed
    assert state.settings.preferred_model == "deepseek/deepseek-chat"
    # empty clears the pin (back to auto)
    res2 = await routes.set_preferred_model(state, "")
    assert res2["preferred_model"] == ""


async def test_set_preferred_model_compacts_whitespace(monkeypatch):
    import skyn3t.web.routes as routes

    monkeypatch.setattr(routes, "_persist_env_var", lambda *a, **k: None)
    state = SimpleNamespace(settings=Settings())
    res = await routes.set_preferred_model(
        state,
        " z z \n openrouter/ gpt 4o-mini ",
    )
    assert res["preferred_model"] == "zzopenrouter/gpt4o-mini"


async def test_set_preferred_model_trims_overly_long_payload(monkeypatch):
    import skyn3t.web.routes as routes

    monkeypatch.setattr(routes, "_persist_env_var", lambda *a, **k: None)
    state = SimpleNamespace(settings=Settings())
    long_model = "x" * 300
    res = await routes.set_preferred_model(state, long_model)
    assert res["preferred_model"] == ""
