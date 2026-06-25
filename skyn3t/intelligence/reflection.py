"""Reflection — analyse success/failure and publish KNOWLEDGE_UPDATED.

After a build (or a stage), :class:`Reflector` distils *what to keep* and *what
to change*, emits a ``KNOWLEDGE_UPDATED`` event (design rule #7), and feeds the
durable findings back into the learning loop / skill library.

2.0 P1 GEPA / prompt evolution hook: :meth:`propose_prompt_improvement` takes an
agent's transcripts and returns a candidate instruction *diff*. It is
Pareto-style — a candidate is only suggested if it improves the failing tasks
*without regressing* the passing ones, which the caller validates by re-running
the held-out set before accepting (the recommender returns the candidate plus
the evidence needed to make that decision).

Pure-offline & deterministic: no model is required, though one can be supplied
to enrich the analysis. Zero import side effects (design rule #4, #6).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

try:
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - defensive
    _log = None  # type: ignore[assignment]


@dataclass(slots=True)
class Transcript:
    """One agent run: the instruction used, the input, the output, the outcome."""

    instruction: str
    task_id: str
    output: str = ""
    passed: bool = False
    score: float = 0.0
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Reflection:
    subject: str
    success: bool
    keep: list[str] = field(default_factory=list)
    change: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "success": self.success,
            "keep": self.keep,
            "change": self.change,
            "summary": self.summary,
        }


@dataclass(slots=True)
class PromptCandidate:
    """A GEPA-style proposed instruction change plus the evidence to judge it."""

    agent_name: str
    base_instruction: str
    candidate_instruction: str
    diff: list[str]
    rationale: str
    # Evidence for Pareto acceptance: the caller re-runs these to confirm.
    failing_task_ids: list[str] = field(default_factory=list)
    passing_task_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "diff": self.diff,
            "rationale": self.rationale,
            "failing_task_ids": self.failing_task_ids,
            "passing_task_ids": self.passing_task_ids,
            "candidate_instruction": self.candidate_instruction,
        }


_STOP = {
    "the", "a", "an", "and", "or", "to", "of", "in", "is", "it", "for", "on",
    "with", "this", "that", "was", "were", "be", "as", "at", "by", "i", "you",
}


def _keywords(text: str, top: int = 8) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", (text or "").lower())
    counts = Counter(w for w in words if w not in _STOP)
    return [w for w, _ in counts.most_common(top)]


class Reflector:
    def __init__(self, event_bus: Any | None = None, llm: Any | None = None) -> None:
        self.event_bus = event_bus
        self.llm = llm

    # ---- build / stage reflection -------------------------------------
    def reflect(self, build: dict[str, Any]) -> Reflection:
        """Analyse a build outcome into keep/change findings."""
        stack = str(build.get("stack") or "generic")
        score = build.get("score")
        verdict = str(build.get("verdict") or build.get("status") or "").lower()
        gaps = [str(g) for g in (build.get("gaps") or [])]
        success = ("go" in verdict and "no_go" not in verdict) or (
            isinstance(score, (int, float)) and score >= 80
        )

        keep: list[str] = []
        change: list[str] = []
        if success:
            keep.append(f"{stack}: current approach scored well; preserve plan shape.")
            if build.get("notes"):
                keep.append(f"{stack}: {str(build['notes'])[:120]}")
        else:
            change.append(f"{stack}: outcome was weak; revise the plan.")
        for g in gaps[:5]:
            change.append(f"Close gap: {g[:140]}")

        summary = (
            f"Build for stack '{stack}' "
            f"{'succeeded' if success else 'fell short'}"
            + (f" (score {score:.0f})" if isinstance(score, (int, float)) else "")
            + f"; {len(keep)} to keep, {len(change)} to change."
        )
        return Reflection(
            subject=str(build.get("slug") or build.get("build_id") or stack),
            success=success,
            keep=keep,
            change=change,
            summary=summary,
        )

    async def reflect_and_publish(self, build: dict[str, Any]) -> Reflection:
        ref = self.reflect(build)
        await self._publish(ref)
        return ref

    @staticmethod
    def _suggestions(ref: Reflection) -> list[dict[str, Any]]:
        """Conservative, safe tuning nudges the SelfTuningEngine can apply.

        Free-text findings are NOT mapped to specific numeric knobs (that would
        be guesswork). A build that fell short only enables ``reflective_retry``
        — a safe, reversible, only-ever-turned-on flag — so the loop does
        something useful without risk. A successful build suggests nothing.
        """
        if ref.success:
            return []
        return [
            {
                "agent": None,  # broadcast to every registered agent
                "reason": ref.summary or f"{ref.subject}: build fell short",
                "changes": {"reflective_retry": True},
            }
        ]

    async def _publish(self, ref: Reflection) -> None:
        if self.event_bus is None:
            return
        try:
            from skyn3t.core.events import EventType

            await self.event_bus.emit(
                EventType.KNOWLEDGE_UPDATED,
                source="intelligence.reflection",
                payload={
                    "reflection": ref.to_dict(),
                    # `suggestions` is the key SelfTuningEngine consumes; without
                    # it the reflection was emitted but never drove any tuning.
                    "suggestions": self._suggestions(ref),
                },
            )
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("reflection.emit_failed", error=str(exc))

    # ---- GEPA prompt evolution (2.0 P1) -------------------------------
    def propose_prompt_improvement(
        self,
        agent_name: str,
        transcripts: list[Transcript],
    ) -> PromptCandidate | None:
        """Propose a Pareto-style instruction diff from transcripts.

        Strategy (deterministic, offline):
          1. Split transcripts into passing vs failing for the same instruction.
          2. Mine keywords that distinguish failing outputs (what they got wrong)
             and synthesise corrective clauses to append to the instruction.
          3. Return the candidate *plus* the failing/passing task ids so the
             caller can re-run them and accept only if failing improve and
             passing do NOT regress (the Pareto guard).

        Returns ``None`` when there is nothing to fix (all passing) or no shared
        base instruction.
        """
        if not transcripts:
            return None
        # Group by instruction; operate on the most common base instruction.
        by_instr: dict[str, list[Transcript]] = {}
        for t in transcripts:
            by_instr.setdefault(t.instruction, []).append(t)
        base_instruction, group = max(by_instr.items(), key=lambda kv: len(kv[1]))

        failing = [t for t in group if not t.passed]
        passing = [t for t in group if t.passed]
        if not failing:
            return None  # Nothing to improve — don't risk a regression.

        # What went wrong? Contrast keywords present in failures vs successes.
        fail_kw = set()
        for t in failing:
            fail_kw.update(_keywords(t.output + " " + " ".join(t.tags)))
        pass_kw = set()
        for t in passing:
            pass_kw.update(_keywords(t.output + " " + " ".join(t.tags)))
        distinctive = [k for k in fail_kw - pass_kw][:5]

        clauses: list[str] = []
        if distinctive:
            clauses.append(
                "Pay special attention to: " + ", ".join(distinctive) + "."
            )
        clauses.append(
            "Before answering, verify the output meets every explicit requirement "
            "and self-check edge cases that previously failed."
        )

        candidate = base_instruction.rstrip()
        added = "\n".join(f"- {c}" for c in clauses)
        candidate_instruction = f"{candidate}\n\nAdditional guidance:\n{added}"
        diff = [f"+ {c}" for c in clauses]
        rationale = (
            f"{len(failing)} of {len(group)} runs for '{agent_name}' failed; "
            f"appending corrective guidance targeting {distinctive or 'requirement adherence'}. "
            "Accept only if re-running failing tasks improves them without "
            "regressing the passing tasks (Pareto guard)."
        )
        cand = PromptCandidate(
            agent_name=agent_name,
            base_instruction=base_instruction,
            candidate_instruction=candidate_instruction,
            diff=diff,
            rationale=rationale,
            failing_task_ids=[t.task_id for t in failing],
            passing_task_ids=[t.task_id for t in passing],
        )
        if _log:
            _log.info(
                "reflection.prompt_candidate",
                agent=agent_name,
                failing=len(failing),
                passing=len(passing),
            )
        return cand

    @staticmethod
    def is_pareto_improvement(
        before: dict[str, bool],
        after: dict[str, bool],
        *,
        failing_ids: list[str],
        passing_ids: list[str],
    ) -> bool:
        """Pareto guard: candidate accepted iff some failing task now passes and
        no previously-passing task regresses.

        ``before``/``after`` map task_id -> passed?.
        """
        # No regression among previously-passing tasks.
        for tid in passing_ids:
            if before.get(tid, True) and not after.get(tid, False):
                return False
        # At least one previously-failing task now passes.
        improved = any(
            (not before.get(tid, False)) and after.get(tid, False)
            for tid in failing_ids
        )
        return improved
