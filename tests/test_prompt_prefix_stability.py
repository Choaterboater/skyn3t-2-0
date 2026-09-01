"""Byte-stable prompt prefixes for provider prompt caching (hermes v0.19.0).

SkyN3t re-sends the large system/directive blocks on every fix-loop iteration
(the initial codegen prompt, then one resume prompt per retry). Provider
prompt caching only hits when the SHARED PREFIX is byte-identical, so the
block ordering is a tested invariant:

* STATIC sections — knowledge, brief, stack/variant directives, architecture,
  instructions, the design bar + tokens, and the contracts/conventions — come
  FIRST, in one fixed order, byte-identical across attempts of the same build.
* VOLATILE sections — council guidance (moa), retry feedback, missing-file and
  existing-file lists — ONLY ever append at the TAIL.

The llm.py side is audited, not reordered: the agentic loop's message assembly
is ``[system, user]`` with the stack-scoped system block leading every request
and loop nudges appending at the tail — already prefix-optimal.
"""

from __future__ import annotations

import os

from skyn3t.adapters.llm import _agentic_system_for
from skyn3t.agents.code_agent import (
    _CONFIG_DIRECTIVE,
    _DESIGN_DIRECTIVE,
    _FULL_FILE_CONTRACT,
    _LLM_DIRECTIVE,
    _VENT_DIRECTIVE,
    CodeAgent,
)
from skyn3t.core.events import EventBus

_BRIEF = "a habit tracker for busy parents"
_STACK = "react_vite"
_KNOWLEDGE = "SKILL QUALITY CONTRACT (apply these stack-specific rules):\nkeep state pure\n"
_PLAN = {
    "stack": _STACK,
    "summary": "A complete habit tracker application.",
    "files": [
        {"path": "src/App.jsx", "purpose": "root component"},
        {"path": "src/main.js", "purpose": "entrypoint"},
        {"path": "README.md", "purpose": "docs"},
    ],
}

# One marker per static section, in the fixed order the sections must appear.
_STATIC_MARKERS = [
    _KNOWLEDGE,
    f"Build a COMPLETE, production-quality {_STACK} application",
    "Architecture summary:",
    "Planned files:",
    "Write ALL files into the CURRENT directory",
    "DEPENDENCY MANIFEST (required)",
    _DESIGN_DIRECTIVE,
    "DESIGN TOKENS",
    _FULL_FILE_CONTRACT,
    _CONFIG_DIRECTIVE,
    _VENT_DIRECTIVE,
    _LLM_DIRECTIVE,
    "Do not ask questions — just build it.",
]


def _agent() -> CodeAgent:
    return CodeAgent(event_bus=EventBus())


def _initial(agent: CodeAgent, moa: str = "") -> str:
    return agent._agentic_prompt(
        _BRIEF, _STACK, _PLAN, _KNOWLEDGE, design=None, moa_guidance=moa)


def _resume(agent: CodeAgent, *, files, missing, error, gap="") -> str:
    return agent._agentic_resume_prompt(
        _BRIEF, _STACK, _PLAN, files, missing, error, gap,
        design=None, knowledge=_KNOWLEDGE)


def test_static_sections_appear_once_in_fixed_order():
    prompt = _initial(_agent())
    positions = []
    for marker in _STATIC_MARKERS:
        assert marker in prompt, f"missing static section: {marker[:60]}"
        positions.append(prompt.index(marker))
    assert positions == sorted(positions), "static sections out of fixed order"


def test_initial_prompts_byte_identical_except_the_tail():
    agent = _agent()
    plain = _initial(agent)
    assert plain == _initial(agent)  # deterministic for the same build inputs
    guided_a = _initial(agent, moa="ADVISORY COUNCIL — alpha guidance")
    guided_b = _initial(agent, moa="completely different council advice")
    # Volatile council guidance only extends the tail: the full static prompt
    # is a strict prefix of every guided variant.
    assert guided_a.startswith(plain)
    assert guided_b.startswith(plain)
    assert guided_a != guided_b
    assert plain.endswith("Do not ask questions — just build it.")


def test_resume_prompt_shares_the_full_static_head_with_initial():
    agent = _agent()
    initial = _initial(agent)
    resume = _resume(
        agent, files={"src/App.jsx": "..."}, missing=["src/main.js"],
        error="provider timeout")
    # LCP(initial, resume) IS the entire static head: the resume re-sends it
    # byte-identical, so the provider cache hits on the fix-loop iteration.
    assert resume.startswith(initial)
    lcp = os.path.commonprefix([initial, resume])
    for marker in _STATIC_MARKERS:
        assert marker in lcp, f"shared prefix lost a static section: {marker[:60]}"


def test_lcp_covers_static_blocks_even_with_council_guidance():
    agent = _agent()
    initial_guided = _initial(agent, moa="ADVISORY COUNCIL — beta guidance")
    resume = _resume(agent, files={}, missing=["README.md"], error="", gap="missing docs")
    lcp = os.path.commonprefix([initial_guided, resume])
    for marker in _STATIC_MARKERS:
        assert marker in lcp, f"shared prefix lost a static section: {marker[:60]}"


def test_resume_prompts_with_varying_volatile_inputs_differ_only_at_the_tail():
    agent = _agent()
    initial = _initial(agent)
    resume_a = _resume(
        agent, files={"src/App.jsx": "x"}, missing=["src/main.js"], error="timeout")
    resume_b = _resume(
        agent, files={}, missing=["src/main.js", "README.md"], error="",
        gap="missing entrypoint")
    shared = os.path.commonprefix([resume_a, resume_b])
    # Both resumes carry the identical static head AND the identical resume
    # preamble; the volatile issues/file lists diverge only after it.
    assert shared.startswith(initial)
    assert "RESUME IN PLACE" in shared
    tail_a = resume_a[len(initial):]
    assert "- timeout" in tail_a and "- src/main.js" in tail_a
    tail_b = resume_b[len(initial):]
    assert "- missing entrypoint" in tail_b and "- README.md" in tail_b


def test_volatile_sections_never_precede_the_static_directives():
    agent = _agent()
    resume = _resume(
        agent, files={"src/App.jsx": "x"}, missing=["src/main.js"],
        error="boom", gap="gap")
    last_static = resume.index("Do not ask questions — just build it.")
    for volatile in (
        "RESUME IN PLACE",
        "Prior completion issues:",
        "Files still required by the approved architecture:",
        "Files already present (inspect before editing):",
        "- boom",
    ):
        assert resume.index(volatile) > last_static


def test_game_stack_prompts_also_share_the_static_head():
    agent = _agent()
    plan = {
        "stack": "phaser",
        "summary": "A phaser arcade game.",
        "files": [
            {"path": "src/main.js", "purpose": "boot"},
            {"path": "src/sim.js", "purpose": "pure simulation"},
        ],
    }
    brief = "a neon arcade platformer"
    initial = agent._agentic_prompt(brief, "phaser", plan, "", design=None)
    resume = agent._agentic_resume_prompt(
        brief, "phaser", plan, {}, ["src/sim.js"], "timeout", "",
        design=None, knowledge="")
    assert resume.startswith(initial)
    assert "GAME" in initial  # the stack directives ride the shared head


def test_agentic_system_block_leads_and_is_stable():
    # llm.py assembles [system, user]: the stack-scoped system block leads
    # every request (the head of the cached prefix) and must be deterministic
    # across sessions of the same stack.
    assert _agentic_system_for(_STACK)
    assert _agentic_system_for(_STACK) == _agentic_system_for(_STACK)
