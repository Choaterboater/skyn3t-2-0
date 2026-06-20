"""Backend selection + CLI degradation for the unified LLM client."""

from __future__ import annotations

from skyn3t.adapters import llm as llm_mod
from skyn3t.adapters.llm import LLMClient, _strip_code_fences
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import Tier


def _client(backend: str, **kw) -> LLMClient:
    return LLMClient(Settings(llm_backend=backend, **kw))


def test_explicit_stub_backend():
    assert _client("stub").backend == "stub"


def test_openrouter_requires_key():
    assert _client("openrouter").backend == "stub"
    assert _client("openrouter", openrouter_api_key="sk-or-test").backend == "openrouter"


def test_auto_prefers_cli_when_available(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert _client("auto").backend == "claude_cli"
    assert _client("kimi_cli").backend == "kimi_cli"


def test_auto_falls_back_to_stub_without_cli(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda b: None)
    assert _client("auto").backend == "stub"
    assert _client("claude_cli").backend == "stub"


async def test_cli_failure_degrades_to_stub(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda b: f"/usr/bin/{b}")

    async def _boom(*_a, **_k):
        raise FileNotFoundError("cli missing")

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _boom)
    result = await _client("claude_cli").complete("hello", tier=Tier.CHEAP)
    assert result.backend == "stub"  # degraded, never raised


def test_strip_code_fences():
    assert _strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fences('{"a": 1}') == '{"a": 1}'


# ---- learned router wiring (gated by model_evolution + auto_route) ---------
from skyn3t.core.model_router import ModelRouter  # noqa: E402
from skyn3t.intelligence.model_tournament import ModelTournament  # noqa: E402
from skyn3t.intelligence.routing_recommendations import (  # noqa: E402
    LearnedModelRouter,
    RoutingRecommender,
)


def test_learned_router_off_by_default():
    c = LLMClient(Settings(model_evolution=False, auto_route=False))
    assert isinstance(c.router, ModelRouter)
    assert not isinstance(c.router, LearnedModelRouter)


def test_learned_router_needs_both_gates():
    assert not isinstance(LLMClient(Settings(model_evolution=True, auto_route=False)).router, LearnedModelRouter)
    assert not isinstance(LLMClient(Settings(model_evolution=False, auto_route=True)).router, LearnedModelRouter)


def test_learned_router_enabled_when_both_gates(tmp_path):
    c = LLMClient(Settings(model_evolution=True, auto_route=True, data_dir=tmp_path))
    assert isinstance(c.router, LearnedModelRouter)
    assert c.router.describe().get("learned") is True


def test_learned_router_prefers_tournament_winner(tmp_path):
    t = ModelTournament(tmp_path / "t.json")
    bucket = t.bucket_key(Tier.STRONG, "")
    # 6 wins -> clears min_plays(5) + min_win_rate(0.55); ":free" passes free_only.
    for _ in range(6):
        t.record_win(bucket, "winner/model:free", ["loser/model:free"])
    router = LearnedModelRouter(RoutingRecommender(t), settings=Settings(free_only=True))
    assert router.resolve(Tier.STRONG) == "winner/model:free"


def test_learned_router_falls_back_without_evidence(tmp_path):
    s = Settings(free_only=True)
    learned = LearnedModelRouter(RoutingRecommender(ModelTournament(tmp_path / "t.json")), settings=s)
    assert learned.resolve(Tier.STRONG) == ModelRouter(s).resolve(Tier.STRONG)


def test_learned_router_respects_free_only(tmp_path):
    t = ModelTournament(tmp_path / "t.json")
    bucket = t.bucket_key(Tier.STRONG, "")
    for _ in range(6):  # a non-free winner must be rejected under free_only
        t.record_win(bucket, "anthropic/claude-3-opus", ["loser/model:free"])
    s = Settings(free_only=True)
    learned = LearnedModelRouter(RoutingRecommender(t), settings=s)
    assert learned.resolve(Tier.STRONG) == ModelRouter(s).resolve(Tier.STRONG)  # fell back, not claude
