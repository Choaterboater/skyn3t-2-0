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
        # Real compiler/test/boot/import failures distilled into avoid-rules:
        # flatten whitespace and truncate so the lesson keeps the error category
        # AND the actual message/filename on one line, not a 700-char build dump.
        for e in (build.get("proof_errors") or [])[:3]:
            flat = " ".join(str(e).split())[:160]
            lessons.append(f"{stack}: avoid — {flat}")
        if (
            not gaps
            and not build.get("proof_errors")
            and not build.get("gate_findings")
            and not build.get("infrastructure_failure")
        ):
            lessons.append(f"{stack}: build failed verification — re-check the plan.")
    elif "go" in verdict or "complete" in verdict or "success" in str(verdict):
        # Never echo the brief into a lesson: a row whose text literally IS an
        # old brief maximally matches any similar future brief in the
        # injection re-rank (BM25 + cosine against the CURRENT brief), gets
        # graded helpful on every go, and permanently crowds actionable
        # avoid/gap rules out of the score-ranked top fetch — one content-free
        # row per distinct brief. Real notes are fine; without them mint one
        # constant, brief-free success note that dedupes to a single row per
        # stack.
        notes = build.get("notes")
        if notes:
            lessons.append(f"{stack}: successful build — {str(notes)[:120]}")
        else:
            lessons.append(
                f"{stack}: build succeeded with this pipeline shape; keep its approach."
            )

    # Advisory-gate findings become lessons REGARDLESS of verdict: the
    # end-of-build gates (seo/mcp_check/rag_check/liveness) record findings and
    # feed ONE repair but never flip a build to no_go — so without this, a 'go'
    # build's caught defect (an SEO hole, an unwired LLM seam, a dead route)
    # taught the system nothing and the same class recurred build after build.
    for finding in (build.get("gate_findings") or [])[:12]:
        flat = " ".join(str(finding).split())[:160]
        if flat:
            lessons.append(f"{stack}: gate flagged — {flat}")

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


# Gates whose verdicts share the same to_dict shape
# ({"skipped": bool, "issues": [str, ...], ...}) under these manifest.extra keys.
_GATE_VERDICT_KEYS = (
    "seo",
    "mcp_check",
    "rag_check",
    "cli_check",
    "cli_playtest",
    "finance_sanity",
    "workflow_depth",
)
_HARD_GATE_KEYS = (
    "verifier_gate",
    "critic_gate",
    "intent_gate",
    "headless_gate_gate",
    "game_visual_gate",
    "qa_playtest_gate",
)
_DETAIL_GATE_KEYS = ("headless_gate", "game_visual", "qa_playtest")


def _flat_finding(text: Any, limit: int = 160) -> str:
    return " ".join(str(text).split())[:limit]


def vent_lesson_texts(stack: str, vents: Any, *, max_vents: int = 3,
                      max_chars: int = 300) -> list[str]:
    """Turn codegen friction vents into low-severity, vent-tagged lesson texts.

    The vent channel (code_agent's ``VENT:`` convention) reports PIPELINE
    friction — a missing tool, a contradictory directive, an undiagnosable
    error, an unsatisfiable gate. Texts are stored through the normal
    ``add_lesson`` path (capture-side dedupe applies, so a recurring vent
    reinforces one row instead of minting duplicates), tagged by the
    ``vent —`` prefix so the class is greppable in the lesson store."""
    out: list[str] = []
    if not isinstance(vents, (list, tuple)):
        return out
    for vent in vents:
        flat = " ".join(str(vent).split())[:max_chars].strip()
        if flat:
            text = f"{stack}: vent — {flat}"
            if text not in out:
                out.append(text)
        if len(out) >= max_vents:
            break
    return out


def extract_gate_findings(extra: dict[str, Any] | None) -> list[str]:
    """Flatten advisory-gate findings out of a build manifest's ``extra`` into
    short ``"<gate>: <issue>"`` strings for lesson capture.

    A skipped gate (could-not-run) contributes nothing — a degrade-open skip
    must never mint an avoid-rule. Capped per gate so one noisy verdict can't
    flood the lesson store. Includes hard verdict gates (verifier/critic/intent/
    headless/game visual/QA) so no_go builds teach concrete avoid-rules instead
    of only "re-check the plan". Never raises; unexpected shapes yield ``[]``.
    """
    out: list[str] = []
    if not isinstance(extra, dict):
        return out

    def add(gate: str, detail: Any) -> None:
        flat = _flat_finding(detail)
        if flat:
            out.append(f"{gate}: {flat}")

    for key in _GATE_VERDICT_KEYS:
        verdict = extra.get(key)
        if not isinstance(verdict, dict) or verdict.get("skipped"):
            continue
        issues = verdict.get("issues")
        if not isinstance(issues, list):
            continue
        for issue in issues[:3]:
            add(key, issue)

    for key in _HARD_GATE_KEYS:
        finding = extra.get(key)
        if isinstance(finding, str) and finding.strip():
            add(key, finding)

    for key in _DETAIL_GATE_KEYS:
        verdict = extra.get(key)
        if not isinstance(verdict, dict) or verdict.get("skipped"):
            continue
        gap = verdict.get("gap")
        if gap:
            add(key, gap)
        for detail_field in ("issues", "gaps", "console_errors"):
            values = verdict.get(detail_field)
            if isinstance(values, list):
                for item in values[:3]:
                    add(key, item)
        roles = verdict.get("missing_sprite_roles")
        if isinstance(roles, list) and roles:
            add(key, "missing sprite role(s) not rendered — " + ", ".join(map(str, roles[:8])))

    live = extra.get("liveness")
    if isinstance(live, dict) and not live.get("skipped"):
        dead = [str(d) for d in (live.get("dead_routes") or [])[:5] if str(d)]
        if dead:
            out.append(f"liveness: route(s) dead after repair — {', '.join(dead)}")

    # External proof-tool absence is an environment problem, not a defect in
    # the generated product, so skipped Docker/Playwright/Maestro steps teach
    # nothing. A proof that actually ran and failed does become a concrete
    # verification lesson.
    ladder = extra.get("proof_ladder")
    if isinstance(ladder, dict) and ladder.get("status") == "failed":
        steps = ladder.get("steps")
        if isinstance(steps, list):
            for step in steps[:8]:
                if (
                    not isinstance(step, dict)
                    or step.get("status") != "failed"
                    or step.get("name") in {
                        "artifact_store",
                        "toolchain",
                        "preview_cleanup",
                    }
                ):
                    continue
                add(
                    f"proof_ladder.{step.get('name') or 'proof'}",
                    step.get("reason") or "required external proof failed",
                )

    seen: set[str] = set()
    deduped: list[str] = []
    for finding in out:
        if finding not in seen:
            seen.add(finding)
            deduped.append(finding)
    return deduped


