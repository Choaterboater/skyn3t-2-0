# tests/test_stack_selector.py
from __future__ import annotations

import asyncio

import pytest

from skyn3t.studio.stack_selector import (
    REAL_BUILDER_STACKS,
    classify_build,
    keyword_choice,
    select_stack,
)


def test_pin_wins_over_everything():
    c = asyncio.run(select_stack("a python script", pin="fastapi", llm=None))
    assert c.stack == "fastapi" and c.method == "pin"


def test_unknown_pin_is_ignored():
    c = asyncio.run(select_stack("a react dashboard", pin="cobol", llm=None))
    assert c.method == "keyword" and c.stack == "react"


def test_keyword_fallback_when_no_llm():
    c = keyword_choice("a command line tool to rename files")
    assert c.stack in REAL_BUILDER_STACKS and c.method == "keyword"


def test_nextjs_brief_picks_nextjs_builder():
    # nextjs is now a REAL builder stack — a next.js brief resolves to nextjs,
    # no longer collapsing to the plain react/Vite scaffold.
    c = keyword_choice("a next.js app")
    assert c.stack == "nextjs"


def test_nextjs_pin_is_accepted_as_real_builder():
    c = asyncio.run(select_stack("anything", pin="nextjs", llm=None))
    assert c.method == "pin" and c.stack == "nextjs"


def test_llm_choice_used_when_available():
    class FakeResult:
        backend = "claude_cli"
        text = '{"stack": "fastapi", "confidence": 0.9, "rationale": "needs a server API"}'

    class FakeLLM:
        async def complete(self, *a, **k):
            return FakeResult()

    c = asyncio.run(select_stack("an app to manage lessons with storage", llm=FakeLLM()))
    assert c.stack == "fastapi" and c.method == "llm" and c.confidence == 0.9


def test_llm_error_falls_back_to_keyword():
    class BadLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("boom")

    c = asyncio.run(select_stack("a react dashboard", llm=BadLLM()))
    assert c.method == "keyword" and c.stack == "react"


def test_stub_backend_falls_back_to_keyword():
    class StubResult:
        backend = "stub"
        text = '{"stack": "fastapi", "confidence": 0.9, "rationale": "x"}'

    class StubLLM:
        async def complete(self, *a, **k):
            return StubResult()

    c = asyncio.run(select_stack("a react dashboard", llm=StubLLM()))
    assert c.method == "keyword" and c.stack == "react"


def test_website_brief_picks_web_not_python():
    # Regression: a "website" brief with NO explicit pin must not come out
    # python. Previously the clarifier's auto-default ("python") was treated as
    # a pin and bypassed selection; now the keyword fallback (and the LLM, when
    # available) reads the brief — "website" maps to the static web stack.
    c = keyword_choice("a website for kids to print off coloring pages of pandas")
    assert c.stack == "static" and c.stack != "python"


def test_typoed_site_brief_picks_web_via_keyword():
    # The REAL failing brief: misspelled "webiste" (so the old "website" keyword
    # never matched) but it says "a site for templates". The keyword fallback now
    # recognizes "site" -> static, so even with the LLM degraded a site brief is
    # not mis-stacked as a python CLI.
    c = keyword_choice(
        "webiste for kids to print off coloring pages - so a site for templates for cloring"
    )
    assert c.stack == "static" and c.stack != "python"


def test_cli_brief_maps_to_python_not_react():
    # Swarm-debug bug: "cli" had no _COLLAPSE entry + the last-resort default was
    # "react", so a command-line brief came out a React app. Must be python.
    c = keyword_choice("a cli tool to rename files in a folder")
    assert c.stack == "python" and c.stack != "react"


def test_classify_build_infers_game_engine_metadata():
    c = classify_build("a space arcade shooter game", "phaser")
    assert c.app_type == "game"
    assert c.engine == "phaser"
    assert c.method == "inferred"
    assert c.layout_profile == "immersive"


def test_classify_build_respects_overrides_without_changing_stack():
    c = classify_build(
        "a dashboard that charts uploaded CSV data",
        "react",
        app_type_override="data viz",
        engine_override="browser_native",
    )
    assert c.app_type == "data_viz"
    assert c.engine == "browser_native"
    assert c.method == "override"
    assert c.layout_profile == "workspace"


def test_classify_build_rejects_control_text_in_overrides():
    c = classify_build(
        "a React dashboard for metrics",
        "react",
        app_type_override="dashboard" + chr(10) + "untrusted_control_text",
        engine_override="dom" + chr(10) + "untrusted_control_text",
    )

    assert c.method == "inferred"
    assert c.app_type == "dashboard"
    assert c.engine == "dom"


def test_classify_build_infers_dashboard_dom():
    c = classify_build("a React admin dashboard for metrics", "react")
    assert c.app_type == "dashboard"
    assert c.engine == "dom"
    assert c.to_dict()["layout_profile"] == "workspace"


def test_classify_build_keeps_browser_only_react_dashboard_out_of_api_service():
    c = classify_build(
        "a browser-only React paper-trading dashboard with no API, backend, or server",
        "react",
    )
    assert c.app_type == "dashboard"
    assert c.engine == "dom"


def test_typesafe_fast_path_skipped_without_settings():
    """No settings passed -> TypeSafe fast path never engages; existing
    keyword/LLM resolution is completely unaffected (default behavior)."""
    c = asyncio.run(select_stack("a react dashboard", llm=None))
    assert c.method == "keyword" and c.stack == "react"


