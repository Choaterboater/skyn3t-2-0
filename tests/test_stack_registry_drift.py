"""Drift check for the stack-group + gate-set registry (``skyn3t/core/stacks.py``).

The recorded "3-stack-vocabulary gotcha" made concrete: adding a stack means
touching planner keywords, scaffold builders, proof commands, AND gate
selection — forget one site and the new stack silently misbehaves there. This
suite (a) locks the registry as the single source of truth for stack-GROUP
membership + end-of-build gate applicability, and (b) makes a missed
vocabulary site fail loudly instead of silently.
"""
from __future__ import annotations

import importlib

from skyn3t.core import stacks

# ---------------------------------------------------------------------------
# Section A — registry self-consistency
# ---------------------------------------------------------------------------


def test_every_group_token_is_a_known_vocabulary_word():
    """Catches typos: every token must normalize in at least one vocabulary
    (agent vocab via _normalize_stack, or planner vocab via REAL_BUILDER_STACKS)."""
    from skyn3t.agents._common import _normalize_stack
    from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS

    for group_name, group in stacks.GROUPS.items():
        for tok in group:
            assert _normalize_stack(tok) or tok in REAL_BUILDER_STACKS, (
                f"unknown stack token {tok!r} in group {group_name!r}"
            )


def test_every_gate_settings_flag_is_a_real_setting():
    from skyn3t.config.settings import Settings

    for spec in stacks.GATES:
        assert spec.settings_flag in Settings.model_fields, spec.name


def test_every_gate_handler_resolves():
    for spec in stacks.GATES:
        mod_name, _, attr_path = spec.handler.partition(":")
        obj: object = importlib.import_module(mod_name)
        for part in attr_path.split("."):
            obj = getattr(obj, part)
        assert callable(obj), spec.name


def test_headless_coupled_gates_share_the_game_stack_set():
    """game_visual/qa_playtest are dispatched off the headless gate's result
    (`gate is not None`) — they inherit its stack selection AND enable flag. A
    future game stack must not get headless without visual/playtest coverage."""
    coupled = [s for s in stacks.GATES if s.via_headless_gate]
    assert coupled, "expected via_headless_gate specs"
    for spec in coupled:
        assert spec.stacks == stacks.GAME_STACKS, spec.name


def test_gate_applies_answers_the_dispatch_question():
    assert stacks.gate_applies("headless_gate", "phaser")
    assert not stacks.gate_applies("headless_gate", "react")
    assert stacks.gate_applies("mcp_check", "mcp")
    assert not stacks.gate_applies("mcp_check", "fastapi")
    assert stacks.gate_applies("liveness", "nextjs")
    assert not stacks.gate_applies("liveness", "react_native")  # not HTTP-served
    assert stacks.gate_applies("seo_check", " NEXTJS ")  # normalizes case/space
    assert not stacks.gate_applies("not_a_gate", "react")
    assert not stacks.gate_applies("liveness", "")


# ---------------------------------------------------------------------------
# Section B — no-copy locks: the consumers must READ the registry, not paste it
# ---------------------------------------------------------------------------


def test_runner_sets_are_the_registry_objects():
    from skyn3t.studio import runner

    assert runner._WEB_STACKS == stacks.WEB_STACKS
    assert runner._DESIGN_STACKS == stacks.DESIGN_STACKS
    assert runner._UI_WEB_STACKS == stacks.UI_WEB_STACKS
    assert runner._GAME_STACKS == stacks.GAME_STACKS
