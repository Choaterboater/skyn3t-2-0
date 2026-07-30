"""Per-model timeout FLOORS for slow-thinking reasoning models.

``_openrouter`` hard-codes ``httpx.AsyncClient(timeout=120)`` and the agentic
loop uses 180. A reasoning model can spend longer than that silently thinking
before emitting its first token, so the request dies mid-think, surfaces as a
transport timeout, gets classified transient, and burns every retry the same
way. That was invisible while the codebase was effectively single-provider; it
becomes a routine failure the moment a Mixture-of-Agents council can put an
``openrouter:openai/o3`` or ``deepseek-r1`` advisor next to a fast one.

Two ideas adapted from NousResearch/hermes-agent (MIT),
``agent/reasoning_timeouts.py``:

* the FLOOR semantic — applied as ``max(configured, floor)``, so this can only
  ever raise a timeout, never shorten one an operator set deliberately;
* the anchored matching discipline — a family matches only at a slug boundary,
  so ``openai/o3-mini`` matches ``o3`` family rules but
  ``meta/llama-4-70b-o1`` does NOT match ``o1``.

Hermes ships ~40 entries covering NVIDIA NIM slugs this repo will never route
to; the table here is deliberately small and covers only families reachable from
:mod:`skyn3t.core.model_router` defaults and the free pool.

Pure stdlib, import has zero side effects.
"""

from __future__ import annotations

import re

#: ``(family regex, floor seconds)``. The regex is matched against the model slug
#: with any ``vendor/`` prefix stripped, anchored at the start, and required to
#: end at a slug boundary (end-of-string or one of ``-``, ``.``, ``_``, ``:``).
#:
#: Ordered current-generation first. Model names churn fast, so this table is a
#: best-effort accelerator, not a registry: an unlisted model simply keeps the
#: caller's configured timeout (``reasoning_timeout_floor`` returns ``None``),
#: which is the pre-existing behaviour. Verify against the live catalog
#: (``GET https://openrouter.ai/api/v1/models``) rather than trusting this list.
_REASONING_FLOORS: tuple[tuple[str, int], ...] = (
    # --- current generation (verified against the OpenRouter catalog, 2026-07) ---
    # GPT-5.6 family: terra / luna / sol, each with a -pro variant. Codex CLI
    # commonly runs these at high reasoning effort, where a silent think phase
    # comfortably exceeds a flat 120s.
    (r"gpt-5\.\d+", 900),
    (r"gpt-5", 600),
    (r"claude-opus-5", 900),
    (r"claude-sonnet-5", 600),
    (r"claude-haiku-5", 300),
    (r"kimi-k3", 600),
    (r"k3", 600),  # the Kimi CLI's own alias
    (r"grok-4", 480),
    (r"qwen3\.\d+", 480),
    (r"gemini-3\.\d+-pro", 600),
    (r"longcat-2", 480),
    (r"laguna-s", 480),
    # --- previous generations, kept for hosts still pinned to them ---
    (r"o1", 600),
    (r"o3", 600),
    (r"o4", 600),
    (r"deepseek-r1", 600),
    (r"deepseek-reasoner", 600),
    (r"qwq", 480),
    (r"qwen3", 480),
    (r"claude-opus-4", 480),
    (r"claude-sonnet-4", 300),
    (r"glm-4[^/]*-thinking", 480),
    (r"grok-[0-9]+-reasoning", 480),
)

_COMPILED: tuple[tuple[re.Pattern[str], int], ...] = tuple(
    (re.compile(rf"^{pattern}(?=$|[-._:])", re.IGNORECASE), floor)
    for pattern, floor in _REASONING_FLOORS
)


def reasoning_timeout_floor(model: str) -> int | None:
    """Minimum seconds a request to ``model`` should be allowed, or ``None``.

    ``None`` means "no opinion" — the caller keeps whatever it already had.
    Never raises.
    """
    slug = str(model or "").strip().lower()
    if not slug:
        return None
    # Strip an aggregator/vendor prefix ("openai/o3", "deepseek/deepseek-r1").
    slug = slug.rsplit("/", 1)[-1]
    best: int | None = None
    for pattern, floor in _COMPILED:
        if pattern.match(slug) and (best is None or floor > best):
            best = floor
    return best


def apply_reasoning_floor(timeout: float | int, model: str) -> int:
    """``max(timeout, floor(model))`` as an int. Never lowers ``timeout``."""
    try:
        base = int(timeout)
    except (TypeError, ValueError):
        base = 0
    floor = reasoning_timeout_floor(model)
    return max(base, floor) if floor else base
