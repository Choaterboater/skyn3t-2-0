"""Planner — turn a brief into an ordered list of StageSpecs + a detected stack.

Stack detection is deterministic and offline (keyword heuristics over the brief).
The planner supports an optional *test-first* ordering (P1): when ``test_first``
is requested or ``settings.best_of_n > 1``, a ``test_author`` stage is placed
immediately before the ``code`` stage so tests anchor the implementation.

It also produces a *file-level plan checklist* (P2): a deterministic list of the
files the build is expected to deliver for the detected stack. The checklist is
attached to the plan and injected into the code stage payload so progress can be
verified file by file (delivered != empty).

Import has zero side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from skyn3t.studio.stages import StageSpec, default_pipeline, test_author_spec

# ---- stack detection -----------------------------------------------------
# Ordered: first matching signature wins. Each entry: (stack, keywords).
_STACK_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nextjs", ("next.js", "nextjs", "next ")),
    # react_native must precede `react`/`nextjs`: "react native expo app" and
    # "ios app" should resolve to the mobile stack, not the web React scaffold.
    ("react_native", (
        "mobile app", "react native", "react-native", "expo",
        "ios app", "android app", "mobile application",
    )),
    ("react", ("react", "vite", "spa", "single page", "frontend", "dashboard ui")),
    ("fastapi", ("fastapi", "rest api", "http api", "backend api", "endpoint")),
    ("flask", ("flask",)),
    ("django", ("django",)),
    ("express", ("express", "node api", "nodejs", "node.js")),
    ("cli", ("cli", "command line", "command-line", "terminal tool")),
    ("python", ("python", "script", "library", "package", "data")),
    ("static", ("static site", "landing page", "html", "website", "web site",
                "web app", "webapp", "web page", "webpage", "site")),
)

_DEFAULT_STACK = "python"

# ---- file-level checklist (P2) ------------------------------------------
# Minimal expected deliverable file set per stack. Used to verify the build is
# not an empty scaffold and to give the code stage a concrete target.
_STACK_FILE_CHECKLIST: dict[str, tuple[str, ...]] = {
    "fastapi": ("README.md", "requirements.txt", "main.py", "test_main.py"),
    "flask": ("README.md", "requirements.txt", "app.py", "tests/test_app.py"),
    "django": ("README.md", "requirements.txt", "manage.py", "project/settings.py"),
    "react": ("README.md", "package.json", "src/App.jsx", "src/main.jsx", "index.html"),
    "nextjs": ("README.md", "package.json", "app/page.tsx", "app/layout.tsx"),
    # Expo mobile app: the manifest (package.json), the Expo config (app.json),
    # the root screen (App.tsx) and a test. No index.html — mobile has no DOM
    # root and cannot be iframe-previewed like the web stacks.
    "react_native": ("README.md", "package.json", "app.json", "App.tsx", "__tests__/App.test.tsx"),
    "express": ("README.md", "package.json", "src/index.js", "test/app.test.js"),
    # CLI/python: a runnable root ``main.py`` (synthesized if the codegen only
    # produced a package) + a manifest + README. Tests are scored as substance,
    # not required as an exact path, so the checklist stops demanding files the
    # codegen never names (the old ``src/main.py`` could never be satisfied by a
    # package layout and forced the fix loop to stub-fill).
    "cli": ("README.md", "pyproject.toml", "main.py"),
    "python": ("README.md", "pyproject.toml", "main.py"),
    "static": ("README.md", "index.html", "styles.css", "main.js"),
}


def detect_stack(brief: str, hint: str | None = None) -> str:
    """Detect a target stack from the brief (deterministic, offline)."""
    if hint:
        h = hint.strip().lower()
        for stack, _ in _STACK_SIGNATURES:
            if h == stack:
                return stack
    low = (brief or "").lower()
    for stack, keywords in _STACK_SIGNATURES:
        if any(k in low for k in keywords):
            return stack
    return _DEFAULT_STACK


def file_checklist(stack: str) -> list[str]:
    """Return the deterministic expected-file checklist for a stack (P2)."""
    return list(_STACK_FILE_CHECKLIST.get(stack, _STACK_FILE_CHECKLIST[_DEFAULT_STACK]))


@dataclass(slots=True)
class BuildPlan:
    """The full plan produced by the planner."""

    slug: str
    brief: str
    stack: str
    stages: list[StageSpec]
    checklist: list[str] = field(default_factory=list)
    test_first: bool = False
    best_of_n: int = 1
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self.stages]

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "brief": self.brief,
            "stack": self.stack,
            "stages": [s.to_dict() for s in self.stages],
            "checklist": list(self.checklist),
            "test_first": self.test_first,
            "best_of_n": self.best_of_n,
            "notes": dict(self.notes),
        }


class Planner:
    """Brief -> :class:`BuildPlan`. Deterministic and offline by default."""

    def __init__(self, settings: Any | None = None) -> None:
        # settings is optional so the planner is usable without the full app.
        self._settings = settings

    def _resolve_test_first(self, explicit: bool | None, best_of_n: int) -> bool:
        if explicit is not None:
            return bool(explicit)
        # Best-of-N benefits from test-first anchoring (P0 x P1 synergy).
        return best_of_n > 1

    def _resolve_best_of_n(self, explicit: int | None) -> int:
        if explicit is not None:
            return max(1, int(explicit))
        if self._settings is not None:
            try:
                return max(1, int(getattr(self._settings, "best_of_n", 1)))
            except (TypeError, ValueError):
                return 1
        return 1

    def _critic_enabled(self) -> bool:
        if self._settings is not None:
            return bool(getattr(self._settings, "critic_enabled", True))
        return True

    def plan(
        self,
        brief: str,
        slug: str,
        *,
        stack_hint: str | None = None,
        test_first: bool | None = None,
        best_of_n: int | None = None,
        gated_stages: tuple[str, ...] = (),
    ) -> BuildPlan:
        """Produce an ordered :class:`BuildPlan` from a brief."""
        stack = detect_stack(brief, stack_hint)
        bon = self._resolve_best_of_n(best_of_n)
        tf = self._resolve_test_first(test_first, bon)

        stages = default_pipeline()

        # Drop the critic stage entirely when disabled (runner also skips, but
        # keeping the plan honest avoids a phantom stage).
        if not self._critic_enabled():
            stages = [s for s in stages if s.agent_type != "critic"]

        # Test-first ordering (P1): insert test_author immediately before code.
        if tf:
            insert_at = next(
                (i for i, s in enumerate(stages) if s.agent_type == "code"),
                len(stages),
            )
            stages.insert(insert_at, test_author_spec())

        # Apply any caller-requested approval gates by stage name.
        if gated_stages:
            gset = set(gated_stages)
            for s in stages:
                if s.name in gset:
                    s.gated = True

        checklist = file_checklist(stack)

        # Inject the checklist into the code stage's extra so the code agent (and
        # the proof-run) knows the concrete deliverable target.
        for s in stages:
            if s.agent_type == "code":
                s.extra["checklist"] = list(checklist)

        return BuildPlan(
            slug=slug,
            brief=brief,
            stack=stack,
            stages=stages,
            checklist=checklist,
            test_first=tf,
            best_of_n=bon,
            notes={"stack_hint": stack_hint or ""},
        )
