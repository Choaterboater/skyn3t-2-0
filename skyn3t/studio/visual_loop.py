# skyn3t/studio/visual_loop.py
"""Visual self-inspection loop (Spec 3, Slice 3).

Ties the existing pieces into the "Kimi magic" auto-iterate loop: serve the
delivered app, screenshot + judge it against the goal (VisualChecker), and if it
looks visually wrong, feed the issues to the ImproveEngine and re-check — up to
``max_rounds`` times. Every collaborator is injected, so the control flow is
testable without a real browser, vision model, or build. A clean audit with no
vision model soft-skips, while an audit-detected workspace issue can drive an
advisory repair round without vision. Missing live preview still soft-skips.
"""
from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class VisualRound:
    index: int
    matches: bool
    skipped: bool
    issues: list[str] = field(default_factory=list)
    improved: bool = False
    proof_passed: bool | None = None
    layout_audit: Any | None = None
    advisory_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VisualLoopResult:
    passed: bool
    skipped: bool
    rounds: list[VisualRound] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rounds"] = [r.to_dict() for r in self.rounds]
        return d


async def _repair_context(provider: Any, verdict: Any) -> str:
    """Resolve optional repair context without letting it break a visual run."""

    if not callable(provider):
        return ""
    try:
        value = provider(verdict)
        if inspect.isawaitable(value):
            value = await value
        return str(value or "").strip()
    except Exception:  # noqa: BLE001 - asset lookup is advisory to the repair loop
        return ""


def _fix_goal(verdict: Any, repair_context: str = "") -> str:
    hint = getattr(verdict, "fix_hint", "") or ""
    issues = getattr(verdict, "issues", None) or []
    base = hint or "Fix these visual issues: " + "; ".join(str(i) for i in issues)
    return f"{base}\n\n{repair_context}" if repair_context else base


async def visual_self_improve(project_dir, goal: str, *, app_runner: Any,
                              checker: Any, improve_engine: Any,
                              vision_fn: Any = None, stack: str = "",
                              max_rounds: int = 2,
                              correlation_id: str | None = None,
                              layout_profile: object | None = None,
                              repair_context_provider: Any = None) -> VisualLoopResult:
    """Serve -> inspect -> improve -> re-check, up to max_rounds inspections.

    Returns passed when an inspection matches the goal; skipped when there is no
    live preview or the available evidence is a clean audit without vision;
    otherwise not-passed with the advisory repair rounds taken. Never raises
    (each collaborator call is guarded)."""
    rounds: list[VisualRound] = []
    n = max(1, int(max_rounds))
    for i in range(n):
        app = await app_runner.start(project_dir, stack)
        try:
            if getattr(app, "status", "") != "running":
                return VisualLoopResult(passed=False, skipped=True, rounds=rounds,
                                        reason="no live preview")
            check_kwargs = {
                "vision_fn": vision_fn,
                "correlation_id": correlation_id,
            }
            if layout_profile is not None:
                check_kwargs["layout_profile"] = layout_profile
            verdict = await checker.check(
                getattr(app, "url", ""),
                goal,
                **check_kwargs,
            )
        finally:
            try:
                stopped = app_runner.stop(app)
                if inspect.isawaitable(stopped):
                    await stopped
            except Exception:  # noqa: BLE001 - teardown must not break the loop
                pass
            try:
                from skyn3t.studio.app_runner import cleanup_serve
                cleanup_serve(app)
            except Exception:  # noqa: BLE001
                pass

        layout_audit = getattr(verdict, "layout_audit", None)
        advisory_only = bool(getattr(verdict, "advisory_only", False))
        if getattr(verdict, "skipped", False):
            if layout_audit is not None:
                rounds.append(
                    VisualRound(
                        index=i,
                        matches=False,
                        skipped=True,
                        layout_audit=layout_audit,
                        advisory_only=advisory_only,
                    )
                )
            return VisualLoopResult(passed=False, skipped=True, rounds=rounds,
                                    reason=getattr(verdict, "reason", "") or "vision unavailable")
        if getattr(verdict, "matches", False):
            rounds.append(VisualRound(index=i, matches=True, skipped=False,
                                      layout_audit=layout_audit,
                                      advisory_only=advisory_only))
            return VisualLoopResult(passed=True, skipped=False, rounds=rounds)

        issues = list(getattr(verdict, "issues", None) or [])
        last = i >= n - 1
        improved = False
        proof_passed = None
        if not last:
            try:
                repair_context = await _repair_context(repair_context_provider, verdict)
                out = await improve_engine.improve(
                    project_dir, goal=_fix_goal(verdict, repair_context),
                    correlation_id=correlation_id)
                improved = getattr(out, "status", "") == "completed"
                proof_passed = getattr(out, "proof_passed", None)
            except Exception:  # noqa: BLE001 - an improve failure ends the loop
                improved = False
                proof_passed = None
        rounds.append(VisualRound(index=i, matches=False, skipped=False,
                                  issues=issues, improved=improved,
                                  proof_passed=proof_passed,
                                  layout_audit=layout_audit,
                                  advisory_only=advisory_only))
        if last or not improved:
            break

    return VisualLoopResult(passed=False, skipped=False, rounds=rounds,
                            reason="visual issues remain after max rounds")
