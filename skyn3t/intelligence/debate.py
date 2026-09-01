"""Flag-gated multi-LLM debate (2.0).

Pipeline: several cheap models *propose* -> *cross-examine* each other ->
*vote* -> a synthesiser merges the winning ideas. The winner of the vote is
recorded into the :class:`ModelTournament` so debate feeds model evolution.

``slots`` is what makes this MULTI-model. Without it every debater resolved
through the same tier to the same model, so "debate" was one model arguing with
itself, voting on itself, and feeding that self-play into Elo as if it were
evidence. Each slot is a concrete ``provider:model`` route
(:mod:`skyn3t.adapters.model_slot`), and debaters are assigned round-robin.

Gated by ``Settings.debate_enabled`` at the call site; :func:`run_debate`
itself also accepts ``enabled`` and short-circuits to a single completion when
debate is off (cheap-by-default, design rule #5). Uses the pinned ``LLMClient``
contract; with the stub backend it still runs end-to-end deterministically so
the loop is testable offline (design rule #6).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

try:
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - defensive
    _log = None  # type: ignore[assignment]

from skyn3t.adapters.model_slot import ModelSlot
from skyn3t.core.model_router import Tier


@dataclass(slots=True)
class Proposal:
    debater: str
    model: str
    text: str
    votes: int = 0
    critiques: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DebateResult:
    question: str
    winner: Proposal | None
    synthesis: str
    proposals: list[Proposal] = field(default_factory=list)
    rounds: int = 0
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "winner_model": self.winner.model if self.winner else None,
            "synthesis": self.synthesis,
            "proposals": [
                {"debater": p.debater, "model": p.model, "votes": p.votes}
                for p in self.proposals
            ],
            "rounds": self.rounds,
            "enabled": self.enabled,
        }


_VOTE_RE = re.compile(r"\b(?:proposal|debater|#)\s*([0-9]+)", re.IGNORECASE)


def _parse_vote(text: str, n: int) -> int | None:
    """Best-effort parse of a 1-based proposal index from a vote message.

    Returns None when no valid in-range index can be extracted — an unparseable
    or out-of-range vote must NOT default to proposals[0], which silently biased
    the debate winner toward the first proposal."""
    m = _VOTE_RE.search(text or "")
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < n:
            return idx
    # Fallback: first standalone integer in range.
    for tok in re.findall(r"\d+", text or ""):
        idx = int(tok) - 1
        if 0 <= idx < n:
            return idx
    return None


class DebateOrchestrator:
    """Runs the propose -> cross-examine -> vote -> synthesise loop."""

    def __init__(
        self,
        llm: Any,
        *,
        tournament: Any | None = None,
        event_bus: Any | None = None,
        num_debaters: int = 3,
        tier: Tier = Tier.CHEAP,
        slots: list[ModelSlot] | None = None,
    ) -> None:
        self.llm = llm
        self.tournament = tournament
        self.event_bus = event_bus
        self.tier = tier
        # Without slots every "debater" resolved through the SAME tier to the
        # SAME model: one model arguing with itself, voting on itself, and
        # feeding that self-play into the Elo tournament. Slots make each
        # debater a concrete provider:model route, which is what makes this an
        # ensemble rather than N samples of one model.
        self.slots = [s for s in (slots or []) if not s.is_empty]
        self.num_debaters = max(2, len(self.slots) or num_debaters)

    def _slot_for(self, index: int) -> ModelSlot | None:
        if not self.slots:
            return None
        return self.slots[index % len(self.slots)]

    async def _complete(
        self, prompt: str, *, system: str | None = None, slot: ModelSlot | None = None
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if slot is not None:
            if slot.provider:
                kwargs["provider_override"] = slot.provider
            if slot.model:
                kwargs["model_override"] = slot.model
        return await self.llm.complete(
            prompt, tier=self.tier, system=system, max_tokens=1024, **kwargs
        )

    async def run(
        self,
        question: str,
        *,
        context: str = "",
        task_type: str = "debate",
        enabled: bool = True,
    ) -> DebateResult:
        if not enabled:
            res = await self._complete(question)
            return DebateResult(
                question=question,
                winner=Proposal("solo", res.model, res.text, votes=1),
                synthesis=res.text,
                proposals=[Proposal("solo", res.model, res.text, votes=1)],
                rounds=0,
                enabled=False,
            )

        # 1) Propose (in parallel).
        prop_prompts = [
            f"{context}\n\nQuestion: {question}\n\n"
            f"You are debater #{i + 1}. Give your best concise answer and reasoning."
            for i in range(self.num_debaters)
        ]
        prop_results = await asyncio.gather(
            *(
                self._complete(p, slot=self._slot_for(i))
                for i, p in enumerate(prop_prompts)
            ),
            return_exceptions=True,
        )
        proposals: list[Proposal] = []
        # ``slot`` records WHICH route produced this proposal, so a failed
        # debater does not shift every later proposal onto the wrong slot when
        # results are compacted.
        slot_by_proposal: list[ModelSlot | None] = []
        for i, r in enumerate(prop_results):
            if isinstance(r, BaseException):
                continue
            proposals.append(Proposal(f"debater-{i + 1}", r.model, r.text))
            slot_by_proposal.append(self._slot_for(i))
        if not proposals:
            return DebateResult(question, None, "", [], rounds=0)

        # 2) Cross-examine: each debater critiques the others.
        joined = "\n\n".join(
            f"Proposal #{i + 1} ({p.debater}):\n{p.text}" for i, p in enumerate(proposals)
        )
        critique_prompts = [
            f"Proposals under debate:\n{joined}\n\n"
            f"As debater #{i + 1}, point out the single biggest flaw in the OTHER "
            f"proposals."
            for i in range(len(proposals))
        ]
        crit_results = await asyncio.gather(
            *(
                self._complete(p, slot=slot_by_proposal[i])
                for i, p in enumerate(critique_prompts)
            ),
            return_exceptions=True,
        )
        for i, r in enumerate(crit_results):
            if not isinstance(r, BaseException) and i < len(proposals):
                proposals[i].critiques.append(r.text)

        # 3) Vote: each debater votes for the strongest proposal (not its own).
        crit_blob = "\n\n".join(
            f"Critique from {proposals[i].debater}: {c}"
            for i, p in enumerate(proposals)
            for c in p.critiques
        )
        vote_prompts = [
            f"Proposals:\n{joined}\n\nCritiques:\n{crit_blob}\n\n"
            f"Vote for the strongest proposal by number (e.g. 'Proposal 2'). "
            f"Reply with just the number."
            for _ in proposals
        ]
        vote_results = await asyncio.gather(
            *(
                self._complete(p, slot=slot_by_proposal[i])
                for i, p in enumerate(vote_prompts)
            ),
            return_exceptions=True,
        )
        for r in vote_results:
            if isinstance(r, Exception):
                continue
            idx = _parse_vote(getattr(r, "text", ""), len(proposals))
            if idx is not None:  # drop unparseable/out-of-range votes (no bias)
                proposals[idx].votes += 1

        winner = max(proposals, key=lambda p: p.votes)
        total_votes = sum(p.votes for p in proposals)

        # 4) Synthesise the winning ideas.
        if total_votes:
            synth_prompt = (
                f"Question: {question}\n\nDebated proposals:\n{joined}\n\n"
                f"The debaters favored proposal by '{winner.debater}'. Synthesise the "
                f"best final answer, incorporating valid points from the others."
            )
        else:
            # No parseable votes — max() returns proposals[0], but no one actually
            # favored it. Don't pretend a winner emerged.
            synth_prompt = (
                f"Question: {question}\n\nDebated proposals:\n{joined}\n\n"
                "The debaters did not reach a clear consensus. Synthesise the best "
                "final answer, weighing all proposals equally."
            )
        try:
            synth = await self._complete(synth_prompt)
            synthesis = synth.text
        except Exception:  # noqa: BLE001
            synthesis = winner.text

        result = DebateResult(
            question=question,
            winner=winner,
            synthesis=synthesis,
            proposals=proposals,
            rounds=3,
        )

        # 5) Record to the tournament (feeds model evolution) — ONLY when voters
        # expressed a real preference. With zero parseable votes, winner is an
        # arbitrary proposals[0]; recording it would bias model evolution toward
        # the first proposal — the very bias _parse_vote removes at the vote level.
        if self.tournament is not None and total_votes > 0:
            try:
                bucket = self.tournament.bucket_key(self.tier, task_type)
                losers = [p.model for p in proposals if p is not winner]
                self.tournament.record_win(
                    bucket, winner.model, losers, task_type=task_type
                )
            except Exception as exc:  # noqa: BLE001
                if _log:
                    _log.warning("debate.tournament_failed", error=str(exc))

        await self._emit(result)
        return result

    async def _emit(self, result: DebateResult) -> None:
        if self.event_bus is None:
            return
        try:
            from skyn3t.core.events import EventType

            await self.event_bus.emit(
                EventType.INSIGHT_PUBLISHED,
                source="intelligence.debate",
                payload={"debate": result.to_dict()},
            )
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("debate.emit_failed", error=str(exc))


async def run_debate(
    llm: Any,
    question: str,
    *,
    enabled: bool = True,
    tournament: Any | None = None,
    event_bus: Any | None = None,
    context: str = "",
    task_type: str = "debate",
    num_debaters: int = 3,
    slots: list[ModelSlot] | str | None = None,
) -> DebateResult:
    """Convenience wrapper around :class:`DebateOrchestrator`.

    ``slots`` accepts either parsed :class:`ModelSlot` objects or a raw
    comma-separated ``provider:model`` string. Omitted, every debater runs on the
    same routed model (the historical self-play behaviour), so existing callers
    are unaffected.
    """
    if isinstance(slots, str):
        from skyn3t.adapters.model_slot import parse_slots

        parsed = parse_slots(slots)
    else:
        parsed = list(slots or [])
    orch = DebateOrchestrator(
        llm,
        tournament=tournament,
        event_bus=event_bus,
        num_debaters=num_debaters,
        slots=parsed,
    )
    return await orch.run(
        question, context=context, task_type=task_type, enabled=enabled
    )
