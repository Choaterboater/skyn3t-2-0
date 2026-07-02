"""Bolt full-file contract (wave-2 item 44) reaches the REPAIR prompt too.

The dangling-import / elided-file bug class ("// rest of the code remains the
same") sneaks in most often on a REWRITE, so the no-placeholder contract must be
present in the code-improver's rewrite prompt, not only in fresh codegen. This
asserts the shared contract string is injected where the improver builds its
prompt.
"""

from __future__ import annotations

import asyncio

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.core.events import EventBus


class _CapturingLLM:
    """Records the prompt the improver would send, returns a benign rewrite."""

    backend = "openrouter"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)

        class _R:
            backend = "openrouter"
            text = "export const x = 1;\n"

        return _R()


def test_improver_rewrite_prompt_carries_full_file_contract(tmp_path) -> None:
    f = tmp_path / "src" / "util.js"
    f.parent.mkdir(parents=True)
    f.write_text("export const x = 0;\n", encoding="utf-8")

    llm = _CapturingLLM()
    agent = CodeImproverAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]
    asyncio.run(
        agent._improve_one("src/util.js", "export const x = 0;\n",
                           "a util module", ["make x = 1"], "react")
    )
    assert llm.prompts, "improver never built a rewrite prompt"
    low = llm.prompts[0].lower()
    assert "complete file" in low
    assert "rest of code" in low
    assert "unchanged" in low
