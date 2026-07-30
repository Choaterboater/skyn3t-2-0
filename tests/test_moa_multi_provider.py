"""Multi-provider fan-out: the fix for debate-as-self-play.

Before slots, DebateOrchestrator._complete passed no model/provider override, so
all N "debaters" resolved through the same tier to the SAME model. Debate was one
model arguing with itself, voting on itself, and feeding that into Elo as if it
were independent evidence.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.adapters.model_slot import parse_slots
from skyn3t.intelligence.debate import DebateOrchestrator, run_debate


class _RecordingLLM:
    """Records the (provider_override, model_override) of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, prompt, tier=None, *, system=None, max_tokens=1024,
                       model_override=None, provider_override=None, **_kw):
        self.calls.append((provider_override or "", model_override or ""))
        label = f"{provider_override or 'routed'}:{model_override or 'default'}"
        # A parseable vote so the debate reaches a winner deterministically.
        return SimpleNamespace(text="Proposal 1", model=label, backend="stub")

    @property
    def routes(self) -> list[tuple[str, str]]:
        return sorted(set(self.calls))


def test_debate_fans_out_across_distinct_provider_slots():
    llm = _RecordingLLM()
    orch = DebateOrchestrator(
        llm, slots=parse_slots("claude_cli:sonnet,openrouter:vendor/m,codex_cli")
    )

    asyncio.run(orch.run("which cache strategy?"))

    proposal_routes = llm.calls[:3]
    assert proposal_routes == [
        ("claude_cli", "sonnet"),
        ("openrouter", "vendor/m"),
        ("codex_cli", ""),
    ]
    # Three genuinely different providers took part.
    assert len({p for p, _ in proposal_routes}) == 3


def test_debate_without_slots_is_unchanged():
    # Backward-compatibility fence: existing callers (the `skyn3t debate` CLI)
    # pass no slots and must keep today's single-model behaviour exactly.
    llm = _RecordingLLM()
    orch = DebateOrchestrator(llm, num_debaters=3)

    asyncio.run(orch.run("which cache strategy?"))

    assert all(call == ("", "") for call in llm.calls)


def test_slot_count_drives_the_debater_count():
    llm = _RecordingLLM()
    orch = DebateOrchestrator(llm, num_debaters=3, slots=parse_slots("codex_cli,stub"))

    assert orch.num_debaters == 2
    asyncio.run(orch.run("q"))
    assert llm.calls[:2] == [("codex_cli", ""), ("stub", "")]


def test_slots_are_assigned_round_robin_when_debaters_outnumber_slots():
    llm = _RecordingLLM()
    orch = DebateOrchestrator(llm, slots=parse_slots("codex_cli"))

    asyncio.run(orch.run("q"))

    # One slot -> still at least two debaters, both on that slot. Degenerate but
    # honest: it is the operator's configuration, not a silent substitution.
    assert llm.calls[0] == ("codex_cli", "")


def test_run_debate_accepts_a_raw_slot_string():
    llm = _RecordingLLM()

    asyncio.run(run_debate(llm, "q", slots="codex_cli,openrouter:vendor/m"))

    assert llm.calls[:2] == [("codex_cli", ""), ("openrouter", "vendor/m")]


def test_a_failed_debater_does_not_shift_later_slots():
    """Compacting failed proposals must not reassign survivors to wrong slots."""

    class _FlakyLLM(_RecordingLLM):
        async def complete(self, prompt, tier=None, *, system=None, max_tokens=1024,
                           model_override=None, provider_override=None, **_kw):
            if provider_override == "openrouter" and "debater #2" in prompt:
                raise RuntimeError("provider down")
            return await super().complete(
                prompt, tier, system=system, max_tokens=max_tokens,
                model_override=model_override, provider_override=provider_override,
            )

    llm = _FlakyLLM()
    orch = DebateOrchestrator(
        llm, slots=parse_slots("claude_cli:sonnet,openrouter:vendor/m,codex_cli")
    )

    result = asyncio.run(orch.run("q"))

    assert len(result.proposals) == 2  # the openrouter debater dropped out
    # The failing call raises before recording, so only 2 proposal calls land.
    assert llm.calls[:2] == [("claude_cli", "sonnet"), ("codex_cli", "")]
    # The critique round must address the two SURVIVING slots. Naively indexing
    # by position would have put the survivors on slots 0 and 1 — i.e. moved the
    # codex debater onto the (dead) openrouter route.
    assert llm.calls[2:4] == [("claude_cli", "sonnet"), ("codex_cli", "")]


def test_distinct_slots_produce_distinct_tournament_competitors(tmp_path):
    from skyn3t.intelligence.model_tournament import ModelTournament

    tournament = ModelTournament(tmp_path / "t.json")
    llm = _RecordingLLM()
    orch = DebateOrchestrator(
        llm,
        tournament=tournament,
        slots=parse_slots("claude_cli:sonnet,openrouter:vendor/m,codex_cli"),
    )

    asyncio.run(orch.run("q", task_type="debate"))

    board = {s.model for s in tournament.leaderboard(tournament.bucket_key("cheap", "debate"))}
    # Three separate rows. Before slots this was one model against itself, so a
    # single competitor accumulated an unopposed win every round.
    assert len(board) == 3
