# skyn3t/studio/fanout.py
"""Fan-out orchestrator (Spec 4).

Generalizes best-of-N from "N same-stack trajectories" (studio.best_of_n) to N
DIVERGENT candidates — different stacks, fixes, or approaches for one brief —
built in parallel, refereed by proof, with the winner selected and the
exploration delta recorded. build_fn-injected so the logic is testable without
the agent spine; a real caller passes a build_fn that drives StudioRunner per
candidate and (optionally) a referee that re-proofs the delivered tree.

Never-sink doctrine: a candidate whose build crashes becomes an error result
rather than aborting the fan-out, and a referee whose re-proof crashes is
contained the same way — that candidate fails CLOSED (error result, not
passed, ``detail["referee_error"]``) while the rest of the fan-out completes.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any


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
    def from_outcome(cls, cand: FanCandidate, outcome: Any,
                     referee_verdict: tuple[bool, float] | None = None) -> FanResult:
        manifest = getattr(outcome, "manifest", None) or {}
        extra = (manifest.get("extra") if isinstance(manifest, dict) else {}) or {}
        proof = extra.get("proof") or {}
        verdict = str(getattr(outcome, "verdict", "no_go") or "no_go")
        score = _as_float(getattr(outcome, "score", None))
        proof_passed = bool(proof.get("passed", False))
        if referee_verdict is not None:
            # The referee re-proofs the delivered tree — it is AUTHORITATIVE over
            # the (possibly stale) build verdict, so its pass/fail also drives the
            # effective verdict that .passed keys on.
            proof_passed, score = bool(referee_verdict[0]), float(referee_verdict[1])
            verdict = "go" if proof_passed else "no_go"
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


def autonomous_stacks(settings: Any, *, has_pin: bool) -> list[str]:
    """The stacks an UNPINNED build should be explored across, or [] when
    autonomous fan-out is off (empty setting) or a stack was explicitly pinned.
    Needs >=2 stacks to be a fan-out. Default behavior is a normal single build."""
    if has_pin:
        return []
    raw = str(getattr(settings, "autonomous_fanout_stacks", "") or "")
    stacks = [s.strip() for s in raw.split(",") if s.strip()]
    return stacks if len(stacks) >= 2 else []


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


_REFEREE_CRASHED = object()  # sentinel: referee raised (distinct from a None verdict)


def _safe_referee(referee: RefereeFn, cand: FanCandidate, outcome: Any) -> Any:
    """Call the referee, containing a crash so it cannot sink the fan-out.

    Returns the referee verdict tuple, or the ``_REFEREE_CRASHED`` sentinel.
    The referee re-proofs the delivered tree (a subprocess-heavy proof_run), so
    it is at least as failure-prone as the build itself — and the build path is
    already protected by ``_safe_build``.
    """
    try:
        return referee(cand, outcome)
    except Exception:  # noqa: BLE001 - a referee crash must not sink the fan-out
        return _REFEREE_CRASHED


async def _emit(event_bus: Any, etype: Any, payload: dict[str, Any], cid: str) -> None:
    if event_bus is None:
        return
    try:
        await event_bus.emit(etype, "fanout", payload, correlation_id=cid)
    except Exception:  # noqa: BLE001 - events never break the fan-out
        pass


async def _run_candidate(cand: FanCandidate, build_fn: BuildFn, referee: RefereeFn | None,
                         event_bus: Any, cid: str) -> FanResult:
    outcome = await _safe_build(build_fn, cand)
    if outcome is None:
        res = FanResult(candidate_id=cand.id, label=cand.label or cand.id,
                        verdict="no_go", score=None, proof_passed=False, status="error")
    else:
        rv = _safe_referee(referee, cand, outcome) if referee is not None else None
        if rv is _REFEREE_CRASHED:
            # Symmetric with a build crash: the referee is AUTHORITATIVE, so a
            # candidate it could not re-proof is unverifiable — fail it CLOSED
            # (error result, not passed) instead of trusting the stale build
            # verdict, and let the rest of the fan-out complete.
            res = FanResult(candidate_id=cand.id, label=cand.label or cand.id,
                            verdict="no_go", score=None, proof_passed=False,
                            status="error",
                            detail={"referee_error": True,
                                    "stack": str(getattr(outcome, "stack", "") or ""),
                                    "slug": str(getattr(outcome, "slug", "") or "")})
        else:
            res = FanResult.from_outcome(cand, outcome, referee_verdict=rv)
    # Stream each candidate's result as it resolves so the cockpit can show
    # progress (FANOUT events fan out via the existing EventType.ALL bridge).
    from skyn3t.core.events import EventType
    await _emit(event_bus, EventType.FANOUT_CANDIDATE, res.to_dict(), cid)
    return res


async def fan_out(candidates: list[FanCandidate], build_fn: BuildFn, *,
                  referee: RefereeFn | None = None, event_bus: Any = None,
                  correlation_id: str = "") -> FanOutOutcome:
    """Build every candidate in parallel, referee, select the winner, record the
    exploration delta (winner score minus the best runner-up). A candidate whose
    build crashes becomes an error result rather than aborting the fan-out; a
    referee crash on a candidate is contained the same way (fail-closed error
    result). When an event_bus is supplied, emits FANOUT_STARTED /
    FANOUT_CANDIDATE (per result) / FANOUT_COMPLETED so the dashboard can watch
    the exploration live."""
    from skyn3t.core.events import EventType
    cid = correlation_id or ""
    await _emit(event_bus, EventType.FANOUT_STARTED,
                {"candidates": [c.id for c in candidates],
                 "n": len(candidates)}, cid)
    results = await asyncio.gather(
        *[_run_candidate(c, build_fn, referee, event_bus, cid) for c in candidates])

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
    await _emit(event_bus, EventType.FANOUT_COMPLETED,
                {"winner": winner.candidate_id if winner else None,
                 "delta": delta, "any_passed": any_passed, "summary": summary}, cid)
    return FanOutOutcome(results=results, winner=winner, any_passed=any_passed,
                         delta=delta, summary=summary)
