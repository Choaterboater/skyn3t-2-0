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
