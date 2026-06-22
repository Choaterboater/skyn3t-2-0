# skyn3t/studio/fanout.py
"""Fan-out orchestrator (Spec 4).

Generalizes best-of-N from "N same-stack trajectories" (studio.best_of_n) to N
DIVERGENT candidates — different stacks, fixes, or approaches for one brief —
built in parallel, refereed by proof, with the winner selected and the
exploration delta recorded. build_fn-injected so the logic is testable without
the agent spine; a real caller passes a build_fn that drives StudioRunner per
candidate and (optionally) a referee that re-proofs the delivered tree.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class FanCandidate:
    id: str
    label: str = ""
    spec: Any = None  # opaque arg handed to build_fn (e.g. {"stack": "react"})

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label}


@dataclass(slots=True)
class FanResult:
    candidate_id: str
    label: str
    verdict: str
    score: float | None
    proof_passed: bool
    status: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # A candidate "passes" when it both objectively proofs AND the build
        # verdict agrees — same bar best_of_n / the runner verdict use.
        return self.proof_passed and self.verdict == "go"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["passed"] = self.passed
        return d

    @classmethod
    def from_outcome(cls, cand: "FanCandidate", outcome: Any,
                     referee_verdict: tuple[bool, float] | None = None) -> "FanResult":
        manifest = getattr(outcome, "manifest", None) or {}
        extra = (manifest.get("extra") if isinstance(manifest, dict) else {}) or {}
        proof = extra.get("proof") or {}
        verdict = str(getattr(outcome, "verdict", "no_go") or "no_go")
        score = _as_float(getattr(outcome, "score", None))
        proof_passed = bool(proof.get("passed", False))
        if referee_verdict is not None:
            proof_passed, score = bool(referee_verdict[0]), float(referee_verdict[1])
        return cls(
            candidate_id=cand.id, label=cand.label or cand.id, verdict=verdict,
            score=score, proof_passed=proof_passed,
            status=str(getattr(outcome, "status", "") or ""),
            detail={"stack": str(getattr(outcome, "stack", "") or ""),
                    "slug": str(getattr(outcome, "slug", "") or "")},
        )


@dataclass(slots=True)
class FanOutOutcome:
    results: list[FanResult]
    winner: FanResult | None
    any_passed: bool
    delta: float                      # winner score - best runner-up score
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner": self.winner.to_dict() if self.winner else None,
            "any_passed": self.any_passed,
            "delta": self.delta,
            "summary": self.summary,
            "results": [r.to_dict() for r in self.results],
        }


BuildFn = Callable[[FanCandidate], Awaitable[Any]]
RefereeFn = Callable[[FanCandidate, Any], "tuple[bool, float]"]


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _rank_key(r: FanResult) -> tuple[int, float]:
    # Passing beats non-passing; within a tier, higher score wins. Mirrors
    # best_of_n: pick the best PASSER, else the MOST-COMPLETE candidate.
    return (1 if r.passed else 0, r.score if r.score is not None else 0.0)


async def _safe_build(build_fn: BuildFn, cand: FanCandidate) -> Any:
    try:
        return await build_fn(cand)
    except Exception:  # noqa: BLE001 - one bad candidate must not sink the fan-out
        return None


async def fan_out(candidates: list[FanCandidate], build_fn: BuildFn, *,
                  referee: RefereeFn | None = None) -> FanOutOutcome:
    """Build every candidate in parallel, referee, select the winner, record the
    exploration delta (winner score minus the best runner-up). A candidate whose
    build crashes becomes an error result rather than aborting the fan-out."""
    outcomes = await asyncio.gather(*[_safe_build(build_fn, c) for c in candidates])
    results: list[FanResult] = []
    for cand, outcome in zip(candidates, outcomes):
        if outcome is None:
            results.append(FanResult(candidate_id=cand.id, label=cand.label or cand.id,
                                     verdict="no_go", score=None, proof_passed=False,
                                     status="error"))
            continue
        rv = referee(cand, outcome) if referee is not None else None
        results.append(FanResult.from_outcome(cand, outcome, referee_verdict=rv))

    ranked = sorted(results, key=_rank_key, reverse=True)
    winner = ranked[0] if ranked else None
    any_passed = any(r.passed for r in results)
    delta = 0.0
    if winner is not None and len(ranked) >= 2:
        ws = winner.score if winner.score is not None else 0.0
        rs = ranked[1].score if ranked[1].score is not None else 0.0
        delta = round(ws - rs, 2)
    summary = {
        "n": len(results),
        "passed": sum(1 for r in results if r.passed),
        "winner": winner.candidate_id if winner else None,
        "delta": delta,
    }
    return FanOutOutcome(results=results, winner=winner, any_passed=any_passed,
                         delta=delta, summary=summary)
