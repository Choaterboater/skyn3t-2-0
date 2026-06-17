"""Clarification — turn an ambiguous brief into clarifying questions.

Detects under-specified briefs (missing stack, auth, persistence, audience, etc.)
and produces a small set of clarifying questions. In unattended mode it
auto-answers each question with a safe, cheap default so the build proceeds
without a human (degrade, don't crash — design rule #6).

Deterministic and offline; import has zero side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ClarifyingQuestion:
    key: str
    question: str
    default: str
    options: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "question": self.question,
            "default": self.default,
            "options": list(self.options),
        }


@dataclass(slots=True)
class ClarificationResult:
    ambiguous: bool
    questions: list[ClarifyingQuestion] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    auto_answered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ambiguous": self.ambiguous,
            "questions": [q.to_dict() for q in self.questions],
            "answers": dict(self.answers),
            "auto_answered": self.auto_answered,
        }


# Each probe: (key, trigger-keywords-that-mean-ALREADY-specified, question, default, options).
_PROBES: tuple[tuple[str, tuple[str, ...], str, str, tuple[str, ...]], ...] = (
    (
        "stack",
        ("python", "react", "fastapi", "flask", "django", "node", "next", "cli", "static", "html"),
        "What technology stack should this target?",
        "python",
        ("python", "fastapi", "react", "cli", "static"),
    ),
    (
        "persistence",
        ("database", "db", "sqlite", "postgres", "store", "persist", "save"),
        "Does it need to persist data, and if so where?",
        "sqlite",
        ("none", "sqlite", "postgres", "file"),
    ),
    (
        "auth",
        ("auth", "login", "user", "account", "sign in", "signin", "oauth"),
        "Does it need user authentication?",
        "none",
        ("none", "token", "oauth", "password"),
    ),
    (
        "audience",
        ("internal", "public", "team", "customer", "personal", "enterprise"),
        "Who is the primary audience?",
        "internal",
        ("internal", "public", "personal"),
    ),
    (
        "tests",
        ("test", "tdd", "pytest", "coverage"),
        "Should the build include automated tests?",
        "yes",
        ("yes", "no"),
    ),
)

# Briefs shorter than this many words are considered inherently ambiguous.
_MIN_WORDS = 6


def analyze(brief: str) -> ClarificationResult:
    """Inspect a brief and return clarifying questions for missing aspects."""
    text = (brief or "").strip()
    low = text.lower()
    questions: list[ClarifyingQuestion] = []

    too_short = len(text.split()) < _MIN_WORDS

    for key, specified_kw, question, default, options in _PROBES:
        already = any(kw in low for kw in specified_kw)
        if not already:
            questions.append(
                ClarifyingQuestion(key=key, question=question, default=default, options=options)
            )

    ambiguous = bool(questions) or too_short
    return ClarificationResult(ambiguous=ambiguous, questions=questions)


def auto_answer(result: ClarificationResult, overrides: dict[str, str] | None = None) -> ClarificationResult:
    """Fill answers with defaults (or overrides) for unattended builds."""
    overrides = overrides or {}
    answers: dict[str, str] = {}
    for q in result.questions:
        answers[q.key] = overrides.get(q.key, q.default)
    return ClarificationResult(
        ambiguous=result.ambiguous,
        questions=result.questions,
        answers=answers,
        auto_answered=True,
    )


def clarify(
    brief: str,
    *,
    unattended: bool = True,
    overrides: dict[str, str] | None = None,
) -> ClarificationResult:
    """End-to-end: analyze then (if unattended) auto-answer."""
    result = analyze(brief)
    if unattended and result.ambiguous:
        return auto_answer(result, overrides)
    if overrides:
        result.answers.update(overrides)
    return result
