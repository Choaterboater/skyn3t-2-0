"""LLM-call resilience: model failover, transient retry, and agentic context editing.

Three middlewares over the OpenRouter path (research 2026-07-01 deep-dive item 2:
langchain ModelFallbackMiddleware + retry-with-backoff + ClearToolUsesEdit):

* MODEL FAILOVER — a retired/invalid model (404 / "not a valid model" 400) falls
  over through an ordered per-tier candidate list instead of killing the build
  (the exact `deepseek-*:free` 404 incident that silently degraded every build).
* RETRY — bounded exponential backoff + jitter for transient classes (429/5xx/
  timeout/connection) BEFORE failover; genuine bad requests / auth fail fast.
* CONTEXT EDITING — in the agentic tool-loop, OLD tool-result file dumps are
  stubbed on a COPY of the history when it blows a byte budget, keeping the
  system prompt, the user goal, and the most recent K tool results intact.

All hermetic: the OpenRouter HTTP client is a scripted fake and the router's
live-catalog lookups are neutered so nothing touches the network.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import skyn3t.adapters.llm as llm
import skyn3t.core.model_router as mr
from skyn3t.adapters.llm import LLMClient, classify_llm_error
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import ModelRouter, Tier


# ---------------------------------------------------------------------------
# Test harness: a scripted OpenRouter HTTP client + httpx error builders.
# ---------------------------------------------------------------------------
def _http_error(status: int, text: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", llm.OPENROUTER_URL)
    resp = httpx.Response(status, request=req, text=text)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


class _Resp:
    """A fake httpx.Response: raise_for_status() raises for >=400, json() replays."""

    def __init__(self, *, status: int = 200, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _http_error(self.status_code, self.text)

    def json(self):
        return self._payload


def _install_scripted(monkeypatch, script: list) -> dict:
    """Replay ``script`` across POSTs (shared across every client instance).

    Each item is a ``_Resp`` (returned) or an ``Exception`` (raised from post,
    e.g. a transport error). Returns a live counter dict {"calls": int}."""
    state = {"i": 0, "calls": 0}
    seq = list(script)

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            state["calls"] += 1
            item = seq[state["i"]]
            state["i"] += 1
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _C())
    return state


@pytest.fixture
def no_router_network(monkeypatch):
    """Neuter the router's live-catalog lookups so fallback resolution is offline
    and deterministic (falls back to the static per-tier defaults)."""
    monkeypatch.setattr(mr, "live_free_model_ids", lambda *a, **k: [])
    monkeypatch.setattr(mr, "live_catalog", lambda *a, **k: [])
    monkeypatch.setattr(mr, "newest_paid_model", lambda *a, **k: None)


def _tool_turn(name, args, tcid="t1"):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": tcid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


# ---------------------------------------------------------------------------
# 1. Pure classifier.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504])
def test_classify_transient_statuses(status):
    assert classify_llm_error(_http_error(status)) == "transient"


def test_classify_model_status_404():
    assert classify_llm_error(_http_error(404, "model not found")) == "model"


def test_classify_400_with_model_marker_is_model():
    assert classify_llm_error(_http_error(400, "xxx is not a valid model id")) == "model"
    assert classify_llm_error(_http_error(400, "no endpoints found for model")) == "model"


def test_classify_400_generic_is_fatal():
    # A genuine bad request on a valid model — retry/failover won't help.
    assert classify_llm_error(_http_error(400, "temperature must be <= 2")) == "fatal"


@pytest.mark.parametrize("status", [401, 403, 422])
def test_classify_auth_and_other_4xx_fatal(status):
    assert classify_llm_error(_http_error(status)) == "fatal"


def test_classify_transport_errors_transient():
    req = httpx.Request("POST", llm.OPENROUTER_URL)
    assert classify_llm_error(httpx.ConnectError("refused", request=req)) == "transient"
    assert classify_llm_error(httpx.ReadTimeout("slow", request=req)) == "transient"


def test_classify_opaque_error_transient():
    # An unknown, non-HTTP failure (flaky gateway returning junk) is retried; the
    # retry budget caps the blast radius.
    assert classify_llm_error(ValueError("junk body")) == "transient"


# ---------------------------------------------------------------------------
# 2. Router fallback candidate list.
# ---------------------------------------------------------------------------
def test_fallback_candidates_free_offline(no_router_network):
    r = ModelRouter(Settings(free_only=True))
    cands = r.fallback_candidates(Tier.CHEAP, primary="qwen/qwen3-coder:free")
    assert cands  # at least one alternative to the dead primary
    assert "qwen/qwen3-coder:free" not in cands  # primary removed
    assert all(m.endswith(":free") for m in cands)  # free_only honored


def test_fallback_candidates_dedupes_and_drops_primary(no_router_network):
    r = ModelRouter(Settings(free_only=True))
    cands = r.fallback_candidates(Tier.STRONG, primary="qwen/qwen3-next-80b-a3b-instruct:free")
    assert len(cands) == len(set(cands))
    assert "qwen/qwen3-next-80b-a3b-instruct:free" not in cands


# ---------------------------------------------------------------------------
# 3. Completion path: retry + failover wired through _openrouter.
# ---------------------------------------------------------------------------
def _or_client(**kw) -> LLMClient:
    base = dict(llm_backend="openrouter", openrouter_api_key="x", llm_retry_base_delay=0.0)
    base.update(kw)
    return LLMClient(Settings(**base))


def test_complete_falls_over_on_retired_model(monkeypatch, no_router_network):
    state = _install_scripted(monkeypatch, [
        _Resp(status=404, text="model not found"),                       # primary retired
        _Resp(status=200, payload={"choices": [{"message": {"content": "built"}}],
                                   "usage": {"prompt_tokens": 2, "completion_tokens": 2}}),
    ])
    c = _or_client(llm_max_retries=0)
    res = asyncio.run(c._openrouter("qwen/qwen3-coder:free", "p", "", 100, False, tier=Tier.CHEAP))
    assert res.text == "built"
    assert state["calls"] == 2  # primary failed -> fallback succeeded


def test_transient_429_retries_then_succeeds_no_failover(monkeypatch, no_router_network):
    state = _install_scripted(monkeypatch, [
        _Resp(status=429),
        _Resp(status=200, payload={"choices": [{"message": {"content": "ok2"}}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
    ])
    c = _or_client(llm_max_retries=2)
    res = asyncio.run(c._openrouter("m/primary", "p", "", 100, False))
    assert res.text == "ok2"
    assert state["calls"] == 2  # retried the SAME model; no failover


def test_non_transient_400_fails_fast(monkeypatch, no_router_network):
    state = _install_scripted(monkeypatch, [_Resp(status=400, text="temperature must be <= 2")])
    c = _or_client(llm_max_retries=3)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(c._openrouter("m/primary", "p", "", 100, False))
    assert ei.value.response.status_code == 400
    assert state["calls"] == 1  # no retry, no failover


def test_fallback_exhaustion_surfaces_original_error(monkeypatch, no_router_network):
    # primary 404 (model error) -> the one offline fallback 503s -> the ORIGINAL
    # 404 surfaces (not the fallback's 503).
    state = _install_scripted(monkeypatch, [
        _Resp(status=404, text="retired"),
        _Resp(status=503),
    ])
    c = _or_client(llm_max_retries=0)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(c._openrouter("qwen/qwen3-coder:free", "p", "", 100, False, tier=Tier.CHEAP))
    assert ei.value.response.status_code == 404
    assert state["calls"] == 2


def test_resilience_flags_off_is_old_behavior(monkeypatch, no_router_network):
    # retry + fallback disabled -> exactly one call, the raw error propagates.
    state = _install_scripted(monkeypatch, [_Resp(status=500)])
    c = _or_client(llm_retry_enabled=False, llm_fallback_enabled=False)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(c._openrouter("m/primary", "p", "", 100, False))
    assert ei.value.response.status_code == 500
    assert state["calls"] == 1


def test_fatal_error_short_circuits_before_fallback(monkeypatch, no_router_network):
    # A 401 must fail fast even with fallback enabled — a different model won't fix auth.
    state = _install_scripted(monkeypatch, [_Resp(status=401)])
    c = _or_client()
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(c._openrouter("m/primary", "p", "", 100, False))
    assert ei.value.response.status_code == 401
    assert state["calls"] == 1


# ---------------------------------------------------------------------------
# 4. Agentic context editing (research item 4).
# ---------------------------------------------------------------------------
def _agentic_history(n_pairs: int, dump_bytes: int) -> list[dict]:
    msgs = [{"role": "system", "content": "SYSTEM PROMPT"},
            {"role": "user", "content": "BUILD GOAL"}]
    for i in range(n_pairs):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"t{i}", "type": "function",
                                     "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "D" * dump_bytes})
    return msgs


def test_context_editing_preserves_system_goal_and_last_k():
    c = LLMClient(Settings(agentic_context_budget_bytes=2000, agentic_context_keep_last=2))
    msgs = _agentic_history(10, 500)
    edited = c._edit_context(msgs)
    assert edited is not msgs
    assert edited[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert edited[1] == {"role": "user", "content": "BUILD GOAL"}
    tool_idx = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
    for i in tool_idx[-2:]:  # most recent K tool results intact
        assert edited[i]["content"] == "D" * 500
    assert "elided" in edited[tool_idx[0]]["content"]  # oldest dump stubbed


def test_context_editing_shrinks_total_bytes():
    c = LLMClient(Settings(agentic_context_budget_bytes=2000, agentic_context_keep_last=2))
    msgs = _agentic_history(10, 500)
    before = sum(c._msg_bytes(m) for m in msgs)
    edited = c._edit_context(msgs)
    after = sum(c._msg_bytes(m) for m in edited)
    # 8 of 10 dumps (all but the protected last-K) collapse from 500 bytes to a
    # ~40-byte stub, so the history shrinks by at least ~400 bytes each.
    assert before - after >= 8 * 400


def test_context_editing_does_not_mutate_canonical():
    c = LLMClient(Settings(agentic_context_budget_bytes=2000, agentic_context_keep_last=2))
    msgs = _agentic_history(10, 500)
    snapshot = json.dumps(msgs)
    c._edit_context(msgs)
    assert json.dumps(msgs) == snapshot  # caller's history untouched


def test_context_editing_off_returns_same_object():
    c = LLMClient(Settings(agentic_context_editing=False))
    msgs = _agentic_history(10, 500)
    assert c._edit_context(msgs) is msgs


def test_context_editing_under_budget_is_noop():
    c = LLMClient(Settings(agentic_context_budget_bytes=10_000_000))
    msgs = _agentic_history(3, 100)
    assert c._edit_context(msgs) is msgs


# ---------------------------------------------------------------------------
# 5. Agentic loop wiring: failover + context editing.
# ---------------------------------------------------------------------------
def test_agentic_survives_retired_model(monkeypatch, no_router_network, tmp_path):
    script = [
        _Resp(status=404, text="model not found"),                       # primary retired
        _Resp(status=200, payload=_tool_turn("write_file", {"path": "a.txt", "content": "hi"})),
        _Resp(status=200, payload=_tool_turn("finish", {}, "t2")),
    ]
    _install_scripted(monkeypatch, script)
    c = _or_client(llm_max_retries=0, agentic_verify_on_stop=False)
    res = asyncio.run(c._openrouter_agentic("build", str(tmp_path), "qwen/qwen3-coder:free",
                                            stack="phaser"))
    assert res["ok"] is True
    assert (tmp_path / "a.txt").read_text() == "hi"


def test_agentic_consults_context_editing_each_turn(monkeypatch, tmp_path):
    calls: list[int] = []
    orig = LLMClient._edit_context

    def _spy(self, messages):
        calls.append(len(messages))
        return orig(self, messages)

    monkeypatch.setattr(LLMClient, "_edit_context", _spy)
    script = [
        _Resp(status=200, payload=_tool_turn("write_file", {"path": "a.txt", "content": "hi"})),
        _Resp(status=200, payload=_tool_turn("finish", {}, "t2")),
    ]
    _install_scripted(monkeypatch, script)
    c = _or_client(agentic_verify_on_stop=False)
    res = asyncio.run(c._openrouter_agentic("build", str(tmp_path), "m", stack="phaser"))
    assert res["ok"] is True
    assert calls  # context editing was consulted before each POST
