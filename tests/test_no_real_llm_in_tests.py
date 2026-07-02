# tests/test_no_real_llm_in_tests.py
"""Pin the anti-quota-leak fences: no test may reach a real LLM backend by
accident. Two seams are fenced in conftest:

1. `_no_cli_vision` neuters visual_check's CLI fallback (a dev machine with
   `claude` on PATH would otherwise spawn a REAL `claude -p` from any test
   that reaches an actual screenshot+judge — the 2026-07-01 quota leak).
2. `_isolate_data_dir` scrubs the LLM key env vars, so a shell-exported
   SKYN3T_OPENROUTER_API_KEY can't flip LLMClient from "stub" to a real paid
   backend for the whole suite.

If either fence is removed, these tests fail loudly instead of the suite
silently burning quota.
"""
from __future__ import annotations

import os

from skyn3t.config.settings import Settings
from skyn3t.studio import visual_check


def test_llm_key_env_vars_are_scrubbed():
    for var in ("SKYN3T_OPENROUTER_API_KEY", "SKYN3T_ANTHROPIC_API_KEY",
                "SKYN3T_OPENAI_API_KEY", "SKYN3T_KIMI_API_KEY"):
        assert var not in os.environ, f"{var} leaked into the test environment"
    s = Settings()
    assert s.openrouter_api_key == ""
    assert s.has_any_llm is False


def test_llm_key_from_shell_export_does_not_reach_settings(monkeypatch):
    # Even if a fixture ordering change let the var survive to here, Settings
    # must not have read the repo .env (env_file is None during tests).
    assert Settings.model_config.get("env_file") is None


def test_cli_vision_fallback_is_neutered():
    # The autouse fence replaces the builder outright; with no OpenRouter key
    # the combined resolver must yield None (soft-skip), never a claude -p fn.
    from types import SimpleNamespace
    s = SimpleNamespace(openrouter_api_key="", cli_llm_provider="claude",
                        openrouter_vision_model="")
    assert visual_check._make_cli_vision_fn(s) is None
    assert visual_check.make_vision_fn(s) is None
