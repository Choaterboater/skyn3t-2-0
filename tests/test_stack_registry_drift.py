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
    assert stacks.gate_applies("deploy_check", "fastapi")   # URL-serving -> checkable
    assert stacks.gate_applies("deploy_check", "static")
    assert not stacks.gate_applies("deploy_check", "mcp")   # stdio, no live URL
    assert not stacks.gate_applies("deploy_check", "python")  # a CLI artifact
    assert stacks.gate_applies("cli_playtest", "python_cli")
    assert stacks.gate_applies("cli_playtest", "swift")
    assert stacks.gate_applies("cli_playtest", "swift_macos")
    assert stacks.gate_applies("cli_playtest", "swiftui")
    assert not stacks.gate_applies("cli_playtest", "react")
    assert not stacks.gate_applies("not_a_gate", "react")
    assert not stacks.gate_applies("liveness", "")


# ---------------------------------------------------------------------------
# Section B — no-copy locks: the consumers must READ the registry, not paste it
# ---------------------------------------------------------------------------


def test_runner_sets_are_the_registry_objects():
    from skyn3t.studio import runner

    # Identity, not equality — a pasted copy with the same contents must FAIL.
    assert runner._WEB_STACKS is stacks.WEB_STACKS
    assert runner._DESIGN_STACKS is stacks.DESIGN_STACKS
    assert runner._UI_WEB_STACKS is stacks.UI_WEB_STACKS
    assert runner._GAME_STACKS is stacks.GAME_STACKS


def test_static_web_gate_sets_follow_the_registry():
    from skyn3t.studio import security_check, web_polish_check

    assert security_check._WEB_STACKS == stacks.DESIGN_STACKS | stacks.UI_WEB_STACKS
    assert web_polish_check._UI_STACKS == stacks.UI_WEB_STACKS - {"phaser"}


def test_cli_playtest_uses_the_registry_stack_set():
    from skyn3t.studio import cli_playtest

    assert cli_playtest._SUPPORTED_STACKS is stacks.CLI_PLAYTEST_STACKS


def test_satellite_game_stack_copies_are_the_registry_object():
    """The four historically byte-identical _GAME_STACKS copies (the concrete
    instance of the vocabulary gotcha) must all be the registry object."""
    from skyn3t.adapters import llm
    from skyn3t.agents import architect
    from skyn3t.studio import asset_reconcile

    assert asset_reconcile._GAME_STACKS is stacks.GAME_STACKS
    assert architect._GAME_STACKS is stacks.GAME_STACKS
    assert llm._GAME_STACKS is stacks.GAME_STACKS


# ---------------------------------------------------------------------------
# Section C — cross-vocabulary completeness: a NEW stack fails here loudly if
# any touchpoint is missed (the item-62 payload; docs/ADDING_A_STACK.md).
# ---------------------------------------------------------------------------

# Proof families proof_run branches on. Python-family stacks are proved via
# boot-import/pytest (no dedicated frozenset in proof_run — maintained here).
_PY_PROOF_FAMILY = frozenset(
    {"fastapi", "python", "mcp", "rag", "workflow", "agent_pack"})


def test_every_builder_stack_covers_all_vocabularies():
    """planner keywords -> agent vocab -> scaffold builder -> proof family:
    every canonical stack must appear in ALL of them (a miss = the stack
    silently falls back or no-ops at that site)."""
    from skyn3t.agents import _scaffold
    from skyn3t.agents._common import KNOWN_STACKS, _normalize_stack
    from skyn3t.studio import planner, proof_run
    from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS

    signature_keys = {s for s, _ in planner._STACK_SIGNATURES}
    for stack in REAL_BUILDER_STACKS:
        norm = _normalize_stack(stack)
        assert norm in KNOWN_STACKS, f"{stack}: no agent-vocab mapping"
        assert norm in _scaffold._BUILDERS, (
            f"{stack}: no scaffold builder for {norm} (would silently fall back)"
        )
        assert stack in signature_keys or stack in planner._STACK_FILE_CHECKLIST, (
            f"{stack}: planner has no keywords/file checklist"
        )
        families = (
            (stack in proof_run._NODE_STACKS)
            + (stack in proof_run._SWIFT_STACKS)
            + (stack in _PY_PROOF_FAMILY)
        )
        assert families == 1, f"{stack}: in {families} proof families, expected exactly 1"


def test_every_builder_stack_scaffold_clears_the_reviewer(tmp_path):
    """The reviewer's structural heuristic is a DE-FACTO vocabulary site: a
    stack whose own pristine scaffold cannot clear GO_THRESHOLD will no_go
    every perfect build of that stack (the agent_pack 46/100 incident — its
    substance was markdown and its manifest catalog.json, which the heuristic
    didn't count). Every real builder's scaffold must score a structural go."""
    from skyn3t.agents._common import _normalize_stack
    from skyn3t.agents._scaffold import scaffold_for
    from skyn3t.agents.reviewer import GO_THRESHOLD, heuristic_score
    from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS

    for stack in sorted(REAL_BUILDER_STACKS):
        norm = _normalize_stack(stack)
        root = tmp_path / stack
        for rel, contents in scaffold_for(norm, "demo", "a demo app").items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(contents)
        score, gaps = heuristic_score(root, {"stack": norm})
        assert score >= GO_THRESHOLD, (
            f"{stack}: pristine scaffold scores {score} < {GO_THRESHOLD} — "
            f"the reviewer would no_go every perfect {stack} build: {gaps}"
        )


def test_deliberately_local_sets_keep_their_documented_relationships():
    """Sets that intentionally diverge from the registry stay local — but their
    RELATIONSHIP to the registry is pinned so silent drift still fails."""
    from skyn3t.intelligence import skill_library
    from skyn3t.studio import seo_check

    # seo_check is documented as "deliberately NARROWER" than the UI-web group:
    # exactly the UI stacks minus the non-crawlable ones.
    assert seo_check._SEO_WEB_STACKS == stacks.UI_WEB_STACKS - {
        "tauri", "desktop", "phaser",
    }
    # mcp is a protocol server, never a web/game/design stack.
    assert [n for n, g in stacks.GROUPS.items() if "mcp" in g] == ["mcp"]
    # skill recall must bridge every game token to its alias group.
    for tok in stacks.GAME_STACKS:
        assert len(skill_library._stack_aliases(tok)) > 1, tok
