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
        if self.i >= len(self._turns):
            return _FakeResp({"choices": [{"message": {"content": "done"}}]})  # terminal: no tool calls
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
    assert res["model"] == "deepseek/deepseek-v3.2"
    assert _client().last_model is None
    assert (tmp_path / "app" / "page.jsx").read_text().startswith("export default")


def test_agentic_loop_records_effective_model(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_file", {"path": "app/page.jsx", "content": "export default function P(){return null}"}),
        _tool_turn("finish", {"summary": "done"}, "t2"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))
    client = _client()
    res = asyncio.run(client._openrouter_agentic("build", str(tmp_path), "openrouter/selected"))
    assert res["model"] == "openrouter/selected"
    assert client.last_model == "openrouter/selected"
    assert client.routes[-1] == ("backend", "codegen", "openrouter/selected")


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


def test_agentic_loop_pushes_past_a_stub(tmp_path, monkeypatch):
    # Model finishes early with only a thin page (a stub). The anti-stub guard must
    # push it to keep building real section components before accepting finish.
    big = "x" * 1500
    turns = [
        _tool_turn("write_file", {"path": "app/page.jsx", "content": "export default ()=>null"}),
        _tool_turn("finish", {}, "t2"),                                   # premature finish -> stub
        _tool_turn("write_file", {"path": "components/Hero.jsx", "content": big}, "t3"),
        _tool_turn("write_file", {"path": "components/Services.jsx", "content": big}, "t4"),
        _tool_turn("write_file", {"path": "components/About.jsx", "content": big}, "t5"),
        _tool_turn("finish", {}, "t6"),                                   # now substantive -> accepted
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))
    res = asyncio.run(_client()._openrouter_agentic("build", str(tmp_path), "m"))
    assert res["ok"] is True
    assert (tmp_path / "components" / "Hero.jsx").exists()   # nudge made it build the UI
    assert (tmp_path / "components" / "Services.jsx").exists()


def test_antistub_catches_leftover_scaffold_homepage(tmp_path, monkeypatch):
    # The model builds components but leaves the offline-scaffold placeholder homepage
    # (orphaned library + counter stub renders). The guard must catch the leftover
    # scaffold ENTRY (not just count components) and push it to overwrite app/page.jsx.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text("// generated offline by SkyN3t\nexport default () => 'count is 0';")
    (tmp_path / "components").mkdir()
    for n in ("Hero", "Services", "About"):
        (tmp_path / "components" / f"{n}.jsx").write_text("x" * 1500)
    turns = [
        _tool_turn("finish", {}, "t1"),  # premature: scaffold homepage still present
        _tool_turn("write_file", {"path": "app/page.jsx",
                                  "content": "import Hero from '../components/Hero';\nexport default () => <Hero/>;"}, "t2"),
        _tool_turn("finish", {}, "t3"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))
    res = asyncio.run(_client()._openrouter_agentic("build", str(tmp_path), "m"))
    assert res["ok"] is True
    assert "generated offline" not in (tmp_path / "app" / "page.jsx").read_text().lower()


def test_agentic_system_prompt_is_stack_aware():
    # SkyN3t builds every kind of app, so the codegen system prompt must be composed per
    # stack — not hardcoded to "build a React/Next marketing site". Web stacks keep the
    # marketing guidance; games/mobile/desktop/api/cli get their own, with no web cruft.
    sysfor = llm._agentic_system_for
    web, game = sysfor("react_vite"), sysfor("phaser")
    mobile, desktop = sysfor("react_native"), sysfor("tauri")
    api, cli = sysfor("fastapi"), sysfor("python_cli")

    # Web keeps its tuned marketing guidance; unknown/empty falls back to web.
    assert "marketing site" in web and "lucide-react" in web and "next/font" in web
    assert sysfor("") == web == llm._AGENTIC_SYSTEM        # back-compat default preserved

    # Each non-web stack gets appropriate guidance and NONE of the web-marketing cruft.
    web_cruft = ("marketing site", "hero, services", "next/font", "lucide-react")
    assert "real-time GAME" in game and "game loop" in game
    assert "MOBILE app" in mobile and "native navigation" in mobile
    assert "DESKTOP app" in desktop
    assert "BACKEND service/API" in api and "endpoints" in api
    assert "COMMAND-LINE tool" in cli
    for body in (game, mobile, desktop, api, cli):
        assert not any(c in body for c in web_cruft)

    # Universal core + tail + anti-derailment guard present on every stack.
    for s in (web, game, mobile, desktop, api, cli):
        assert "OPENROUTER_API_KEY" in s
        assert "never silently switch to a different kind of project" in s


def test_antistub_nudge_skipped_for_game_stack(tmp_path, monkeypatch):
    # A phaser game has no .jsx components, so the React `_looks_stub()` heuristic is
    # ALWAYS true. Firing the nudge here drags the model off the game into a React
    # marketing site that clobbers it (the validated 2026-06-29 derailment). For a
    # non-React stack the nudge must be suppressed: the early `finish` is accepted and
    # the would-be marketing components are never written.
    big = "x" * 1500
    turns = [
        _tool_turn("write_file", {"path": "src/sim/sim.js", "content": "export const step=()=>{}"}),
        _tool_turn("finish", {}, "t2"),                                    # "stub" under React heuristic
        _tool_turn("write_file", {"path": "components/Hero.jsx", "content": big}, "t3"),  # must NOT run
        _tool_turn("finish", {}, "t4"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))
    res = asyncio.run(_client()._openrouter_agentic("build", str(tmp_path), "m", stack="phaser"))
    assert res["ok"] is True
    assert (tmp_path / "src" / "sim" / "sim.js").exists()
    assert not (tmp_path / "components" / "Hero.jsx").exists()   # nudge suppressed -> no derailment


def test_antistub_nudge_still_fires_for_react_stack(tmp_path, monkeypatch):
    # The nudge must keep working for React-class stacks (its intended target): a thin
    # homepage + early finish is pushed to build real section components.
    big = "x" * 1500
    turns = [
        _tool_turn("write_file", {"path": "app/page.jsx", "content": "export default ()=>null"}),
        _tool_turn("finish", {}, "t2"),                                    # premature finish -> stub
        _tool_turn("write_file", {"path": "components/Hero.jsx", "content": big}, "t3"),
        _tool_turn("write_file", {"path": "components/Services.jsx", "content": big}, "t4"),
        _tool_turn("write_file", {"path": "components/About.jsx", "content": big}, "t5"),
        _tool_turn("finish", {}, "t6"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))
    res = asyncio.run(_client()._openrouter_agentic("build", str(tmp_path), "m", stack="react_vite"))
    assert res["ok"] is True
    assert (tmp_path / "components" / "Hero.jsx").exists()       # nudge still drives the UI build


def test_supports_agentic_openrouter_flag():
    on = LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x", openrouter_agentic=True))
    off = LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x", openrouter_agentic=False))
    assert on.supports_agentic is True
    assert off.supports_agentic is False
