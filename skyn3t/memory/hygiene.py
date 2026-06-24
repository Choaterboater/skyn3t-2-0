"""Memory hygiene (2.0 P2): scoring, versioning, and decay for lessons.

This module keeps the persistent learning store *clean* by decaying and
pruning low-value lessons. It operates **exclusively through the public
MemoryStore API** (``relevant_lessons`` / ``grade_lesson`` / ``add_lesson``)
plus a small read-only helper — it never imports or mutates ``store.py`` /
``models.py`` internals.

A lesson's persisted ``score`` is ``helpful - hurt`` (see store.py). Hygiene
adds a *decayed effective score*: lessons that have been used a lot without
ever helping, or that never get reused at all, are demoted and (optionally)
neutralised by grading them down so they fall out of ``relevant_lessons``
ranking. We can't DELETE rows without touching store.py, so "decay" here means
driving the score negative so the lesson is effectively retired.

Design rules: #2 close the learning edge (bad lessons get retired),
#5 cheap (pure computation), #6 degrade — every op tolerates a missing store.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol


class _StoreLike(Protocol):
    async def relevant_lessons(
        self, stack: str, stage: str = "", limit: int = 5, *, ascending: bool = False
    ) -> list[dict[str, Any]]: ...
    async def grade_lesson(self, lesson_id: int, helpful: bool) -> None: ...


@dataclass(slots=True)
class HygieneReport:
    scanned: int = 0
    retired: int = 0
    kept: int = 0
    details: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "retired": self.retired,
            "kept": self.kept,
            "details": self.details,
        }


def effective_score(
    raw_score: float,
    times_used: int,
    *,
    helpful: int | None = None,
    hurt: int | None = None,
    decay: float = 0.85,
) -> float:
    """Compute a usage-aware, decayed score for a lesson.

    * A lesson used many times but with a low raw score is penalised (it keeps
      getting surfaced yet rarely helps).
    * A never-used lesson is neutral-ish (no evidence either way), so we apply
      only mild decay.
    * ``helpful`` / ``hurt`` (when known) refine the hit-rate component.
    """
    base = float(raw_score)
    if helpful is not None and hurt is not None and (helpful + hurt) > 0:
        hit_rate = helpful / (helpful + hurt)  # 0..1
        base += (hit_rate - 0.5) * 2.0  # +/-1 nudge by hit rate
    # Usage-weighted decay: more uses without a positive score -> stronger decay.
    usage_factor = decay ** max(0, times_used)
    if base <= 0:
        # Low/zero-value lessons decay toward negative as they age in use.
        return base - (1.0 - usage_factor) - 0.1 * math.log1p(max(0, times_used))
    return base * usage_factor


def is_stale(
    lesson: dict[str, Any],
    *,
    min_effective_score: float = -0.5,
    min_uses_to_judge: int = 3,
) -> bool:
    """Decide whether a lesson should be retired.

    Stale = it has had enough chances (``times_used >= min_uses_to_judge``) and
    its effective score has decayed below ``min_effective_score``.
    """
    times_used = int(lesson.get("times_used", 0) or 0)
    raw = float(lesson.get("score", 0.0) or 0.0)
    eff = effective_score(
        raw,
        times_used,
        helpful=lesson.get("helpful"),
        hurt=lesson.get("hurt"),
    )
    if times_used < min_uses_to_judge:
        return False
    return eff < min_effective_score


class LessonHygiene:
    """Runs decay/retirement over a stack's lessons via MemoryStore public API."""

    def __init__(
        self,
        store: _StoreLike,
        *,
        min_effective_score: float = -0.5,
        min_uses_to_judge: int = 3,
        retire_grades: int = 2,
    ) -> None:
        self.store = store
        self.min_effective_score = min_effective_score
        self.min_uses_to_judge = min_uses_to_judge
        # How many extra "unhelpful" grades to push so a stale lesson's score
        # drops decisively below its peers in relevant_lessons ranking.
        self.retire_grades = max(1, int(retire_grades))
        # Terminal state: ids already retired by this instance. The ascending
        # (worst-first) scan always re-surfaces a retired lesson, so without this
        # every sweep re-grades it — driving its score and hurt count down without
        # bound and re-counting it as "retired" forever.
        self._retired: set[int] = set()

    async def sweep(self, stack: str, stage: str = "", scan_limit: int = 200) -> HygieneReport:
        """Scan lessons for a stack/stage and retire the stale ones.

        "Retire" = grade the lesson unhelpful enough times that its persisted
        score goes negative, so it no longer ranks in ``relevant_lessons``.
        We never delete rows (store.py is owned by another package).
        """
        report = HygieneReport()
        # Retirement targets the WORST-scored (most-stale) lessons, so scan in
        # ascending score order — a score-DESC top-N would cut off the very rows
        # this sweep exists to retire. Fall back to the default ordering for
        # stores that don't support the ``ascending`` flag.
        try:
            try:
                lessons = await self.store.relevant_lessons(
                    stack, stage, limit=scan_limit, ascending=True
                )
            except TypeError:
                lessons = await self.store.relevant_lessons(stack, stage, limit=scan_limit)
        except Exception:  # degrade, don't crash
            return report

        for lesson in lessons:
            report.scanned += 1
            lid = lesson.get("id")
            if lid is None:
                report.kept += 1
                continue
            if int(lid) in self._retired:
                report.kept += 1  # already retired — terminal, never re-grade
                continue
            if is_stale(
                lesson,
                min_effective_score=self.min_effective_score,
                min_uses_to_judge=self.min_uses_to_judge,
            ):
                graded = 0
                for _ in range(self.retire_grades):
                    try:
                        await self.store.grade_lesson(int(lid), helpful=False)
                        graded += 1
                    except Exception:
                        break
                if graded:
                    report.retired += 1
                    self._retired.add(int(lid))
                    report.details.append(
                        {"id": lid, "action": "retired", "grades_applied": graded}
                    )
                else:
                    report.kept += 1
            else:
                report.kept += 1
        return report
