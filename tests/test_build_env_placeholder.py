"""The node build env seeds placeholder provider keys so a generated app that
instantiates an LLM SDK client at module top-level doesn't crash `next build`.
Real host keys are not passed into generated build subprocesses."""
from __future__ import annotations

from skyn3t.studio.proof_run import _node_build_env


def test_build_env_seeds_placeholder_keys(monkeypatch):
    for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    env = _node_build_env()
    assert env["OPENAI_API_KEY"] == "sk-build-placeholder"
    assert env["OPENROUTER_API_KEY"] == "sk-build-placeholder"
    assert env["CI"] == "1"


def test_build_env_scrubs_real_host_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-real")
    env = _node_build_env()
    assert env["OPENROUTER_API_KEY"] == "sk-build-placeholder"
    assert env["OPENAI_API_KEY"] == "sk-build-placeholder"
