"""provider:model slot addressing.

The load-bearing property is backward compatibility: a prefix becomes a provider
ONLY when it names a known provider, so every existing bare model id keeps
meaning "pin this model on the active backend". If that regresses, every
tournament_model_pool / model_override string in the wild changes meaning.
"""

from __future__ import annotations

import pytest

from skyn3t.adapters.model_slot import (
    KNOWN_CLI_PROVIDERS,
    ModelSlot,
    parse_slot,
    parse_slots,
)


@pytest.mark.parametrize(
    ("raw", "provider", "model"),
    [
        ("codex_cli", "codex_cli", ""),
        ("codex", "codex_cli", ""),  # bare CLI name is accepted
        ("claude-cli", "claude_cli", ""),  # dash spelling normalises
        ("claude_cli:sonnet", "claude_cli", "sonnet"),
        ("openrouter:deepseek/deepseek-chat", "openrouter", "deepseek/deepseek-chat"),
        ("openrouter", "openrouter", ""),
        ("stub", "stub", ""),
        ("  kimi_cli : k2  ", "kimi_cli", "k2"),
    ],
)
def test_known_provider_prefix_is_split(raw, provider, model):
    slot = parse_slot(raw)

    assert (slot.provider, slot.model) == (provider, model)


@pytest.mark.parametrize(
    "raw",
    [
        "deepseek/deepseek-chat",
        "anthropic/claude-sonnet-4",
        "meta-llama/llama-4-70b:free",
        "foo:bar",
        "gpt-5.5",
    ],
)
def test_unknown_prefix_stays_a_whole_model_id(raw):
    # This is the backward-compatibility guarantee: provider "" means "pin this
    # model on the active backend", which is exactly today's model_override.
    slot = parse_slot(raw)

    assert slot.provider == ""
    assert slot.model == raw.strip()


def test_auto_is_not_addressable_as_a_slot():
    # Two slots resolving through "auto" would silently be the same model, which
    # defeats the entire point of a multi-provider fan-out.
    slot = parse_slot("auto")

    assert slot.provider == ""
    assert slot.model == "auto"


def test_address_round_trips():
    for raw in ("claude_cli:sonnet", "openrouter:deepseek/deepseek-chat", "codex_cli", "gpt-5.5"):
        assert parse_slot(parse_slot(raw).address).address == parse_slot(raw).address


def test_parse_slots_splits_and_drops_blanks():
    slots = parse_slots("claude_cli:sonnet, ,openrouter:x/y ,,codex_cli")

    assert [s.address for s in slots] == [
        "claude_cli:sonnet",
        "openrouter:x/y",
        "codex_cli",
    ]


def test_parse_slots_accepts_an_iterable_and_none():
    assert parse_slots(None) == []
    assert parse_slots([]) == []
    assert [s.address for s in parse_slots(["codex_cli", "openrouter:x/y"])] == [
        "codex_cli",
        "openrouter:x/y",
    ]


def test_empty_slot_is_dropped_not_raised():
    assert parse_slots("   ,  ,") == []
    assert parse_slot("").is_empty is True


def test_cli_provider_list_matches_the_adapter():
    # model_slot may not import llm (llm imports it), so the list is duplicated.
    # Pin them together so the duplication cannot drift.
    from skyn3t.adapters.llm import KNOWN_CLI_PROVIDERS as adapter_providers

    assert set(KNOWN_CLI_PROVIDERS) == set(adapter_providers)


def test_slot_is_hashable_and_frozen():
    slot = ModelSlot(provider="codex_cli", model="x")

    # Hashable so slots can be de-duplicated; frozen so a slot handed to a
    # fan-out cannot be mutated underneath a concurrent advisor.
    assert {slot, ModelSlot(provider="codex_cli", model="x")} == {slot}
    with pytest.raises(AttributeError):
        slot.provider = "other"  # type: ignore[misc]
