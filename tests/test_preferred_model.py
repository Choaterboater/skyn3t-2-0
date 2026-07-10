"""GUI model selection: a `preferred_model` pins the OpenRouter model skyn3t uses
(empty = auto / learned-router routing). The dropdown is fed by a live /models
list and sets the pin via /settings/model.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import _FREE_DEFAULTS, Tier, is_free_model_id


async def test_preferred_model_pins_the_model_used():
    c = LLMClient(Settings(llm_backend="stub", free_only=False, preferred_model="openai/gpt-4o"))
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
    c = LLMClient(Settings(llm_backend="stub", free_only=False, preferred_model="openai/gpt-4o"))
    res = await c.complete("hi", model_override="anthropic/claude-sonnet")
    assert res.model == "anthropic/claude-sonnet"


async def test_free_only_blocks_paid_preferred_and_manual_models():
    c = LLMClient(Settings(llm_backend="stub", free_only=True, preferred_model="openai/gpt-4o"))

    preferred = await c.complete("hi", tier=Tier.BACKEND)
    manual = await c.complete("hi", tier=Tier.BACKEND, model_override="anthropic/claude-sonnet")

    assert preferred.model == _FREE_DEFAULTS[Tier.BACKEND]
    assert manual.model == _FREE_DEFAULTS[Tier.BACKEND]


async def test_free_only_allows_free_manual_model():
    c = LLMClient(Settings(llm_backend="stub", free_only=True, preferred_model="openai/gpt-4o"))
    res = await c.complete("hi", tier=Tier.BACKEND, model_override="qwen/manual:free")
    assert res.model == "qwen/manual:free"


async def test_free_only_allows_openrouter_free_router_manual_model():
    c = LLMClient(Settings(llm_backend="stub", free_only=True, preferred_model="openai/gpt-4o"))
    res = await c.complete("hi", tier=Tier.BACKEND, model_override="openrouter/free")
    assert res.model == "openrouter/free"


async def test_list_models_uses_public_endpoint_without_key(monkeypatch):
    from skyn3t.web.routes import list_openrouter_models

    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = SimpleNamespace(settings=SimpleNamespace(openrouter_api_key=""))

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "provider/model-b"}, {"id": "provider/model-a"}]}

    called_headers = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, headers=None):
            called_headers.append(headers or {})
            return _Resp()

    import httpx

    from skyn3t.web import routes
    monkeypatch.setattr(routes, "_MODELS_CACHE", {"ts": 0.0, "models": None, "catalog": None, "note": None})
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    res = await list_openrouter_models(state)

    assert res["models"] == ["provider/model-a", "provider/model-b"]
    assert "items" not in res
    assert res["details_endpoint"] == "/models/catalog"
    assert called_headers
    assert "Authorization" not in (called_headers[0] or {})
    assert res["note"] == "ok"


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


async def test_list_models_falls_back_to_public_if_key_is_rejected(monkeypatch):
    import httpx

    from skyn3t.web import routes

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    routes._MODELS_CACHE.update(ts=0.0, models=None, catalog=None, note=None)

    class _Resp:
        def __init__(self, headers):
            self.headers = headers

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [{"id": "provider/model-free"}, {"id": "provider/model-paid"}]
            }

    calls = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, headers=None):
            calls.append(headers or {})
            if headers.get("Authorization"):
                raise ValueError("invalid key")
            return _Resp(headers)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    state = SimpleNamespace(settings=SimpleNamespace(openrouter_api_key=""))
    res = await routes.list_openrouter_models(state)

    assert res["models"] == ["provider/model-free", "provider/model-paid"]
    assert any(h.get("Authorization") == "Bearer sk-or-env" for h in calls)
    assert any("Authorization" not in (h or {}) for h in calls)


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


async def test_resolve_openrouter_model_reports_known(monkeypatch):
    import skyn3t.web.routes as routes

    async def fake_list_models(_state):
        return {"models": ["provider/known-model", "openai/gpt-4o-mini"], "note": "ok"}

    monkeypatch.setattr(routes, "list_openrouter_models", fake_list_models)
    state = SimpleNamespace(settings=SimpleNamespace(openrouter_api_key="sk-or-x"))
    out = await routes.resolve_openrouter_model(state, "provider/known-model")
    assert out["model"] == "provider/known-model"
    assert out["available"] is True
    assert out["status"] == "known"


async def test_resolve_openrouter_model_reports_unknown_with_suggestions(monkeypatch):
    import skyn3t.web.routes as routes

    async def fake_list_models(_state):
        return {"models": ["provider/gpt-4o-mini", "provider/another"], "note": "ok"}

    monkeypatch.setattr(routes, "list_openrouter_models", fake_list_models)
    state = SimpleNamespace(settings=SimpleNamespace(openrouter_api_key="sk-or-x"))
    out = await routes.resolve_openrouter_model(state, "provider/gpt")
    assert out["available"] is False
    assert out["status"] == "unknown"
    assert out["model"] == "provider/gpt"
    assert "provider/gpt-4o-mini" in out["suggestions"]


async def test_model_routing_preview_reports_tier_models_and_pricing(monkeypatch):
    import skyn3t.web.routes as routes

    async def fake_catalog(_state):
        return (
            [
                {
                    "id": "openrouter/primary-fast",
                    "pricing": {
                        "prompt": 0.0000004,
                        "completion": 0.000002,
                    },
                },
                {"id": "openrouter/manual", "pricing": {"prompt": 0.000001}},
            ],
            "ok",
        )

    class _Router:
        def resolve(self, tier):
            return {
                "cheap": "openrouter/primary-fast",
                "ui": "openrouter/primary-fast",
                "backend": "openrouter/manual",
                "strong": "openrouter/manual",
                "docs": "openrouter/manual",
            }[tier.value]

    monkeypatch.setattr(routes, "_load_openrouter_catalog", fake_catalog)
    state = SimpleNamespace(
        settings=SimpleNamespace(openrouter_api_key="sk-or-testing", free_only=False),
        llm_client=SimpleNamespace(backend="openrouter"),
        router=_Router(),
    )

    out = await routes.model_routing_preview_payload(state, build_profile="balanced")

    assert out["build_profile"] == "balanced"
    assert out["backend"] == "openrouter"
    assert out["model_override"] == ""
    assert len(out["tiers"]) == 5
    assert out["tiers"][0]["tier"] == "cheap"
    assert out["tiers"][0]["model"] == "openrouter/primary-fast"
    assert out["tiers"][0]["pricing_raw"]["prompt"] == 0.0000004
    assert isinstance(out["tiers"][0]["cost_estimates_usd"]["prompt_1k_completion_1k"], float)
    assert out["tiers"][1]["pricing"].startswith("prompt:")
    assert out["catalog_note"] == "ok"
    assert out["catalog_model_count"] == 2
    assert out["resolution_kind"] == "estimate"
    assert out["authoritative"] is False
    assert all(entry["resolution_kind"] == "estimate" for entry in out["tiers"])
    assert "may differ" in out["estimate_reason"]
    # Missing completion pricing is unknown, never silently estimated as free.
    backend = next(entry for entry in out["tiers"] if entry["tier"] == "backend")
    assert backend["cost_estimates_usd"]["prompt_1k_completion_1k"] is None


async def test_model_routing_preview_uses_manual_override_for_every_tier(monkeypatch):
    import skyn3t.web.routes as routes

    async def fake_catalog(_state):
        return ([{"id": "manual/test", "pricing": {"prompt": 0.5}}], "ok")

    class _Router:
        def resolve(self, tier):
            raise AssertionError("router should be bypassed when override is provided")

    monkeypatch.setattr(routes, "_load_openrouter_catalog", fake_catalog)
    state = SimpleNamespace(
        settings=SimpleNamespace(openrouter_api_key="sk-or-testing", free_only=False),
        llm_client=SimpleNamespace(backend="openrouter"),
        router=_Router(),
    )

    out = await routes.model_routing_preview_payload(
        state,
        build_profile="best_quality",
        model_override="  manual/test  ",
    )

    assert out["model_override"] == "manual/test"
    assert all(entry["model"] == "manual/test" for entry in out["tiers"])
    assert all(entry["source"] == "manual" for entry in out["tiers"])
    assert all(entry["pricing"] == "prompt:$500000/M" for entry in out["tiers"])
    assert all("pricing_raw" in entry for entry in out["tiers"])


async def test_model_routing_preview_free_only_blocks_paid_manual_override(monkeypatch):
    import skyn3t.web.routes as routes

    async def fake_catalog(_state):
        return (
            [
                {"id": "manual/paid", "pricing": {"prompt": 0.5}},
                {"id": _FREE_DEFAULTS[Tier.BACKEND], "pricing": {"prompt": 0.0}},
            ],
            "ok",
        )

    class _Router:
        def resolve(self, tier, profile="balanced"):
            return _FREE_DEFAULTS[tier]

    monkeypatch.setattr(routes, "_load_openrouter_catalog", fake_catalog)
    state = SimpleNamespace(
        settings=SimpleNamespace(openrouter_api_key="sk-or-testing", free_only=True),
        llm_client=SimpleNamespace(backend="openrouter"),
        router=_Router(),
    )

    out = await routes.model_routing_preview_payload(
        state,
        build_profile="best_quality",
        model_override="manual/paid",
    )

    assert out["model_override"] == "manual/paid"
    assert out["free_only"] is True
    assert all(is_free_model_id(entry["model"]) for entry in out["tiers"])
    assert all(entry["source"] == "free_only" for entry in out["tiers"])


async def test_model_routing_preview_shows_preferred_model(monkeypatch):
    import skyn3t.web.routes as routes

    async def fake_catalog(_state):
        return ([{"id": "deepseek/deepseek-v3", "pricing": {"prompt": 0.5}}], "ok")

    class _Router:
        def resolve(self, tier):
            raise AssertionError("router should be bypassed when preferred is set")

    monkeypatch.setattr(routes, "_load_openrouter_catalog", fake_catalog)
    state = SimpleNamespace(
        settings=SimpleNamespace(
            openrouter_api_key="sk-or-testing",
            preferred_model="preferred/custom-model",
            free_only=False,
        ),
        llm_client=SimpleNamespace(backend="openrouter"),
        router=_Router(),
    )

    out = await routes.model_routing_preview_payload(
        state,
        build_profile="balanced",
    )

    assert out["model_override"] == ""
    assert out["preferred_model"] == "preferred/custom-model"
    assert all(entry["model"] == "preferred/custom-model" for entry in out["tiers"])
    assert all(entry["source"] == "preferred" for entry in out["tiers"])


async def test_model_catalog_filters_and_sorts(monkeypatch):
    import skyn3t.web.routes as routes

    async def fake_catalog(_state):
        return (
            [
                {
                    "id": "openai/gpt-4o-mini",
                    "pricing": {"prompt": 0.0000003, "completion": 0.0000012},
                },
                {
                    "id": "qwen/qwen3-coder",
                    "pricing": {"prompt": 0.0000008, "completion": 0.0000016},
                },
                {
                    "id": "openrouter/deepseek-v3:free",
                    "pricing": {"prompt": 0.0, "completion": 0.0},
                },
                {
                    "id": "openrouter/free",
                    "pricing": {"prompt": 0.0, "completion": 0.0},
                },
            ],
            "ok",
        )

    monkeypatch.setattr(routes, "_load_openrouter_catalog", fake_catalog)
    state = SimpleNamespace(settings=SimpleNamespace(openrouter_api_key="sk-or-testing"))

    out_qwen = await routes.list_openrouter_model_catalog(
        state,
        query="qwen",
        sort="price",
        order="asc",
        limit=10,
    )
    assert out_qwen["count"] == 1
    assert out_qwen["items"][0]["id"] == "qwen/qwen3-coder"

    out_provider = await routes.list_openrouter_model_catalog(
        state,
        provider="openai",
        limit=10,
    )
    assert out_provider["count"] == 1
    assert out_provider["items"][0]["provider"] == "openai"
    assert out_provider["items"][0]["is_free"] is False

    out_free = await routes.list_openrouter_model_catalog(
        state,
        only_free=True,
        limit=10,
    )
    assert out_free["count"] == 2
    assert {item["id"] for item in out_free["items"]} == {"openrouter/deepseek-v3:free", "openrouter/free"}
    assert all(item["is_free"] is True for item in out_free["items"])
    assert all(item["pricing_summary"] == "free" for item in out_free["items"])
