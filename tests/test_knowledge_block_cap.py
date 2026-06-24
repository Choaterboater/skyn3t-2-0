"""Injected skill advice is capped so it can't bloat the codegen prompt.

Full skill-doc bodies (tens of KB each) otherwise dwarf the build instruction
and slow claude -p into its timeout, shipping a stub.
"""

from __future__ import annotations

from skyn3t.agents._common import knowledge_block, _MAX_SKILL_ADVICE


def test_huge_skill_advice_is_capped():
    out = knowledge_block({"skills_advice": "x" * 50_000})
    assert len(out) <= _MAX_SKILL_ADVICE + 200  # not ~50KB


def test_small_skill_advice_passes_through():
    out = knowledge_block({"skills_advice": "use semantic HTML and react-hook-form"})
    assert "use semantic HTML and react-hook-form" in out


def test_overall_block_stays_bounded():
    out = knowledge_block({
        "skills_advice": "a" * 50_000,
        "recall": ["b" * 50_000],
        "lessons": ["c" * 50_000],
    })
    assert len(out) < 20_000  # every section is bounded