def proof_ladder_infrastructure_unavailable(
    extra: dict[str, Any] | None,
) -> bool:
    """Whether a no-go came only from proof infrastructure not being runnable."""

    if not isinstance(extra, dict):
        return False
    ladder = extra.get("proof_ladder")
    if not isinstance(ladder, dict) or ladder.get("status") != "skipped":
        return False
    steps = ladder.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    return all(
        isinstance(step, dict)
        and step.get("status") == "skipped"
        and str(step.get("name") or "") in {
            "docker",
            "maestro",
            "playwright",
            "proof_selection",
        }
        for step in steps
    )


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
        # Codegen friction vents become low-severity "vent" lessons through the
        # same deduped add_lesson path — a vent that keeps recurring surfaces
        # exactly like any recurring finding. No parallel store.
        texts.extend(vent_lesson_texts(stack, build.get("vents")))
        texts = await self._drop_known(stack, texts)
        ids: list[int] = []
        stored: list[str] = []
        for text in texts:
            lid = await self._add_lesson(stack, stage, text, source_build)
            if lid is None:
                continue  # a concurrent capture stored it first — not a new lesson
            ids.append(lid)
            stored.append(text)
        if ids and self.event_bus is not None:
            await self._emit_captured(stack, stage, stored, ids, source_build)
        _info("learning.captured", stack=stack, stage=stage, count=len(ids))
        return ids

    async def _drop_known(self, stack: str, texts: list[str]) -> list[str]:
        """Capture-side dedupe: a lesson text already stored for this stack is
        NOT re-inserted — duplicates crowd the score-ranked injection top-5 with
        identical advice and split one lesson's helpful/hurt grading history
        across rows. Degrades open (keeps the text) when the store lacks
        ``lesson_exists`` (duck-typed stores) or the check errors."""
        if not texts:
            return []
        if self.store is None:
            mem = {m["text"] for m in self._mem if m.get("stack") == stack}
            return [t for t in texts if t not in mem]
        exists = getattr(self.store, "lesson_exists", None)
        if exists is None:
            return texts
        out: list[str] = []
        for text in texts:
            try:
                known = bool(await exists(stack, text))
            except Exception as exc:  # noqa: BLE001 - degrade open
                _info("learning.dedupe_check_failed", error=str(exc))
                known = False
            if not known:
                out.append(text)
        if len(out) < len(texts):
            _info("learning.dedupe_skipped", stack=stack, skipped=len(texts) - len(out))
        return out

    async def _add_lesson(
        self, stack: str, stage: str, text: str, source_build: Any
    ) -> int | None:
        if self.store is not None:
            try:
                return int(
                    await self.store.add_lesson(
                        stack, stage, text, source_build=source_build
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _info("learning.store_add_failed", error=str(exc))
                if await self._known_after_failed_add(stack, text):
                    return None
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

    async def _known_after_failed_add(self, stack: str, text: str) -> bool:
        """Whether a failed ``add_lesson`` lost the capture race.

        ``_drop_known`` -> ``add_lesson`` spans two store sessions, so a
        concurrent build can insert the identical (stack, text) between the
        check and the insert; the store's unique index then rejects ours. Such
        a text is already stored — buffering it in the in-memory fallback would
        mint the very duplicate the dedupe exists to prevent. Degrades to
        ``False`` (keep the fallback buffer) for duck-typed stores without
        ``lesson_exists`` and for genuine store outages.
        """
        exists = getattr(self.store, "lesson_exists", None)
        if exists is None:
            return False
        try:
            return bool(await exists(stack, text))
        except Exception:  # noqa: BLE001 - degrade to the fallback buffer
            return False

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

            if self.event_bus is None:
                return
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
        self, injected: InjectedLessons, *, helpful: bool, quality: float | None = None
    ) -> None:
        """Grade every injected lesson by the new build's outcome.

        ``quality`` (0..1) gives a continuous reward; omitted -> binary +1/-1.
        """
        for lid in injected.lesson_ids:
            await self._grade(lid, helpful, quality)
        _info(
            "learning.graded",
            count=len(injected.lesson_ids),
            helpful=helpful,
        )

    async def _grade(self, lesson_id: int, helpful: bool, quality: float | None = None) -> None:
        if self.store is not None:
            try:
                await self.store.grade_lesson(lesson_id, helpful, quality=quality)
                return
            except Exception as exc:  # noqa: BLE001
                _info("learning.store_grade_failed", error=str(exc))
        for r in self._mem:
            if r["id"] == lesson_id:
                r["helpful" if helpful else "unhelpful"] += 1
                break
