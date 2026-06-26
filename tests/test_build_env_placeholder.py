"""The node build env seeds placeholder provider keys so a generated app that
instantiates an LLM SDK client at module top-level doesn't crash `next build` on a
missing key (the real key is only needed at runtime/serve)."""
from __future__ import annotations

from skyn3t.studio.proof_run import _node_build_env


def test_build_env_seeds_placeholder_keys(monkeypatch):
    for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    env = _node_build_env()
    assert env["OPENAI_API_KEY"] == "sk-build-placeholder"
    assert env["OPENROUTER_API_KEY"] == "sk-build-placeholder"
    assert env["CI"] == "1"


def test_build_env_preserves_real_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-real")
    env = _node_build_env()
    assert env["OPENROUTER_API_KEY"] == "sk-or-real"          # real key kept
    assert env["OPENAI_API_KEY"] == "sk-build-placeholder"    # missing one seeded
