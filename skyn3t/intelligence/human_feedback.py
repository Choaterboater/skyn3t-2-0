"""Deterministic human design-feedback capture for future web builds.

This module deliberately turns free-form feedback into a *small fixed vocabulary*
of durable design rules.  The original feedback is not copied into lesson text:
that keeps a future prompt from treating arbitrary user prose as instructions,
while retaining the actual reusable signal (for example, prefer real photography
when a reviewer rejects synthetic people).

Lessons are stored under a shared ``human_design`` stack at the ``design`` stage.
The StudioRunner can merge that stack into every web-design build without mixing
these rules into unrelated application stacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HUMAN_DESIGN_LESSON_STACK = "human_design"
HUMAN_DESIGN_LESSON_STAGE = "design"

MAX_FEEDBACK_CHARS = 4_000
MAX_CONTEXT_CHARS = 1_000
MAX_DESIGN_LESSONS = 6

FEEDBACK_CATEGORIES = frozenset(
    {
        "visual",
        "content",
        "usability",
        "accessibility",
        "performance",
        "general",
    }
)


class HumanFeedbackValidationError(ValueError):
    """The submitted feedback cannot safely enter the learning loop."""


class HumanFeedbackPersistenceError(RuntimeError):
    """The durable lesson store was unavailable or rejected a capture."""


@dataclass(frozen=True, slots=True)
class HumanFeedback:
    """Validated feedback supplied by a human reviewer."""

    feedback: str
    category: str
    context: str
    rating: int | None


@dataclass(frozen=True, slots=True)
class CapturedFeedbackLesson:
    """One distilled rule and whether this request created it."""

    text: str
    captured: bool
    deduped: bool
    lesson_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": self.text,
            "captured": self.captured,
            "deduped": self.deduped,
        }
        if self.lesson_id is not None:
            payload["id"] = self.lesson_id
        return payload


@dataclass(frozen=True, slots=True)
class HumanFeedbackCaptureResult:
    """Persistence result returned to the web layer."""

    feedback: HumanFeedback
    lessons: tuple[CapturedFeedbackLesson, ...]
    stack: str = HUMAN_DESIGN_LESSON_STACK
    stage: str = HUMAN_DESIGN_LESSON_STAGE

    @property
    def captured(self) -> int:
        return sum(1 for lesson in self.lessons if lesson.captured)

    @property
    def deduped(self) -> int:
        return sum(1 for lesson in self.lessons if lesson.deduped)


def _normalize_text(value: Any, *, field: str, limit: int, required: bool) -> str:
    if value is None:
        if required:
            raise HumanFeedbackValidationError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise HumanFeedbackValidationError(f"{field} must be a string")
    if len(value) > limit:
        raise HumanFeedbackValidationError(f"{field} must be at most {limit} characters")
    # Permit normal multi-line text, but reject NUL and other non-printing control
    # characters before collapsing whitespace for deterministic keyword matching.
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise HumanFeedbackValidationError(f"{field} contains unsupported control characters")
    normalized = " ".join(value.split())
    if required and not normalized:
        raise HumanFeedbackValidationError(f"{field} is required")
    return normalized


def validate_human_feedback(
    feedback: Any,
    *,
    category: Any = None,
    context: Any = None,
    rating: Any = None,
) -> HumanFeedback:
    """Validate and normalize API input without coercing arbitrary objects."""
    normalized_feedback = _normalize_text(
        feedback,
        field="feedback",
        limit=MAX_FEEDBACK_CHARS,
        required=True,
    )
    normalized_context = _normalize_text(
        context,
        field="context",
        limit=MAX_CONTEXT_CHARS,
        required=False,
    )

    if category in (None, ""):
        normalized_category = "general"
    elif not isinstance(category, str):
        raise HumanFeedbackValidationError("category must be a string")
    else:
        normalized_category = category.strip().lower().replace("-", "_")
        if normalized_category not in FEEDBACK_CATEGORIES:
            choices = ", ".join(sorted(FEEDBACK_CATEGORIES))
            raise HumanFeedbackValidationError(f"category must be one of: {choices}")

    if rating is None or rating == "":
        normalized_rating = None
    elif isinstance(rating, bool) or not isinstance(rating, int):
        raise HumanFeedbackValidationError("rating must be an integer from 1 to 5")
    elif not 1 <= rating <= 5:
        raise HumanFeedbackValidationError("rating must be an integer from 1 to 5")
    else:
        normalized_rating = rating

    return HumanFeedback(
        feedback=normalized_feedback,
        category=normalized_category,
        context=normalized_context,
        rating=normalized_rating,
    )


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _append_rule(rules: list[str], rule: str) -> None:
    if rule not in rules and len(rules) < MAX_DESIGN_LESSONS:
        rules.append(rule)


def distill_design_lessons(feedback: HumanFeedback) -> list[str]:
    """Turn reviewer feedback into fixed, reusable web-design rules.

    This is intentionally heuristic rather than generative.  The output never
    quotes reviewer text or context, so feedback cannot become prompt-injection
    content simply by being saved as a lesson.
    """
    signal = f"{feedback.feedback} {feedback.context}".lower()
    rules: list[str] = []

    image_words = ("image", "images", "photo", "photos", "photograph", "picture", "hero")
    people_words = (
        "person", "people", "human", "guy", "man", "woman", "model", "face",
        "hand", "hands", "body", "portrait", "golfer",
    )
    synthetic_words = ("generated", "synthetic", "artificial", "ai image", "ai photo")
    real_photo_words = (
        "real photo", "real photos", "real photograph", "real photography",
        "editorial photo", "editorial photography", "stock photo", "stock photography",
    )
    has_people = _contains_any(signal, people_words)
    has_synthetic_people = has_people and _contains_any(signal, synthetic_words)
    wants_real_photos = _contains_any(signal, real_photo_words)

    if has_synthetic_people or wants_real_photos:
        _append_rule(
            rules,
            "Use real, rights-cleared editorial photography for people; do not use generated human imagery unless explicitly requested.",
        )
    if has_people and _contains_any(
        signal,
        ("weird", "uncanny", "wrong", "bad", "off", "awkward", "hard hat", "anatomy"),
    ):
        _append_rule(
            rules,
            "Verify people imagery for believable anatomy, hands, faces, and apparel before delivery.",
        )
    if _contains_any(signal, image_words) and _contains_any(
        signal,
        ("crop", "framing", "focal", "weird", "wrong", "bad", "off", "hero"),
    ):
        _append_rule(
            rules,
            "Select imagery with an intentional crop, clear focal subject, and a composition that supports the page hierarchy.",
        )

    if _contains_any(signal, ("font", "fonts", "typeface", "typography")) or (
        _contains_any(signal, ("title", "headline", "heading", "h1"))
        and _contains_any(signal, ("wack", "weird", "awkward", "bad", "off", "generic"))
    ):
        _append_rule(
            rules,
            "Use deliberate typography and a clear headline hierarchy; avoid generic pairings and awkward display copy.",
        )
    if _contains_any(
        signal,
        ("css", "layout", "spacing", "alignment", "grid", "template", "assembled", "put together", "amateur"),
    ):
        _append_rule(
            rules,
            "Keep layout, spacing, and alignment intentional so the result feels designed rather than assembled from a template.",
        )

    color_words = ("color", "colors", "colour", "colours", "palette", "theme")
    positive_words = ("better", "good", "great", "like", "love", "cohesive", "improved")
    negative_color_words = ("bad color", "bad colors", "wrong color", "wrong colors", "color clash", "colors clash", "low contrast", "poor contrast", "washed-out", "washed out", "muddy color", "muddy colors")
    if _contains_any(signal, color_words):
        if _contains_any(signal, negative_color_words):
            _append_rule(
                rules,
                "Use a coherent palette with sufficient contrast and hierarchy; avoid competing or washed-out color combinations.",
            )
        elif _contains_any(signal, positive_words):
            _append_rule(
                rules,
                "Preserve the cohesive, restrained color palette when revising the page.",
            )

    if feedback.category == "usability" or _contains_any(
        signal,
        ("doesn't work", "doesnt work", "does not work", "button", "link", "navigation", "nav"),
    ):
        _append_rule(
            rules,
            "Keep navigation and primary actions discoverable, clearly labeled, and verified to work before delivery.",
        )
    if feedback.category == "accessibility" or _contains_any(
        signal,
        ("accessibility", "accessible", "keyboard", "screen reader", "contrast"),
    ):
        _append_rule(
            rules,
            "Meet accessible contrast, semantic-heading, and keyboard-interaction expectations in the finished web experience.",
        )
    if feedback.category == "performance" or _contains_any(
        signal,
        ("slow", "performance", "loading", "load time", "heavy", "large image"),
    ):
        _append_rule(
            rules,
            "Optimize visual assets and loading behavior so high-quality presentation does not come at the cost of page performance.",
        )

    if feedback.rating is not None and feedback.rating <= 2:
        _append_rule(
            rules,
            "Do not consider a web design complete until a human reviewer confirms the reported visual defects are resolved.",
        )
    elif feedback.rating is not None and feedback.rating >= 4:
        _append_rule(
            rules,
            "Preserve reviewer-approved visual direction while making narrowly scoped, evidence-based improvements.",
        )

    if not rules:
        fallbacks = {
            "content": "Make page copy concise, credible, and specific to the product; review it for a clear hierarchy before delivery.",
            "usability": "Run a human usability review of navigation and primary actions before delivery.",
            "accessibility": "Run an accessibility review of contrast, semantics, and keyboard interaction before delivery.",
            "performance": "Review page performance and visual-asset weight before delivery.",
            "visual": "Run a final visual review of imagery, typography, layout, and color coherence before delivery.",
            "general": "Apply human design review findings before delivery and verify the result against the requested quality bar.",
        }
        _append_rule(rules, fallbacks[feedback.category])

    return rules[:MAX_DESIGN_LESSONS]


async def _lesson_exists(store: Any, text: str) -> bool:
    checker = getattr(store, "lesson_exists", None)
    if not callable(checker):
        return False
    return bool(await checker(HUMAN_DESIGN_LESSON_STACK, text))


async def _emit_captured(
    event_bus: Any,
    *,
    lessons: list[str],
    lesson_ids: list[int],
    source_build: str | None,
    category: str,
    rating: int | None,
) -> None:
    if event_bus is None or not lessons:
        return
    try:
        from skyn3t.core.events import EventType

        await event_bus.emit(
            EventType.LESSON_CAPTURED,
            source="intelligence.human_feedback",
            payload={
                "stack": HUMAN_DESIGN_LESSON_STACK,
                "stage": HUMAN_DESIGN_LESSON_STAGE,
                "lesson_ids": lesson_ids,
                "lessons": lessons,
                "source_build": source_build,
                "human_feedback": True,
                "category": category,
                "rating": rating,
            },
            correlation_id=source_build,
        )
    except Exception:
        # The durable capture has already succeeded.  Event emission is an
        # observability side effect, never a reason to reject the human input.
        return


async def capture_human_design_feedback(
    store: Any,
    *,
    feedback: Any,
    category: Any = None,
    context: Any = None,
    rating: Any = None,
    source_build: Any = None,
    event_bus: Any = None,
) -> HumanFeedbackCaptureResult:
    """Validate, distill, dedupe, and persist human design feedback.

    A persistent ``MemoryStore`` is required: silently falling back to volatile
    memory would make the UI claim it learned when the next server restart would
    forget the feedback.
    """
    item = validate_human_feedback(
        feedback,
        category=category,
        context=context,
        rating=rating,
    )
    adder = getattr(store, "add_lesson", None)
    if store is None or not callable(adder):
        raise HumanFeedbackPersistenceError("the persistent learning store is unavailable")

    source = str(source_build or "").strip()[:64] or None
    captured_texts: list[str] = []
    captured_ids: list[int] = []
    results: list[CapturedFeedbackLesson] = []

    for text in distill_design_lessons(item):
        try:
            known = await _lesson_exists(store, text)
        except Exception as exc:  # noqa: BLE001 - fail closed; never claim persistence
            raise HumanFeedbackPersistenceError("the persistent learning store is unavailable") from exc
        if known:
            results.append(CapturedFeedbackLesson(text=text, captured=False, deduped=True))
            continue
        try:
            raw_id = await adder(
                HUMAN_DESIGN_LESSON_STACK,
                HUMAN_DESIGN_LESSON_STAGE,
                text,
                source_build=source,
            )
            lesson_id = int(raw_id) if raw_id is not None else None
        except Exception as exc:  # a unique-index conflict may mean we lost a race
            try:
                won_by_peer = await _lesson_exists(store, text)
            except Exception:
                won_by_peer = False
            if won_by_peer:
                results.append(CapturedFeedbackLesson(text=text, captured=False, deduped=True))
                continue
            raise HumanFeedbackPersistenceError("the persistent learning store could not save feedback") from exc

        captured_texts.append(text)
        if lesson_id is not None:
            captured_ids.append(lesson_id)
        results.append(
            CapturedFeedbackLesson(
                text=text,
                captured=True,
                deduped=False,
                lesson_id=lesson_id,
            )
        )

    await _emit_captured(
        event_bus,
        lessons=captured_texts,
        lesson_ids=captured_ids,
        source_build=source,
        category=item.category,
        rating=item.rating,
    )
    return HumanFeedbackCaptureResult(feedback=item, lessons=tuple(results))
