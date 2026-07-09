"""Prompt evolver — GEPA-style base-prompt evolution as gated proposals (P1).

GEPA (Genetic-Pareto / reflective prompt evolution) improves a base prompt by:
  1. running the current prompt against a set of tasks,
  2. reflecting on the traces/failures to find weaknesses,
  3. proposing mutated prompt candidates,
  4. keeping candidates that Pareto-dominate on the chosen metrics.

Here we implement a lightweight, dependency-free version of that loop. A real
deployment supplies an LLM + an evaluator; offline, we use deterministic
reflection heuristics so the loop still produces sensible, testable mutations
(design rules #1 "delivered != empty" and #6 "degrade, don't crash").

Crucially, an evolved prompt is never adopted automatically: each candidate is
emitted as a GATED :class:`ProposalType.TUNING` proposal carrying the new
prompt text, so a human (or the gate) decides whether to adopt it (rule #4).

Import is side-effect free.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.events import EventBus
from skyn3t.cortex.proposal_store import Proposal, ProposalType


def _text_hash(text: str) -> str:
    """Stable content hash for dedupe keys. The builtin hash() is randomized per
    process (PYTHONHASHSEED), so a persisted dedupe_key would not match the same
    text after a restart — breaking duplicate detection. sha256 is deterministic."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

# An evaluator scores a prompt in [0,1] given a list of task dicts.
Evaluator = Callable[[str, list[dict[str, Any]]], Awaitable[float]]


@dataclass
class PromptCandidate:
    """A single prompt variant and its measured fitness."""

    text: str
    score: float = 0.0
    generation: int = 0
    parent: str | None = None
    notes: str = ""

    def dominates(self, other: PromptCandidate) -> bool:
        # Single-objective here; extend with extra metrics for true Pareto.
        return self.score > other.score


# Deterministic reflective mutation operators. Each takes a prompt and returns
# a mutated prompt. They encode common prompt-quality improvements so the loop
# is useful even with no LLM available.
def _add_step_by_step(prompt: str) -> str:
    line = "Think step by step and verify each step before continuing."
    return prompt if line in prompt else f"{prompt.rstrip()}\n{line}"


def _add_format_guard(prompt: str) -> str:
    line = "Return only the requested output; do not add commentary."
    return prompt if line in prompt else f"{prompt.rstrip()}\n{line}"


def _add_self_check(prompt: str) -> str:
    line = "Before finishing, re-read the requirements and confirm each is met."
    return prompt if line in prompt else f"{prompt.rstrip()}\n{line}"


