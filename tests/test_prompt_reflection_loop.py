"""Phase B / B2: the factory proposes its own instruction fixes from build outcomes.

PromptReflectionLoop accumulates per-agent win/fail transcripts and, when an agent
shows both, asks the Reflector for a Pareto-style instruction improvement and submits
it as a GATED proposal (never auto-applied).
"""

from __future__ import annotations

import asyncio

from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.components import PromptReflectionLoop


class _FakeCortex:
    def __init__(self):
        self.proposals = []

    async def submit(self, proposal):
        self.proposals.append(proposal)


async def _stage(bus, *, score, gaps=None, build_id="b"):
    payload = {
        "build_id": build_id, "stage": "code", "capability": "code",
        "status": "completed", "score": score,
    }
    if gaps:
        payload["gaps"] = gaps
    await bus.emit(EventType.BUILD_STAGE_COMPLETED, "studio", payload)


def test_proposes_instruction_improvement_on_mixed_outcomes():
    async def go():
        bus = EventBus()
        cortex = _FakeCortex()
        comp = PromptReflectionLoop(cortex, bus, min_each=2)
        await comp.run()

        await _stage(bus, score=95)
        await _stage(bus, score=90)                              # passing
        await _stage(bus, score=20, gaps=["no recognizable entrypoint"])
        await _stage(bus, score=30, gaps=["python syntax error"])  # failing
        await bus.emit(EventType.BUILD_COMPLETED, "studio", {"build_id": "b", "verdict": "no_go"})

        assert cortex.proposals, "expected a gated instruction-improvement proposal"
        p = cortex.proposals[-1]
        assert p.safe is False  # gated — never auto-applied
        assert p.payload["agent"] == "code"
        assert "prompt_candidate" in p.payload
        assert p.payload["prompt_candidate"]["agent_name"] == "code"

    asyncio.run(go())


def test_no_proposal_when_all_passing():
    async def go():
        bus = EventBus()
        cortex = _FakeCortex()
        comp = PromptReflectionLoop(cortex, bus, min_each=2)
        await comp.run()
        for s in (95, 90, 88, 92):  # all passing -> nothing to fix
            await _stage(bus, score=s)
        await bus.emit(EventType.BUILD_COMPLETED, "studio", {"build_id": "b"})
        assert not cortex.proposals

    asyncio.run(go())


def test_dedupes_per_agent():
    async def go():
        bus = EventBus()
        cortex = _FakeCortex()
        comp = PromptReflectionLoop(cortex, bus, min_each=2)
        await comp.run()
        await _stage(bus, score=95)
        await _stage(bus, score=90)
        await _stage(bus, score=20, gaps=["no entrypoint"])
        await _stage(bus, score=25, gaps=["syntax error"])
        await bus.emit(EventType.BUILD_COMPLETED, "studio", {"build_id": "b"})
        await bus.emit(EventType.BUILD_COMPLETED, "studio", {"build_id": "b2"})
        assert len(cortex.proposals) == 1  # one proposal per agent, not spammed

    asyncio.run(go())


def test_maybe_propose_survives_concurrent_buf_mutation():
    # Reviewer finding: _maybe_propose awaits cortex.submit() mid-loop; a
    # concurrent build event mutating self._buf during the await must not raise
    # "dictionary changed size during iteration" (which skipped later agents).
    async def go():
        bus = EventBus()

        class _MutatingCortex:
            def __init__(self):
                self.proposals = []

            async def submit(self, p):
                self.proposals.append(p)
                # Simulate a concurrent on_stage adding a new _buf key mid-await.
                comp._buf.setdefault("brainstorm", [])

        cortex = _MutatingCortex()
        comp = PromptReflectionLoop(cortex, bus, min_each=2)
        t = comp._Transcript

        def mixed(agent):
            return [
                t(
                    instruction=f"agent:{agent}",
                    task_id=f"{agent}:{i}",
                    output=("clean build" if i < 2 else f"{agent} failed missing broken entrypoint"),
                    passed=(i < 2),
                    score=(90.0 if i < 2 else 20.0),
                    tags=[agent],
                )
                for i in range(4)
            ]

        # Two qualifying agents so the loop must iterate PAST the first (which
        # triggers the mutation). The bug skipped everything after the first.
        comp._buf["code"] = mixed("code")
        comp._buf["design"] = mixed("design")

        await comp._maybe_propose()  # must NOT raise

        agents = {p.payload["agent"] for p in cortex.proposals}
        assert agents == {"code", "design"}  # both processed despite the mutation

    asyncio.run(go())
