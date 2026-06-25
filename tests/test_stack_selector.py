# tests/test_stack_selector.py
from __future__ import annotations

import asyncio

from skyn3t.studio.stack_selector import (
    REAL_BUILDER_STACKS,
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
