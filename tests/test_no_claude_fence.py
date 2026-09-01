"""no_claude is a hard fence, not a ranking tweak.

With Claude unchecked (not paid for), NOTHING resolves to it: not an explicit
claude_cli backend, not a provider_override, not a custom chain entry, not a
codegen pin. Opt-in (no_claude=False) restores every path.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.adapters.llm import LLMClient, explicit_routing_lock_error
from skyn3t.config.settings import Settings


def test_explicit_claude_cli_backend_degrades_to_stub_when_no_claude():
    client = LLMClient(Settings(no_claude=True, llm_backend="claude_cli"))
    assert client.backend == "stub"


def test_explicit_claude_cli_backend_works_when_opted_in(monkeypatch):
    client = LLMClient(Settings(no_claude=False, llm_backend="claude_cli"))
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {}, raising=False)
    client._cli_available = lambda _p: True
    assert client.backend == "claude_cli"


def test_provider_override_to_claude_is_fenced_too():
    client = LLMClient(Settings(no_claude=True, llm_backend="codex_cli"))
    assert client._effective_backend("claude_cli") == "stub"


def test_auto_chain_skips_claude_even_when_operator_left_it_in(monkeypatch):
    client = LLMClient(Settings(
        no_claude=True, llm_backend="auto", auto_cli_priority="codex,claude,kimi",
    ))
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {}, raising=False)
    client._cli_available = lambda p: p == "claude"
    assert client._auto_cli_provider() == ""
    # opting back in restores the chain
    client.settings.no_claude = False
    assert client._auto_cli_provider() == "claude"


def test_codegen_lock_names_no_claude_for_explicit_claude_pin():
    err = explicit_routing_lock_error(
        SimpleNamespace(
            llm_backend="auto", codegen_cli_provider="claude", no_claude=True,
            openrouter_api_key="", auto_allow_openrouter=False,
        ),
        cli_available=lambda _p: True,
        require_codex_for_auto=False,
    )
    assert "no_claude" in err


def test_shipped_default_is_unchecked():
    assert Settings().no_claude is True
