# skyn3t/studio/visual_loop.py
"""Visual self-inspection loop (Spec 3, Slice 3).

Ties the existing pieces into the "Kimi magic" auto-iterate loop: serve the
delivered app, screenshot + judge it against the goal (VisualChecker), and if it
looks visually wrong, feed the issues to the ImproveEngine and re-check — up to
``max_rounds`` times. Every collaborator is injected, so the control flow is
testable without a real browser, vision model, or build. It degrades cleanly:
no live preview, or no vision model wired, yields a ``skipped`` result rather
than failing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class VisualRound:
    index: int
    matches: bool
    skipped: bool
    issues: list[str] = field(default_factory=list)
    improved: bool = False

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


def _fix_goal(verdict: Any) -> str:
    hint = getattr(verdict, "fix_hint", "") or ""
    if hint:
        return hint
    issues = getattr(verdict, "issues", None) or []
    return "Fix these visual issues: " + "; ".join(str(i) for i in issues)


async def visual_self_improve(project_dir, goal: str, *, app_runner: Any,
                              checker: Any, improve_engine: Any,
                              vision_fn: Any = None, stack: str = "",
                              max_rounds: int = 2) -> VisualLoopResult:
    """Serve -> inspect -> improve -> re-check, up to max_rounds inspections.

    Returns passed when an inspection matches the goal; skipped when there's no
    live preview or no vision model; otherwise not-passed with the rounds taken.
    Never raises (each collaborator call is guarded)."""
    rounds: list[VisualRound] = []
    n = max(1, int(max_rounds))
    for i in range(n):
        app = await app_runner.start(project_dir, stack)
        try:
            if getattr(app, "status", "") != "running":
                return VisualLoopResult(passed=False, skipped=True, rounds=rounds,
                                        reason="no live preview")
            verdict = await checker.check(getattr(app, "url", ""), goal,
                                          vision_fn=vision_fn)
        finally:
            try:
                app_runner.stop(app)
            except Exception:  # noqa: BLE001 - teardown must not break the loop
                pass
            try:
                from skyn3t.studio.app_runner import cleanup_serve
                cleanup_serve(app)
            except Exception:  # noqa: BLE001
                pass

        if getattr(verdict, "skipped", False):
            return VisualLoopResult(passed=False, skipped=True, rounds=rounds,
                                    reason=getattr(verdict, "reason", "") or "vision unavailable")
        if getattr(verdict, "matches", False):
            rounds.append(VisualRound(index=i, matches=True, skipped=False))
            return VisualLoopResult(passed=True, skipped=False, rounds=rounds)

        issues = list(getattr(verdict, "issues", None) or [])
        last = i >= n - 1
        improved = False
        if not last:
            try:
                await improve_engine.improve(project_dir, goal=_fix_goal(verdict))
                improved = True
            except Exception:  # noqa: BLE001 - an improve failure ends the loop
                improved = False
        rounds.append(VisualRound(index=i, matches=False, skipped=False,
                                  issues=issues, improved=improved))
        if last or not improved:
            break

    return VisualLoopResult(passed=False, skipped=False, rounds=rounds,
                            reason="visual issues remain after max rounds")
