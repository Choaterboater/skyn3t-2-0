# tests/test_codegen_validation.py
"""Codegen must not write invalid source. extract_code must handle a fence with
no newline after the language marker; _generate_file must return None (keep the
scaffold) when both LLM attempts fail validation, not write broken code."""
from __future__ import annotations

from skyn3t.agents._common import extract_code
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus


def test_extract_code_handles_fence_without_newline():
    # An LLM fence like ```json{...}``` (no newline) must still extract the body,
    # not return the raw fence markup (which then fails validation).
    assert extract_code('```json{"x": 1}```').strip() == '{"x": 1}'


def test_extract_code_still_handles_normal_fence():
    assert extract_code("```python\nprint(1)\n```").strip() == "print(1)"


class _FakeResult:
    def __init__(self, text: str, backend: str = "claude_cli"):
        self.text = text
        self.backend = backend


class _FakeLLM:
    backend = "claude_cli"
    supports_agentic = False

    def __init__(self, text: str):
        self._text = text

    async def complete(self, *a, **k):
        return _FakeResult(self._text)


async def test_generate_file_returns_none_when_both_attempts_invalid(tmp_path):
    # Invalid Python from both the initial call and the retry must yield None so
    # the caller keeps the runnable scaffold rather than writing broken code.
    agent = CodeAgent(event_bus=EventBus(), llm=_FakeLLM("def broken(:\n    pass\n"))
    out = await agent._generate_file("app.py", "a brief", "python", {"files": [], "summary": ""})
    assert out is None


async def test_generate_file_returns_valid_code_unchanged(tmp_path):
    agent = CodeAgent(event_bus=EventBus(), llm=_FakeLLM("x = 1\n"))
    out = await agent._generate_file("app.py", "a brief", "python", {"files": [], "summary": ""})
    assert out is not None and "x = 1" in out
