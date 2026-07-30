"""``provider:model`` slot addressing for multi-provider fan-out.

Until now a model was addressed as a bare id and routed through whatever backend
was globally active, so every parallel "competitor" — best-of-N trajectories,
debate proposals — collapsed onto ONE backend. Debate in particular was
self-play: N debaters, one model, arguing with itself. A slot names the provider
AND the model, which is what lets a Mixture-of-Agents council fan out across
``claude_cli`` + ``openrouter`` + ``codex_cli`` simultaneously.

Backward compatibility is the load-bearing property of the grammar: a prefix is
consumed as a provider ONLY when it is a known provider. So ``claude_cli:sonnet``
splits, but ``deepseek/deepseek-chat`` and ``foo:bar`` stay whole model ids with
an empty provider, meaning "pin this model on the active backend" — exactly the
semantics every existing ``tournament_model_pool`` / ``model_override`` string
already has.

This module is pure stdlib and must NOT import :mod:`skyn3t.adapters.llm`
(``llm`` imports this). Import has zero side effects.

The provider+model-pair-per-slot idea is adapted from NousResearch/hermes-agent
(MIT), ``hermes_cli/moa_config.py:14-22`` and ``:194-222``, which models MoA
reference slots as ``{"provider": ..., "model": ...}`` dicts. The flat string
encoding here is this repo's own: settings are flat scalar Pydantic fields with
``env_prefix="SKYN3T_"``, and ``tournament_model_pool`` is already a comma-list,
so a comma-list of ``provider:model`` stays env-settable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: Headless CLI providers. Duplicated from :mod:`skyn3t.adapters.llm` on purpose
#: — that module imports this one, so the dependency may only point one way.
#: ``llm`` re-exports its own name, and a drift test pins the two together.
KNOWN_CLI_PROVIDERS: tuple[str, ...] = ("codex", "claude", "copilot", "kimi")

#: Everything addressable as a slot provider. ``auto`` is deliberately absent: a
#: slot names a CONCRETE route, and "whatever auto resolves to" would make two
#: slots silently the same model — the exact bug this module exists to prevent.
KNOWN_SLOT_PROVIDERS: frozenset[str] = frozenset(
    {"openrouter", "stub", *(f"{p}_cli" for p in KNOWN_CLI_PROVIDERS)}
)


@dataclass(frozen=True, slots=True)
class ModelSlot:
    """One concrete route: a provider and, optionally, a model pinned on it."""

    provider: str = ""  # "" -> inherit the active backend
    model: str = ""  # "" -> that provider's default / router-resolved

    @property
    def address(self) -> str:
        """Canonical ``provider:model`` text. Round-trips through ``parse_slot``."""
        if self.provider and self.model:
            return f"{self.provider}:{self.model}"
        return self.provider or self.model

    @property
    def is_empty(self) -> bool:
        return not self.provider and not self.model

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model, "address": self.address}


def _normalize_provider(raw: str) -> str:
    """Accept ``claude``/``claude_cli``/``claude-cli`` for a CLI provider."""
    token = str(raw or "").strip().lower().replace("-", "_")
    if not token:
        return ""
    if token in KNOWN_SLOT_PROVIDERS:
        return token
    if token in KNOWN_CLI_PROVIDERS:
        return f"{token}_cli"
    return ""


def parse_slot(raw: str) -> ModelSlot:
    """Parse one ``provider:model`` / ``provider`` / ``model`` token.

    Splits on the FIRST colon only, and only when the left side names a known
    provider — otherwise the whole token is a model id. That keeps OpenRouter
    ids (``deepseek/deepseek-chat``) and any colon-bearing vendor id intact.
    """
    text = str(raw or "").strip()
    if not text:
        return ModelSlot()
    if ":" in text:
        head, _, tail = text.partition(":")
        provider = _normalize_provider(head)
        if provider:
            return ModelSlot(provider=provider, model=tail.strip())
        return ModelSlot(model=text)
    provider = _normalize_provider(text)
    if provider:
        return ModelSlot(provider=provider)
    return ModelSlot(model=text)


def parse_slots(raw: str | Iterable[str] | None) -> list[ModelSlot]:
    """Parse a comma-separated string (or iterable) into slots.

    Blank and unparseable entries are dropped rather than raising: a typo in one
    advisor slot must not take a build down with it.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        tokens: list[str] = raw.split(",")
    else:
        tokens = []
        for item in raw:
            tokens.extend(str(item).split(","))
    out: list[ModelSlot] = []
    for token in tokens:
        slot = parse_slot(token)
        if not slot.is_empty:
            out.append(slot)
    return out


#: Deliberately NOT provided: a ``canonical_competitor()`` that namespaces
#: tournament ids as ``openrouter:<vendor>/<model>``. Competitor ids are already
#: unambiguous — ``LLMClient._cli`` labels CLI results ``<provider>-cli[:model]``
#: and OpenRouter ids always carry a ``vendor/`` prefix — so namespacing would
#: rewrite every existing Elo key to solve a collision that cannot occur.
