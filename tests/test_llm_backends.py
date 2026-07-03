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


def test_openrouter_plain_env_key_is_routable(monkeypatch):
    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    assert _client("openrouter").backend == "openrouter"
    status = _client("auto").backend_status()
    assert status["active"] == "openrouter"
    assert status["openrouter_configured"] is True


def test_explicit_openrouter_missing_key_is_reported():
    c = _client("openrouter")
    status = c.backend_status()
    assert status["active"] == "stub"
    assert status["requested"] == "openrouter"
    assert status["state"] == "missing_key"
    assert "OPENROUTER_API_KEY" in status["reason"]


def test_openrouter_codegen_model_pin_used_for_agentic(monkeypatch, tmp_path):
    c = _client(
        "openrouter",
        openrouter_api_key="sk-or-test",
        openrouter_codegen_model="provider/custom-code-model",
    )
    captured = {}

    async def _fake(prompt, workdir, model, timeout=None, stack=""):
        captured["model"] = model
        return {"ok": True, "backend": "openrouter"}

    monkeypatch.setattr(c, "_openrouter_agentic", _fake)
    import asyncio

    asyncio.run(c.agentic_build("build", str(tmp_path), stack="react_vite"))
    assert captured["model"] == "provider/custom-code-model"


def test_preferred_model_used_for_openrouter_agentic_codegen(monkeypatch, tmp_path):
    c = _client(
        "openrouter",
        openrouter_api_key="sk-or-test",
        preferred_model="provider/selected-in-ui",
    )
    captured = {}

    async def _fake(prompt, workdir, model, timeout=None, stack=""):
        captured["model"] = model
        return {"ok": True, "backend": "openrouter"}

    monkeypatch.setattr(c, "_openrouter_agentic", _fake)
    import asyncio

    asyncio.run(c.agentic_build("build", str(tmp_path), stack="react_vite"))
    assert captured["model"] == "provider/selected-in-ui"


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


def _install_fake_cli(monkeypatch, captured):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda b: f"/usr/bin/{b}")

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b'{"ok": true}', b"")

    async def _fake_exec(*argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _fake_exec)


async def test_cli_references_image_file_path(monkeypatch):
    # build-from-image on the CLI backend: the saved image PATH is referenced in
    # the prompt so `claude -p` reads the file (the proven make_vision_fn pattern).
    import os
    import tempfile

    captured: dict = {}
    _install_fake_cli(monkeypatch, captured)
    fd, path = tempfile.mkstemp(suffix=".png")
    os.write(fd, b"\x89PNG\r\n")
    os.close(fd)
    res = await _client("claude_cli").complete("design it", tier=Tier.UI, images=[path])
    assert res.backend == "claude_cli"
    full = captured["argv"][-1]
    assert path in full and "image file" in full.lower()


async def test_cli_writes_data_url_to_temp_file(monkeypatch):
    # A data: URL can't be passed to the CLI inline — it's written to a temp file
    # and that path is referenced instead.
    import base64

    captured: dict = {}
    _install_fake_cli(monkeypatch, captured)
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n").decode()
    await _client("claude_cli").complete("design", tier=Tier.UI, images=[data_url])
    full = captured["argv"][-1]
    assert "data:" not in full
    assert ".png" in full and "image file" in full.lower()


def test_supports_image_input():
    assert _client("openrouter", openrouter_api_key="sk").supports_image_input is True
    assert _client("stub").supports_image_input is False


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


def test_runtime_model_pin_overrides_router_default():
    r = ModelRouter(Settings(free_only=False, model_backend="provider/backend-pin"))
    assert r.resolve(Tier.BACKEND) == "provider/backend-pin"


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
