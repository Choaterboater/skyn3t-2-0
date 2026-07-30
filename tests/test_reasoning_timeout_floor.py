"""Reasoning-model timeout floors.

_openrouter hard-codes httpx timeout=120. A model that thinks silently for
longer dies mid-think, surfaces as a transport timeout, is classified transient,
and burns every retry identically. Floors raise the ceiling for known slow
families without ever shortening a timeout an operator set deliberately.
"""

from __future__ import annotations

import pytest

from skyn3t.adapters.reasoning_timeouts import (
    apply_reasoning_floor,
    reasoning_timeout_floor,
)


@pytest.mark.parametrize(
    "model",
    [
        "openai/o1",
        "openai/o3",
        "openai/o3-mini",
        "o4-mini",
        "deepseek/deepseek-r1",
        "deepseek-reasoner",
        "qwen/qwq-32b",
        "qwen3-235b",
        "anthropic/claude-opus-4",
        "z-ai/glm-4.6-thinking",
    ],
)
def test_known_reasoning_slugs_get_a_floor(model):
    floor = reasoning_timeout_floor(model)

    assert floor is not None and floor >= 300


@pytest.mark.parametrize(
    "model",
    [
        "",
        "gpt-4o-mini",
        "anthropic/claude-haiku-4-5",
        "meta-llama/llama-3-70b",
        "mistralai/mistral-small",
    ],
)
def test_ordinary_models_have_no_opinion(model):
    assert reasoning_timeout_floor(model) is None


@pytest.mark.parametrize(
    "model",
    [
        "meta/llama-4-70b-o1",  # 'o1' appears, but not at a slug boundary
        "someone/foo-o3",
        "octo3",
        "no1",
    ],
)
def test_slug_anchoring_rejects_an_embedded_match(model):
    # Without start-anchoring + boundary matching, every model with "o1" or "o3"
    # anywhere in its name would get a 10-minute timeout.
    assert reasoning_timeout_floor(model) is None


def test_vendor_prefix_is_stripped_before_matching():
    assert reasoning_timeout_floor("openai/o3") == reasoning_timeout_floor("o3")


def test_floor_never_lowers_a_larger_configured_timeout():
    assert apply_reasoning_floor(900, "openai/o3") == 900
    assert apply_reasoning_floor(120, "openai/o3") == 600
    assert apply_reasoning_floor(120, "gpt-4o-mini") == 120


def test_apply_floor_tolerates_bad_input():
    assert apply_reasoning_floor("nonsense", "gpt-4o-mini") == 0  # type: ignore[arg-type]
    assert apply_reasoning_floor(None, "openai/o3") == 600  # type: ignore[arg-type]
