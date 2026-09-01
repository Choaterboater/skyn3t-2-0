"""Event-loop hot-path regressions for the LLM adapter.

Two findings share one theme — per-completion work that used to run ON the
event-loop thread:

* Budget ledger transactions (cross-process lock file + JSON read/rewrite,
  worst case ``_LEDGER_LOCK_TIMEOUT_S`` under contention) ran three times per
  completion, synchronously. ``complete()`` now runs the pre-dispatch check and
  a FUSED post-record transaction (``BudgetTracker.record_and_check``) via
  ``asyncio.to_thread``, and failed-attempt accounting is off-loop too.
* ``_openrouter`` built a fresh ``httpx.AsyncClient`` (new SSLContext +
  TCP+TLS handshake) for every attempt. It now reuses one keep-alive client
  per running event loop, passing the per-model reasoning-floor timeout as a
  per-request override.

All hermetic: scripted HTTP fakes, tmp_path ledgers, no network.
"""
from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

import skyn3t.adapters.llm as llm
from skyn3t.adapters.llm import BudgetExceeded, BudgetTracker, LLMClient, LLMResult
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import Tier


def _result(cost: float, tokens: int = 10) -> LLMResult:
    return LLMResult(
        text="ok", model="m", backend="openrouter",
        prompt_tokens=tokens, completion_tokens=0, cost_usd=cost,
    )


def _http_error(status: int, text: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", llm.OPENROUTER_URL)
    resp = httpx.Response(status, request=req, text=text)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


# ---------------------------------------------------------------------------
# Fused ledger transaction.
# ---------------------------------------------------------------------------
def test_record_and_check_is_one_ledger_transaction(tmp_path, monkeypatch):
    ledger = tmp_path / "budget" / "daily_usage.json"
    tracker = BudgetTracker(
        per_build_cap=0, daily_cap=0.5, token_cap=0, ledger_path=ledger
    )  # __post_init__ takes the guard once — start counting only from here
    real_lock = llm._exclusive_ledger_lock
    entries = {"n": 0}

    def counting_lock(path):
        entries["n"] += 1
        return real_lock(path)

    monkeypatch.setattr(llm, "_exclusive_ledger_lock", counting_lock)

    tracker.record_and_check(_result(0.2))
    assert entries["n"] == 1  # record() then check() used to pay two

    with pytest.raises(BudgetExceeded, match="daily cap"):
        tracker.record_and_check(_result(0.4))
    assert entries["n"] == 2
    # The over-cap spend is still persisted (recorded first, then checked) —
    # byte-identical semantics to the old record()-then-check() pair.
    restored = BudgetTracker(
        per_build_cap=0, daily_cap=0.5, token_cap=0, ledger_path=ledger
    )
    assert round(restored.spent_day, 4) == 0.6


def test_record_and_check_matches_record_then_check_when_under_cap(tmp_path):
    fused = BudgetTracker(
        per_build_cap=0, daily_cap=1.0, token_cap=0,
        ledger_path=tmp_path / "fused" / "daily_usage.json",
    )
    paired = BudgetTracker(
        per_build_cap=0, daily_cap=1.0, token_cap=0,
        ledger_path=tmp_path / "paired" / "daily_usage.json",
    )

    fused.record_and_check(_result(0.3, tokens=25))
    paired.record(_result(0.3, tokens=25))
    paired.check()

    assert round(fused.spent_day, 4) == round(paired.spent_day, 4) == 0.3
    assert fused.tokens_day == paired.tokens_day == 25
    assert len(fused.calls) == len(paired.calls) == 1


# ---------------------------------------------------------------------------
# Budget transactions leave the event-loop thread.
# ---------------------------------------------------------------------------
async def test_complete_budget_transactions_run_off_the_event_loop(tmp_path):
    client = LLMClient(Settings(llm_backend="stub", data_dir=tmp_path))
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real_check = client.budget.check
    real_record_and_check = client.budget.record_and_check

    def check_spy() -> None:
        seen["check"] = threading.get_ident()
        real_check()

    def record_and_check_spy(r) -> None:
        seen["record_and_check"] = threading.get_ident()
        real_record_and_check(r)

    client.budget.check = check_spy
    client.budget.record_and_check = record_and_check_spy

    result = await client.complete("hi")

    assert result.text
    assert seen["check"] != loop_thread
    assert seen["record_and_check"] != loop_thread


async def test_failed_attempt_accounting_runs_off_the_event_loop(tmp_path):
    client = LLMClient(Settings(llm_backend="stub", data_dir=tmp_path))
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def spy(model, exc, *, prompt_tokens, max_completion_tokens):
        seen.append(threading.get_ident())

    client._record_failed_openrouter_attempt = spy

    async def always_fatal(model):
        raise _http_error(401)

    with pytest.raises(httpx.HTTPStatusError):
        await client._resilient_call("some/model", Tier.CHEAP, always_fatal)

    assert seen
    assert all(ident != loop_thread for ident in seen)


# ---------------------------------------------------------------------------
# Shared per-loop OpenRouter client.
# ---------------------------------------------------------------------------
def test_openrouter_reuses_one_client_per_loop_with_per_request_timeout(monkeypatch):
    llm.reset_openrouter_clients()
    constructed = {"n": 0}
    posts: list[dict] = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    class _Client:
        def __init__(self, *a, **k):
            constructed["n"] += 1

        async def post(self, url, json=None, headers=None, timeout=None):
            posts.append({"model": (json or {}).get("model"), "timeout": timeout})
            return _Resp()

    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    client = LLMClient(Settings(
        llm_backend="openrouter", openrouter_api_key="sk-or-test",
    ))

    async def two_calls():
        await client._openrouter("openai/gpt-4o-mini", "one", "", 64, False)
        await client._openrouter("openai/o3", "two", "", 64, False)

    try:
        asyncio.run(two_calls())
        # One construction (one TCP+TLS handshake) per loop — not per attempt.
        assert constructed["n"] == 1
        assert [p["model"] for p in posts] == ["openai/gpt-4o-mini", "openai/o3"]
        # The reasoning floor still applies per request on the shared client.
        assert [p["timeout"] for p in posts] == [120, 600]

        # A NEW loop gets a NEW client, and the dead loop's entry is evicted.
        asyncio.run(two_calls())
        assert constructed["n"] == 2
        assert len(llm._OPENROUTER_CLIENTS) == 1
    finally:
        llm.reset_openrouter_clients()
