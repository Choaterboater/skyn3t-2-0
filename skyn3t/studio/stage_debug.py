"""Per-stage debug pass — verify each build step, fix it, then proceed.

The pipeline used to debug only ONCE, at the end (a single proof + fix loop on
the merged tree), so a broken early stage poisoned everything downstream. This
pass runs after each productive stage: it checks the stage's output, and for the
code stage runs a bounded fix loop (re-using the ``code_improver`` agent), then
emits the events the cockpit renders. Fully autonomous — never prompts a human;
an unfixable step is flagged ``degraded`` and the build proceeds best-effort.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from skyn3t.core.events import EventType
from skyn3t.studio.proof_run import proof_run

# Stage agent_types whose output gets a full proof + fix loop. Other stages get
# a light "did it produce output" check with no auto-fix (Phase A scope).
_CODE_AGENT_TYPES = frozenset({"code"})

EmitFn = Callable[[EventType, dict[str, Any]], Awaitable[None]]
ImproveFn = Callable[[list[str]], Awaitable[bool]]


@dataclass(slots=True)
class StageDebugResult:
    passed: bool
    degraded: bool
    attempts: int
    score: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _Check:
    passed: bool
    score: float | None
    gaps: list[str]


def _run_check(spec: Any, record: Any, worktree_dir: str, plan: Any, settings: Any) -> _Check:
    """Stage-appropriate pass/fail. Code stages get a real proof; others a light check."""
    if spec.agent_type in _CODE_AGENT_TYPES:
        proof = proof_run(
            worktree_dir,
            checklist=list(getattr(plan, "checklist", []) or []),
            execution_backend=getattr(settings, "execution_backend", "auto"),
            stack=getattr(plan, "stack", ""),
            run_tests=bool(getattr(settings, "run_generated_tests", True)),
            test_timeout=int(getattr(settings, "generated_test_timeout", 90)),
            run_build=bool(getattr(settings, "run_generated_build", True)),
            build_timeout=int(getattr(settings, "generated_build_timeout", 300)),
        )
        gaps = list(proof.missing) + list(proof.syntax_errors)
        return _Check(passed=proof.passed, score=proof.score, gaps=gaps)
    passed = record.status == "completed"
    gaps = [] if passed else [f"stage {spec.name} status={record.status}"]
    return _Check(passed=passed, score=record.score, gaps=gaps)


async def debug_stage(
    *,
    build_id: str,
    spec: Any,
    record: Any,
    worktree_dir: str,
    plan: Any,
    settings: Any,
    emit: EmitFn,
    improve: ImproveFn | None = None,
    max_attempts: int = 3,
) -> StageDebugResult:
    """Run the per-stage debug loop, emitting STAGE_DEBUG_* events. Never raises."""
    base = {"build_id": build_id, "stage": spec.name, "capability": spec.capability}
    check_kind = "proof" if spec.agent_type in _CODE_AGENT_TYPES else "light"
    await emit(EventType.STAGE_DEBUG_STARTED, {**base, "check": check_kind})

    check = _run_check(spec, record, worktree_dir, plan, settings)
    attempts = 0
    while not check.passed and improve is not None and attempts < max_attempts:
        attempts += 1
        score_before = check.score
        try:
            ran = await improve(check.gaps)
        except Exception:  # noqa: BLE001 - a failed fix must not crash the build
            ran = False
        nxt = _run_check(spec, record, worktree_dir, plan, settings)
        await emit(EventType.STAGE_DEBUG_ATTEMPT, {
            **base, "agent_type": spec.agent_type, "attempt": attempts,
            "errors": check.gaps[:10], "fix_applied": bool(ran),
            "passed": nxt.passed, "score_before": score_before, "score_after": nxt.score,
        })
        check = nxt
        if not ran:
            # A successfully dispatched improver that changed zero files cannot
            # make the next identical proof pass. Stop here instead of spending
            # every debug attempt on repeated no-op model calls.
            break

    status = "passed" if check.passed else "degraded"
    await emit(EventType.STAGE_DEBUG_RESOLVED, {
        **base, "status": status, "reason": "; ".join(check.gaps[:3]),
    })
    return StageDebugResult(
        passed=check.passed, degraded=not check.passed, attempts=attempts,
        score=check.score, detail={"gaps": check.gaps[:10]},
    )