def _tighten(prompt: str) -> str:
    # Collapse excess blank lines — a cheap clarity win.
    lines = [ln.rstrip() for ln in prompt.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    return "\n".join(out).strip()


_MUTATORS: list[Callable[[str], str]] = [
    _add_step_by_step,
    _add_format_guard,
    _add_self_check,
    _tighten,
]


class PromptEvolver:
    """Runs a small GEPA-style evolution and emits gated prompt proposals."""

    def __init__(
        self,
        cortex: Any | None = None,
        event_bus: EventBus | None = None,
        settings: Settings | None = None,
        *,
        evaluator: Evaluator | None = None,
        generations: int = 2,
        population: int = 4,
    ) -> None:
        self.cortex = cortex
        self.event_bus = event_bus
        self.settings = settings or get_settings()
        self.evaluator = evaluator or self._heuristic_evaluator
        self.generations = max(1, generations)
        self.population = max(1, population)

    # ---- evaluation ------------------------------------------------------
    async def _heuristic_evaluator(self, prompt: str, tasks: list[dict[str, Any]]) -> float:
        """Deterministic offline fitness: rewards known good-prompt signals.

        Scores in [0,1]. Used when no real evaluator is supplied so evolution
        is meaningful without network/LLM access.
        """
        signals = (
            "step by step",
            "verify",
            "return only",
            "confirm each",
            "requirements",
        )
        low = prompt.lower()
        hits = sum(1 for s in signals if s in low)
        base = hits / len(signals)
        # Penalize excessive length (clarity/cost).
        length_penalty = min(len(prompt) / 4000.0, 0.3)
        return max(0.0, min(1.0, base - length_penalty))

    # ---- evolution -------------------------------------------------------
    async def evolve(
        self,
        base_prompt: str,
        tasks: list[dict[str, Any]] | None = None,
        prompt_key: str = "base_prompt",
    ) -> PromptCandidate:
        """Evolve ``base_prompt`` and return the best candidate found.

        Also emits a gated proposal for the best candidate if it beats the
        baseline. Never adopts it directly.
        """
        tasks = tasks or []
        self._last_tasks = list(tasks)
        base_score = await self.evaluator(base_prompt, tasks)
        best = PromptCandidate(text=base_prompt, score=base_score, generation=0, notes="baseline")

        parent = best
        for gen in range(1, self.generations + 1):
            candidates = await self._spawn(parent, tasks, gen)
            for cand in candidates:
                if cand.dominates(best):
                    best = cand
            # Next parent is the current best (elitist selection).
            parent = best

        if best.text != base_prompt and best.score > base_score:
            await self._propose(best, base_prompt, base_score, prompt_key)
        return best

    async def evolve_from_manifest(self, manifest: Any) -> PromptCandidate | None:
        """Use a completed build manifest's captured prompts as evolution input.

        ``StudioRunner`` records exact code prompts under ``extra.prompts``. This
        bridge turns that real build evidence plus reviewer gaps into the task
        set consumed by ``evolve``. It returns ``None`` when no prompt was
        captured, keeping old builds harmless.
        """
        raw_value = (
            manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest or {})
        )
        raw: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
        extra_value = raw.get("extra")
        extra: dict[str, Any] = extra_value if isinstance(extra_value, dict) else {}
        prompts_value = extra.get("prompts")
        prompts: list[Any] = prompts_value if isinstance(prompts_value, list) else []
        prompt_row = next(
            (p for p in prompts if isinstance(p, dict) and str(p.get("text") or "").strip()),
            None,
        )
        if prompt_row is None:
            return None
        stage = str(prompt_row.get("stage") or "prompt")
        slug = str(raw.get("slug") or raw.get("build_id") or "build")
        tasks = [self._task_from_manifest(raw)]
        return await self.evolve(
            str(prompt_row.get("text") or ""),
            tasks=tasks,
            prompt_key=f"{slug}:{stage}",
        )

    @staticmethod
    def _task_from_manifest(raw: dict[str, Any]) -> dict[str, Any]:
        gaps: list[str] = []
        stages_value = raw.get("stages")
        stages: list[Any] = stages_value if isinstance(stages_value, list) else []
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            summary = stage.get("output_summary")
            if not isinstance(summary, dict):
                continue
            stage_gaps = summary.get("gaps")
            if isinstance(stage_gaps, list):
                gaps.extend(str(g) for g in stage_gaps if str(g))
        return {
            "slug": raw.get("slug"),
            "brief": raw.get("brief"),
            "stack": raw.get("stack"),
            "score": raw.get("score"),
            "verdict": raw.get("verdict"),
            "gaps": gaps,
        }

    async def _spawn(
        self, parent: PromptCandidate, tasks: list[dict[str, Any]], generation: int
    ) -> list[PromptCandidate]:
        out: list[PromptCandidate] = []
        for mutate in _MUTATORS[: self.population]:
            text = mutate(parent.text)
            if text == parent.text:
                continue
            score = await self.evaluator(text, tasks)
            out.append(
                PromptCandidate(
                    text=text,
                    score=score,
                    generation=generation,
                    parent=parent.text[:60],
                    notes=mutate.__name__,
                )
            )
        return out

    async def _propose(
        self,
        cand: PromptCandidate,
        base_prompt: str,
        base_score: float,
        prompt_key: str,
    ) -> Proposal:
        prop = Proposal(
            type=ProposalType.TUNING,
            title=f"GEPA-evolved prompt for '{prompt_key}' (+{cand.score - base_score:.2f})",
            source="prompt_evolver",
            rationale=(
                f"reflective evolution over {self.generations} generations; "
                f"baseline={base_score:.2f} -> candidate={cand.score:.2f}"
            ),
            payload={
                "prompt_key": prompt_key,
                "setting": f"prompt::{prompt_key}",
                "value": cand.text,
                "base_score": base_score,
                "candidate_score": cand.score,
                "generation": cand.generation,
                "mutation": cand.notes,
                "tasks": list(getattr(self, "_last_tasks", [])),
            },
            confidence=min(0.5 + (cand.score - base_score), 0.95),
            # Prompt changes alter behavior broadly -> never auto-apply.
            safe=False,
            dedupe_key=f"prompt:{prompt_key}:{_text_hash(cand.text)}",
        )
        if self.cortex is not None:
            await self.cortex.submit(prop)
        return prop
