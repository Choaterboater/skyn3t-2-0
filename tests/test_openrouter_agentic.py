"""OpenRouter agentic codegen loop: a cheap model authors the whole project via
tool-calls (write_file/finish) with file writes confined to the workdir. This is
what lets cheap models build coherent full apps (vs the weak per-file path)."""
from __future__ import annotations

import asyncio
import json

import skyn3t.adapters.llm as llm
from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeClient:
    """Replays canned OpenRouter responses (one per POST) as an async ctx manager."""
    def __init__(self, turns):
        self._turns, self.i = list(turns), 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        p = self._turns[self.i]
        self.i += 1
        return _FakeResp(p)


def _tool_turn(name, args, tcid="t1"):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": tcid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def _client():
    return LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x"))


def test_agentic_loop_writes_files(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_file", {"path": "app/page.jsx", "content": "export default function P(){return null}"}),
        _tool_turn("finish", {"summary": "done"}, "t2"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))
    res = asyncio.run(_client()._openrouter_agentic("build", str(tmp_path), "deepseek/deepseek-v3.2"))
    assert res["ok"] is True
    assert (tmp_path / "app" / "page.jsx").read_text().startswith("export default")


def test_agentic_loop_confines_paths(tmp_path, monkeypatch):
    sub = tmp_path / "proj"
    sub.mkdir()
    outside = tmp_path / "escaped.txt"
    turns = [
        _tool_turn("write_file", {"path": "../escaped.txt", "content": "x"}),
        _tool_turn("finish", {}, "t2"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))
    res = asyncio.run(_client()._openrouter_agentic("x", str(sub), "m"))
    assert not outside.exists()   # path traversal blocked
    assert res["ok"] is False     # nothing written


def test_supports_agentic_openrouter_flag():
    on = LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x", openrouter_agentic=True))
    off = LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x", openrouter_agentic=False))
    assert on.supports_agentic is True
    assert off.supports_agentic is False
