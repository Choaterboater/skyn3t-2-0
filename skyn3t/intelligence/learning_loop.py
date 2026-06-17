"""Learning loop — close the learning edge (design rule #2).

This module captures a *lesson* from each build outcome and feeds it back into
the next matching build:

  1. ``capture_from_build``  — after a build, mine durable lessons (what worked /
     what failed) and persist them via :class:`MemoryStore.add_lesson`. Also
     publishes ``LESSON_CAPTURED`` so the rest of the system (dashboard, replay)
     can observe it (design rule #7: everything is an event).
  2. ``inject_for_build``    — before the next matching build, fetch the lessons
     most relevant to that stack/stage and return them as injectable advice. The
     returned handle records which lesson ids were used.
  3. ``grade_injected``      — once the new build finishes, grade each injected
     lesson by whether the outcome was good (CLOSE THE LOOP).

2.0 P1 hook — auto-mined best practices: :func:`mine_best_practices` diffs the
*accepted* vs *rejected* outputs of a build and distils short, durable rules.

Everything degrades gracefully: with no MemoryStore the loop keeps an in-memory
buffer so the module is still useful (and testable) offline (design rule #6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

try:  # structlog is a core dep, but never let logging break import.
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - defensive
    _log = None  # type: ignore[assignment]


def _info(event: str, **kw: Any) -> None:
    if _log is not None:
        try:
            _log.info(event, **kw)
        except Exception:  # pragma: no cover - defensive
            pass


# Words that signal a durable, reusable lesson vs. one-off noise.
_POSITIVE_MARKERS = ("worked", "passed", "fixed", "stable", "fast", "clean", "go")
_NEGATIVE_MARKERS = ("failed", "broke", "flaky", "timeout", "regression", "no_go", "missing")


@dataclass(slots=True)
class InjectedLessons:
    """Handle returned by :func:`inject_for_build`.

    Hold onto it for the duration of the build, then call
    :meth:`LearningLoop.grade_injected` with the outcome to close the loop.
    """

    stack: str
    stage: str
    lesson_ids: list[int] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    def as_advice(self) -> str:
        """Render injected lessons as non-binding advice for a prompt."""
        if not self.texts:
            return ""
        bullets = "\n".join(f"- {t}" for t in self.texts)
        return (
            "Lessons learned from prior similar builds (advisory, not "
            f"mandatory):\n{bullets}"
        )


def mine_best_practices(
    accepted: list[str] | None,
    rejected: list[str] | None,
    *,
    max_rules: int = 5,
) -> list[str]:
    """Distil short best-practice rules from accepted vs rejected outputs.

    2.0 P1 auto-mined best practices. This is deliberately deterministic and
    offline: it contrasts signal phrases present in accepted outputs but absent
    in rejected ones (and vice-versa) to produce ``do``/``avoid`` rules.
    """
    accepted = accepted or []
    rejected = rejected or []

    def _phrases(texts: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in texts:
            for line in str(t).splitlines():
                line = line.strip().lstrip("-*0123456789. ").strip()
                if 8 <= len(line) <= 140:
                    key = re.sub(r"\s+", " ", line.lower())
                    counts[key] = counts.get(key, 0) + 1
        return counts

    acc = _phrases(accepted)
    rej = _phrases(rejected)
    rules: list[str] = []

    # Phrases that show up only in accepted output -> "do".
    for phrase, n in sorted(acc.items(), key=lambda kv: -kv[1]):
        if phrase not in rej and n >= 1:
            rules.append(f"Prefer: {phrase}")
        if len(rules) >= max_rules:
            break

    # Phrases unique to rejected output -> "avoid".
    for phrase, n in sorted(rej.items(), key=lambda kv: -kv[1]):
        if phrase not in acc and n >= 1:
            rules.append(f"Avoid: {phrase}")
        if len(rules) >= max_rules:
            break

    return rules[:max_rules]


def _summarize_outcome(build: dict[str, Any]) -> list[str]:
    """Extract candidate lesson strings from a build-outcome dict."""
    lessons: list[str] = []
    verdict = str(build.get("verdict") or build.get("status") or "").lower()
    score = build.get("score")
    stack = build.get("stack") or "generic"
    gaps = build.get("gaps") or []

    if isinstance(score, (int, float)):
        if score >= 90:
            lessons.append(
                f"{stack}: this build shape scored {score:.0f}; keep its approach."
            )
        elif score < 60:
            lessons.append(
                f"{stack}: build scored {score:.0f}; the chosen approach underperformed."
            )

    if "no_go" in verdict or "fail" in verdict:
        for g in gaps[:3]:
            lessons.append(f"{stack}: avoid the gap '{str(g)[:120]}'.")
        if not gaps:
            lessons.append(f"{stack}: build failed verification — re-check the plan.")
    elif "go" in verdict or "complete" in verdict or "success" in str(verdict):
        notes = build.get("notes") or build.get("brief")
        if notes:
            lessons.append(f"{stack}: successful build — {str(notes)[:120]}")

    # Fold in any auto-mined best practices.
    lessons.extend(
        mine_best_practices(build.get("accepted"), build.get("rejected"))
    )
    # De-dup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for ls in lessons:
        if ls and ls not in seen:
            seen.add(ls)
            out.append(ls)
    return out


class LearningLoop:
    """Capture / inject / grade lessons around builds.

    Parameters
    ----------
    store:
        A ``MemoryStore`` (duck-typed: needs ``add_lesson``, ``relevant_lessons``,
        ``grade_lesson``). If ``None``, an in-memory fallback is used so the loop
        still functions offline.
    event_bus:
        Optional ``EventBus`` to emit ``LESSON_CAPTURED`` events.
    """

    def __init__(self, store: Any | None = None, event_bus: Any | None = None) -> None:
        self.store = store
        self.event_bus = event_bus
        # Offline fallback storage.
        self._mem: list[dict[str, Any]] = []
        self._next_id = 1

    # ---- capture -------------------------------------------------------
    async def capture_from_build(self, build: dict[str, Any]) -> list[int]:
        """Mine lessons from a finished build and persist them.

        Returns the list of stored lesson ids.
        """
        stack = str(build.get("stack") or "generic")
        stage = str(build.get("stage") or "")
        source_build = build.get("build_id") or build.get("slug")
        texts = _summarize_outcome(build)
        ids: list[int] = []
        for text in texts:
            lid = await self._add_lesson(stack, stage, text, source_build)
            ids.append(lid)
        if ids and self.event_bus is not None:
            await self._emit_captured(stack, stage, texts, ids, source_build)
        _info("learning.captured", stack=stack, stage=stage, count=len(ids))
        return ids

    async def _add_lesson(
        self, stack: str, stage: str, text: str, source_build: Any
    ) -> int:
        if self.store is not None:
            try:
                return int(
                    await self.store.add_lesson(
                        stack, stage, text, source_build=source_build
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _info("learning.store_add_failed", error=str(exc))
        lid = self._next_id
        self._next_id += 1
        self._mem.append(
            {
                "id": lid,
                "stack": stack,
                "stage": stage,
                "text": text,
                "source_build": source_build,
                "helpful": 0,
                "unhelpful": 0,
            }
        )
        return lid

    async def _emit_captured(
        self,
        stack: str,
        stage: str,
        texts: list[str],
        ids: list[int],
        source_build: Any,
    ) -> None:
        try:
            from skyn3t.core.events import EventType

            await self.event_bus.emit(
                EventType.LESSON_CAPTURED,
                source="intelligence.learning_loop",
                payload={
                    "stack": stack,
                    "stage": stage,
                    "lesson_ids": ids,
                    "lessons": texts,
                    "source_build": source_build,
                },
            )
        except Exception as exc:  # noqa: BLE001
            _info("learning.emit_failed", error=str(exc))

    # ---- inject --------------------------------------------------------
    async def inject_for_build(
        self, stack: str, stage: str = "", limit: int = 5
    ) -> InjectedLessons:
        """Fetch the most relevant lessons for the next matching build."""
        rows = await self._relevant(stack, stage, limit)
        injected = InjectedLessons(stack=stack, stage=stage)
        for row in rows:
            lid = row.get("id")
            text = row.get("text")
            if lid is not None and text:
                injected.lesson_ids.append(int(lid))
                injected.texts.append(str(text))
        _info(
            "learning.injected",
            stack=stack,
            stage=stage,
            count=len(injected.lesson_ids),
        )
        return injected

    async def _relevant(
        self, stack: str, stage: str, limit: int
    ) -> list[dict[str, Any]]:
        if self.store is not None:
            try:
                return list(
                    await self.store.relevant_lessons(stack, stage=stage, limit=limit)
                )
            except Exception as exc:  # noqa: BLE001
                _info("learning.store_relevant_failed", error=str(exc))
        out = [
            r
            for r in self._mem
            if r["stack"] == stack and (not stage or r["stage"] in ("", stage))
        ]
        return out[-limit:][::-1]

    # ---- grade (close the loop) ---------------------------------------
    async def grade_injected(
        self, injected: InjectedLessons, *, helpful: bool
    ) -> None:
        """Grade every injected lesson by the new build's outcome."""
        for lid in injected.lesson_ids:
            await self._grade(lid, helpful)
        _info(
            "learning.graded",
            count=len(injected.lesson_ids),
            helpful=helpful,
        )

    async def _grade(self, lesson_id: int, helpful: bool) -> None:
        if self.store is not None:
            try:
                await self.store.grade_lesson(lesson_id, helpful)
                return
            except Exception as exc:  # noqa: BLE001
                _info("learning.store_grade_failed", error=str(exc))
        for r in self._mem:
            if r["id"] == lesson_id:
                r["helpful" if helpful else "unhelpful"] += 1
                break
