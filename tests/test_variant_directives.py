"""Variant codegen directives: model-generated variants hear their contracts.

The scaffold variants (§3.6/3.7/3.9/3.10) carry contracts their gates and
tests enforce — but a MODEL-generated build only learns a contract from its
prompt. ``variant_directive(stack, brief)`` (pure; the same triggers as
``scaffold_for``) selects the compact contract clause the codegen prompt
injects — one good clause is worth many repairs.
"""

from __future__ import annotations

from skyn3t.agents.code_agent import variant_directive


def test_each_variant_brief_gets_its_contract():
    cases = (
        ("fastapi", "a stock api with price history", ("NO_DATA", "DATA_VENDOR")),
        ("fastapi", "an llm gateway with cost tracking",
         ("ALL_UPSTREAMS_FAILED", "internal:", "usage ledger")),
        ("fastapi", "a paper trading simulator with a portfolio dashboard",
         ("PAPER-ONLY", "NO_DATA", "as_of", "Decimal")),
        ("rag", "a chatbot that remembers me", ("MEMORY_FILE", "AFTER its own")),
        ("python_cli", "a terminal assistant for my notes",
         ("workspace-write", "session_start", "denied")),
    )
    for stack, brief, tokens in cases:
        directive = variant_directive(stack, brief)
        assert directive, (stack, brief)
        for token in tokens:
            assert token in directive, (stack, brief, token)


def test_base_briefs_get_no_variant_directive():
    for stack, brief in (
        ("fastapi", "a rest api for my todo app"),
        ("rag", "a rag app to chat with my documents"),
        ("python_cli", "a command line todo manager"),
        ("workflow", "an agent that sends briefings"),  # workflow has no variants
    ):
        assert variant_directive(stack, brief) == "", (stack, brief)


def test_triggers_are_stack_scoped():
    # A gateway phrase on the WRONG stack must not inject the gateway contract.
    assert variant_directive("rag", "an llm gateway with cost tracking") == ""
    assert variant_directive("fastapi", "a chatbot that remembers me") == ""
