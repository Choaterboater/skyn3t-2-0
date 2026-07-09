"""OpenRouter agentic codegen loop: a cheap model authors the whole project via
tool-calls (write_file/finish) with file writes confined to the workdir. This is
what lets cheap models build coherent full apps (vs the weak per-file path)."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

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


class _DelayedClient(_FakeClient):
    """Replays each turn after a fixed delay to exercise progress deadlines."""

    def __init__(self, turns, delay):
        super().__init__(turns)
        self.delay = delay

    async def post(self, url, json=None, headers=None):
        await asyncio.sleep(self.delay)
        return await super().post(url, json=json, headers=headers)


class _StallPrimaryThenFallbackClient:
    """Primary model stalls once; fallback model then writes and finishes."""

    def __init__(self):
        self.models: list[str] = []
        self.fallback_turn = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        model = (json or {}).get("model", "")
        self.models.append(model)
        if model == "primary/slow":
            await asyncio.sleep(0.05)
            return _FakeResp({"choices": [{"message": {"content": "too late"}}]})
        self.fallback_turn += 1
        if self.fallback_turn == 1:
            return _FakeResp(_tool_turn(
                "write_file",
                {"path": "server.py", "content": "def app():\n    return 'ok'\n"},
            ))
        return _FakeResp(_tool_turn("finish", {}, "t2"))


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


def test_agentic_loop_writes_validated_file_batch(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_files", {"files": [
            {"path": "src/main.py", "content": "from .service import value\n"},
            {"path": "src/service.py", "content": "value = 42\n"},
            {"path": "pyproject.toml", "content": "[project]\nname='batch-app'\n"},
        ]}),
        _tool_turn("finish", {"summary": "done"}, "t2"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))

    res = asyncio.run(_client()._openrouter_agentic(
        "build", str(tmp_path), "m", stack="fastapi"
    ))

    assert res["ok"] is True
    assert res["completed"] is True
    assert res["files_written"] == 3
    assert (tmp_path / "src" / "main.py").exists()
    assert (tmp_path / "src" / "service.py").exists()
    assert (tmp_path / "pyproject.toml").exists()


def test_agentic_batch_rejects_all_files_if_one_path_escapes(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_files", {"files": [
            {"path": "src/safe.py", "content": "safe = True\n"},
            {"path": "../escaped.py", "content": "escaped = True\n"},
        ]}),
        _tool_turn("finish", {}, "t2"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))

    res = asyncio.run(_client()._openrouter_agentic(
        "build", str(tmp_path), "m", stack="fastapi"
    ))

    assert res["ok"] is False
    assert not (tmp_path / "src" / "safe.py").exists()
    assert not (tmp_path.parent / "escaped.py").exists()


def test_agentic_batch_rejects_duplicate_normalized_paths(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_files", {"files": [
            {"path": "src/../app.py", "content": "first = True\n"},
            {"path": "app.py", "content": "second = True\n"},
        ]}),
        _tool_turn("finish", {}, "t2"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))

    res = asyncio.run(_client()._openrouter_agentic(
        "build", str(tmp_path), "m", stack="fastapi"
    ))

    assert res["ok"] is False
    assert not (tmp_path / "app.py").exists()


def test_agentic_slice_tool_writes_only_owned_paths(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_files", {"files": [
            {"path": "config/owned.json", "content": '{"ok": true}\n'},
            {"path": "src/out-of-scope.jsx", "content": "export default null;\n"},
        ]}),
        _tool_turn(
            "write_file",
            {"path": "src/also-out-of-scope.jsx", "content": "export default null;\n"},
            "t2",
        ),
        _tool_turn("finish", {}, "t3"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))

    res = asyncio.run(_client()._openrouter_agentic(
        "build config only",
        str(tmp_path),
        "m",
        stack="fastapi",
        allowed_paths=["config/owned.json"],
    ))

    assert res["ok"] is True
    assert res["files_written"] == 1
    assert (tmp_path / "config" / "owned.json").is_file()
    assert not (tmp_path / "src" / "out-of-scope.jsx").exists()
    assert not (tmp_path / "src" / "also-out-of-scope.jsx").exists()


def test_agentic_write_progress_extends_nominal_window(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_files", {"files": [
            {"path": f"src/part{i}.py", "content": f"part = {i}\n"},
        ]}, f"t{i}")
        for i in range(3)
    ]
    turns.append(_tool_turn("finish", {}, "tf"))
    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda *a, **k: _DelayedClient(turns, delay=0.04),
    )

    started = time.monotonic()
    res = asyncio.run(_client()._openrouter_agentic(
        "build", str(tmp_path), "m", timeout=0.10, stack="fastapi"
    ))
    elapsed = time.monotonic() - started

    assert elapsed > 0.10, "total duration may exceed the no-progress window"
    assert res["ok"] is True
    assert res["files_written"] == 3


def test_agentic_request_without_progress_times_out(tmp_path, monkeypatch):
    turns = [_tool_turn(
        "write_file", {"path": "too-late.py", "content": "late = True\n"}
    )]
    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda *a, **k: _DelayedClient(turns, delay=0.08),
    )

    res = asyncio.run(_client()._openrouter_agentic(
        "build", str(tmp_path), "m", timeout=0.03, stack="fastapi"
    ))

    assert res["ok"] is False
    assert res["completed"] is False
    assert res["timed_out"] is True
    assert "no file-write progress" in res["error"]
    assert not (tmp_path / "too-late.py").exists()


def test_identical_rewrites_do_not_extend_progress_window(tmp_path, monkeypatch):
    same = _tool_turn(
        "write_file", {"path": "same.py", "content": "value = 1\n"}
    )
    turns = [same, same, same, _tool_turn("finish", {}, "tf")]
    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda *a, **k: _DelayedClient(turns, delay=0.04),
    )

    res = asyncio.run(_client()._openrouter_agentic(
        "build", str(tmp_path), "m", timeout=0.09, stack="fastapi"
    ))

    assert res["ok"] is False
    assert res["timed_out"] is True
    assert res["files_written"] == 1


def test_changed_rewrite_counts_as_real_progress(tmp_path, monkeypatch):
    turns = [
        _tool_turn("write_file", {"path": "changed.py", "content": "value = 1\n"}),
        _tool_turn(
            "write_file", {"path": "changed.py", "content": "value = 2\n"}, "t2"
        ),
        _tool_turn("finish", {}, "tf"),
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))

    res = asyncio.run(_client()._openrouter_agentic(
        "build", str(tmp_path), "m", stack="fastapi"
    ))

    assert res["ok"] is True
    assert res["files_written"] == 2
    assert (tmp_path / "changed.py").read_text() == "value = 2\n"


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


def test_agentic_loop_stall_falls_over_to_configured_fast_model(tmp_path, monkeypatch):
    fake = _StallPrimaryThenFallbackClient()
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)
    client = LLMClient(Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        free_only=False,
        llm_max_retries=0,
        llm_fallback_models="fallback/fast",
        agentic_verify_on_stop=False,
    ))
    client.settings.agentic_idle_timeout = 0.01

    res = asyncio.run(client._openrouter_agentic(
        "build",
        str(tmp_path),
        "primary/slow",
        stack="fastapi",
    ))

    assert res["ok"] is True
    assert res["stalled"] is True
    assert res["attempted_model"] == "primary/slow"
    assert res["fallback_model"] == "fallback/fast"
    assert res["turn_timeouts"] == 1
    assert fake.models[:2] == ["primary/slow", "fallback/fast"]
    assert (tmp_path / "server.py").exists()


def test_agentic_loop_records_openrouter_usage_for_budget(tmp_path, monkeypatch):
    turns = [
        {
            **_tool_turn(
                "write_file",
                {"path": "src/main.js", "content": "export const ok = true;"},
            ),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        {
            **_tool_turn("finish", {}, "t2"),
            "usage": {"prompt_tokens": 6, "completion_tokens": 4},
        },
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: _FakeClient(turns))
    client = LLMClient(
        Settings(
            llm_backend="openrouter",
            openrouter_api_key="x",
            free_only=False,
            per_build_usd_cap=10.0,
            daily_usd_cap=10.0,
            daily_token_cap=10_000,
        )
    )

    res = asyncio.run(
        client._openrouter_agentic("build", str(tmp_path), "provider/paid", stack="phaser")
    )

    assert res["ok"] is True
    assert client.budget.tokens_day == 25
    assert client.budget.spent_build > 0.0


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


@pytest.mark.parametrize(
    ("stack", "sources"),
    [
        ("astro", [
            ("src/pages/index.astro", 2200),
            ("src/components/Hero.astro", 1500),
            ("src/components/Services.astro", 1500),
        ]),
        ("static_html", [
            ("index.html", 1600),
            ("about.html", 1600),
            ("contact.html", 1600),
            ("styles.css", 1200),
        ]),
        ("vue", [
            ("src/App.vue", 2200),
            ("src/components/Hero.vue", 1500),
            ("src/components/Services.vue", 1500),
        ]),
        ("sveltekit", [
            ("src/routes/+page.svelte", 2200),
            ("src/components/Hero.svelte", 1500),
            ("src/components/Services.svelte", 1500),
        ]),
    ],
)
def test_antistub_uses_each_web_stacks_native_sources(tmp_path, stack, sources):
    for rel, size in sources:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x" * size)

    assert llm._agentic_project_looks_stub(tmp_path, stack) is False


def test_antistub_does_not_count_data_as_a_static_homepage(tmp_path):
    (tmp_path / "index.html").write_text("<main>thin</main>")
    data = tmp_path / "src" / "data.js"
    data.parent.mkdir(parents=True)
    data.write_text("export const rows = " + "x" * 6000)

    assert llm._agentic_project_looks_stub(tmp_path, "static_html") is True


def test_supports_agentic_openrouter_flag():
    on = LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x", openrouter_agentic=True))
    off = LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x", openrouter_agentic=False))
    assert on.supports_agentic is True
    assert off.supports_agentic is False
