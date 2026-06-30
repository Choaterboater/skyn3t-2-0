"""Offline tests for the `skyn3t debate` wiring (stub backend, no network)."""

from __future__ import annotations

import asyncio

from skyn3t.cli.main import _run_debate
from skyn3t.config.settings import Settings


def test_debate_enabled_runs_full_debate(tmp_path):
    s = Settings(llm_backend="stub", debate_enabled=True, data_dir=tmp_path)
    res = asyncio.run(_run_debate("Which sort algorithm is best for small lists?", settings=s))
    assert res is not None
    assert res.enabled is True
    assert res.winner is not None
    assert res.synthesis  # non-empty synthesised answer


def test_debate_disabled_is_single_completion(tmp_path):
    s = Settings(llm_backend="stub", debate_enabled=False, data_dir=tmp_path)
    res = asyncio.run(_run_debate("Which sort algorithm is best for small lists?", settings=s))
    assert res is not None
    assert res.enabled is False
    assert res.rounds == 0  # short-circuited to one completion, no debate rounds


def test_a2a_conversation_flag_runs_full_debate(tmp_path):
    s = Settings(
        llm_backend="stub",
        debate_enabled=False,
        a2a_conversation=True,
        data_dir=tmp_path,
    )
    res = asyncio.run(_run_debate("Which sort algorithm is best for small lists?", settings=s))
    assert res is not None
    assert res.enabled is True
    assert res.rounds == 3