def test_typesafe_fast_path_skipped_when_unconfigured(monkeypatch):
    """Settings present but no TypeSafe key configured -> adapter.available is
    False -> fast path degrades to None, falling through to keyword/LLM."""
    from skyn3t.config.settings import Settings

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("SKYN3T_TYPESAFE_API_KEY", raising=False)
    settings = Settings(typesafe_api_key="")
    c = asyncio.run(select_stack("a react dashboard", llm=None, settings=settings))
    assert c.method == "keyword" and c.stack == "react"


def test_typesafe_fast_path_used_when_available(monkeypatch):
    """When TypeSafe is configured and returns a valid choice, it wins over
    the LLM path (never called) — the fast path IS the resolution, not just
    an advisory."""
    from skyn3t.config.settings import Settings
    from skyn3t.studio import stack_selector

    settings = Settings(typesafe_api_key="fake_key_for_mock")

    class _FakeAdapter:
        available = True

        def __init__(self, *_a, **_k):
            pass

        def ask_choice(self, **_kwargs):
            return {"choice": "react", "confidence": 0.95}

    monkeypatch.setattr("skyn3t.adapters.typesafe.TypeSafeAdapter", _FakeAdapter)

    async def _boom(*_a, **_k):
        raise AssertionError("LLM path must not be called when TypeSafe succeeds")

    monkeypatch.setattr(stack_selector, "_llm_choice", _boom)

    c = asyncio.run(select_stack("a react dashboard", llm="not-none-sentinel", settings=settings))
    assert c.method == "typesafe" and c.stack == "react" and c.confidence == 0.95


def test_typesafe_fast_path_unknown_choice_falls_back(monkeypatch):
    from skyn3t.config.settings import Settings

    settings = Settings(typesafe_api_key="fake_key_for_mock")

    class _FakeAdapter:
        available = True

        def __init__(self, *_a, **_k):
            pass

        def ask_choice(self, **_kwargs):
            return {"choice": "not_a_real_stack", "confidence": 0.9}

    monkeypatch.setattr("skyn3t.adapters.typesafe.TypeSafeAdapter", _FakeAdapter)

    c = asyncio.run(select_stack("a react dashboard", llm=None, settings=settings))
    assert c.method == "keyword" and c.stack == "react"


@pytest.mark.parametrize("confidence", [0, 0.52, 0.599, None, -1, 1.1, float("nan"),
                                        float("inf"), True, "invalid"])
def test_typesafe_uncertain_or_invalid_confidence_uses_llm(monkeypatch, confidence):
    from unittest.mock import AsyncMock, MagicMock

    from skyn3t.studio.stack_selector import StackChoice

    adapter = MagicMock()
    adapter.ask_choice.return_value = {"choice": "python", "confidence": confidence}
    monkeypatch.setattr("skyn3t.adapters.typesafe.TypeSafeAdapter", lambda _: adapter)
    llm_choice = AsyncMock(return_value=StackChoice("react", "llm", 0.9, "web app"))
    monkeypatch.setattr("skyn3t.studio.stack_selector._llm_choice", llm_choice)

    choice = asyncio.run(select_stack("a react dashboard", settings=object(), llm=object()))

    assert choice.method == "llm"
    assert choice.stack == "react"
    llm_choice.assert_awaited_once()


@pytest.mark.parametrize("confidence", [0.6, 1.0])
def test_typesafe_confidence_boundary_accepted(monkeypatch, confidence):
    from unittest.mock import MagicMock

    adapter = MagicMock()
    adapter.ask_choice.return_value = {"choice": "react", "confidence": confidence}
    monkeypatch.setattr("skyn3t.adapters.typesafe.TypeSafeAdapter", lambda _: adapter)
    choice = asyncio.run(select_stack("a dashboard", settings=object()))
    assert choice.method == "typesafe"
    assert choice.confidence == confidence


def test_explicit_pin_bypasses_typesafe(monkeypatch):
    from unittest.mock import MagicMock

    adapter = MagicMock(side_effect=AssertionError("pin must not call TypeSafe"))
    monkeypatch.setattr("skyn3t.adapters.typesafe.TypeSafeAdapter", adapter)
    choice = asyncio.run(select_stack("a dashboard", pin="react", settings=object()))
    assert choice.method == "pin"
    adapter.assert_not_called()


@pytest.mark.parametrize("response", [{}, {"choice": "react"}, {"choice": ["react"]}])
def test_typesafe_malformed_answers_use_keywords(monkeypatch, response):
    from unittest.mock import MagicMock

    adapter = MagicMock()
    adapter.ask_choice.return_value = response
    monkeypatch.setattr("skyn3t.adapters.typesafe.TypeSafeAdapter", lambda _: adapter)
    choice = asyncio.run(select_stack("a react dashboard", settings=object()))
    assert choice.method == "keyword"
    assert choice.stack == "react"


def test_typesafe_request_failure_uses_keywords(monkeypatch):
    from unittest.mock import MagicMock

    adapter = MagicMock()
    adapter.ask_choice.side_effect = TimeoutError("unavailable")
    monkeypatch.setattr("skyn3t.adapters.typesafe.TypeSafeAdapter", lambda _: adapter)
    choice = asyncio.run(select_stack("a react dashboard", settings=object()))
    assert choice.method == "keyword"
    assert choice.stack == "react"
