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

import re
from dataclasses import dataclass, field
from typing import Any

from skyn3t.studio.stages import StageSpec, default_pipeline, test_author_spec

# ---- stack detection -----------------------------------------------------
# Ordered: first matching signature wins. Each entry: (stack, keywords).
_STACK_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nextjs", ("next.js", "nextjs", "next ")),
    # Astro / Remix are React-family / web frameworks with their own real builder;
    # match them before the generic `react` and `static` signatures so their
    # briefs don't collapse to the Vite or plain-HTML scaffold.
    ("astro", ("astro",)),
    ("remix", ("remix",)),
    # MCP server (Model Context Protocol, wave-2 §3.3): a Python stdio tool server
    # exposing tools to AI assistants — NOT a web/HTTP app. Placed HIGH (before
    # react_native / swift / tauri / phaser / react / fastapi / static): an "mcp
    # server that exposes X" brief contains "server" (fastapi/express territory),
    # "tool" (generic), AND "exposes" (whose prefix "expo" the react_native
    # signature substring-matches) — so without an earlier MCP-SPECIFIC signature
    # it collapses to a web/API/mobile scaffold. Keywords are MCP-distinctive: bare
    # "mcp" is word-bounded (via _WORD_BOUNDED_KEYWORDS) so it only fires on the
    # standalone token, never buried in another word; the spelled-out "model
    # context protocol" needs no bounding. We deliberately do NOT claim the
    # ambiguous bare "tool server" phrase (it would steal "a tool for my server") —
    # an MCP brief always carries "mcp" or "model context protocol". Placing mcp
    # before react_native etc. is safe: no genuine mobile/desktop/game/web brief
    # contains "mcp" or "model context protocol".
    ("mcp", ("model context protocol", "mcp")),
    # RAG "chat with your documents" app (wave-2 §3.1): a FastAPI retrieval app —
    # NOT the generic web/API scaffold. Placed HIGH (right after mcp, before
    # react_native / react / fastapi / static): a RAG brief almost always carries
    # "app"/"service"/"ai" (react/fastapi/static territory), so without an earlier
    # RAG-SPECIFIC signature it collapses to a web scaffold with no retrieval.
    # Keywords are RAG-distinctive multi-word phrases; bare "rag" is word-bounded
    # (via _WORD_BOUNDED_KEYWORDS) so "storage"/"dragging" never route here. Bare
    # "search"/"document" are deliberately NOT keywords (the phaser bare-"game"
    # lesson: "a search bar for my store" / "document my api" must not be stolen);
    # only "semantic search" / "document q&a" — phrases that NAME retrieval — match.
    ("rag", ("retrieval augmented", "retrieval-augmented", "rag",
             "chat with my doc", "chat with your doc", "chat with my pdf",
             "chat with my notes", "chat with my files",
             "knowledge base", "document q&a", "doc q&a", "semantic search")),
    # Agent-workflow app (wave-2 §3.2, demand #2): a multi-step runner with a
    # run ledger and dry-run triggers — a FastAPI app, NOT the generic web/API
    # scaffold. Placed after rag (no keyword overlap) and before react_native /
    # react / fastapi: workflow briefs carry "app"/"api"/"service". Keywords are
    # WORKFLOW-distinctive: bare "workflow" is word-bounded singular only (a
    # "dashboard for my workflows" is a dashboard — plural doesn't match);
    # bare "automation"/"pipeline"/"scheduled" are deliberately NOT claimed
    # (they'd steal CI-dashboard / data-pipeline / calendar briefs).
    ("workflow", ("agent workflow", "workflow", "team of agents",
                  "multi-agent", "multi agent", "agent that",
                  "automation pipeline", "monitor and notify",
                  "scheduled briefing", "daily briefing")),
    # react_native must precede `react`/`nextjs`: "react native expo app" and
    # "ios app" should resolve to the mobile stack, not the web React scaffold.
    ("react_native", (
        "mobile app", "react native", "react-native", "expo",
        "ios app", "android app", "mobile application",
    )),
    # Swift / SwiftUI native macOS must precede tauri AND react/static: a brief
    # like "a swiftui macos app" contains "macos app" (a Tauri keyword) and "app"
    # (react/static), so without an earlier, SWIFT-SPECIFIC signature it would
    # collapse to the cross-platform Tauri or the React scaffold. Keywords are
    # deliberately Swift-DISTINCTIVE (they name Swift/SwiftUI/SPM) so we never
    # steal Tauri's ambiguous bare "mac app"/"macos app" briefs — those, with no
    # Swift signal, still fall through to tauri below. Bare "swift" is omitted (it
    # steals "a swift/fast web app", "taylor swift fan site"). Phrases that END in
    # "swift" (macos swift / written in swift / built with swift) plus "spm" are
    # matched whole-word via _WORD_BOUNDED_KEYWORDS so "swiftly"/"swiftness" and
    # "raspmelody" don't false-route; the swift-LEADING phrases are safe as plain
    # substrings (they only extend into other swift wording).
    ("swift", ("swiftui", "swift app", "swift macos", "swift package",
               "swiftpm", "swift native", "spm", "macos swift",
               "written in swift", "built with swift")),
    # Tauri desktop must precede react/static — a desktop app uses a React frontend
    # and may say "app"/"web", but it bundles to a native Mac/Windows binary.
    ("tauri", ("desktop app", "tauri", "mac app", "macos app", "windows app",
               "native app", "desktop application", "standalone app", "menu bar app",
               "cross-platform app", "desktop")),
    # Phaser game must precede react/static: a "single-page browser game" or an
    # "html5 game" (contains "html") would otherwise collapse to the React or
    # plain-HTML scaffold. First-matching signature wins. Bare "game" is omitted
    # (it steals "board game tracker" / "video game blog"); the ambiguous single
    # words (arcade/platformer/shooter) are matched whole-word via
    # _WORD_BOUNDED_KEYWORDS so "troubleshooter"/"endgame"/"barcade" don't match.
    ("phaser", ("phaser", "arcade", "platformer", "browser game",
                "html5 game", "2d game", "game engine", "shooter",
                "side-scroller", "endless runner", "tower defense")),
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
    # Tauri desktop: the Vite/React frontend entry + the Tauri shell config. The
    # src-tauri Rust shell is fixed boilerplate; `npm run build` builds the frontend.
    "tauri": ("README.md", "package.json", "index.html", "src/main.jsx",
              "src-tauri/tauri.conf.json"),
    # Phaser 3 + Vite game: the HTML entry + the vanilla-JS game module. Note
    # src/main.js (NOT main.jsx) — a Phaser game is plain JS, not React.
    "phaser": ("README.md", "package.json", "index.html", "src/main.js"),
    # Swift / SwiftUI native macOS (Swift Package Manager): the SwiftPM manifest is
    # the load-bearing artifact (it declares the executable + test targets). NO
    # package.json / index.html — this stack has no npm and no DOM.
    "swift": ("README.md", "Package.swift"),
    # MCP server (Python stdio Model Context Protocol): the runnable server is the
    # load-bearing artifact, its tools split into a pure tools.py, plus the SDK
    # manifest. NO package.json / index.html — this stack is a stdio program.
    "mcp": ("README.md", "server.py", "tools.py", "requirements.txt"),
    # RAG "chat with your documents" app: the FastAPI server is the load-bearing
    # artifact, its chunker/retriever split into a PURE rag_core.py (the sim-core
    # split reapplied), plus the Python manifest. NO package.json / index.html.
    "rag": ("README.md", "main.py", "rag_core.py", "requirements.txt"),
    # Agent-workflow app: the FastAPI runner is the load-bearing artifact, its
    # engine split into a PURE workflow_core.py (steps as pure functions, run
    # ledger, typed errors), plus the Python manifest. NO package.json.
    "workflow": ("README.md", "main.py", "workflow_core.py", "requirements.txt"),
    # Next.js App Router: the offline scaffold ships plain JSX (no TypeScript
    # toolchain), so the runnable root page/layout are .jsx, plus the config.
    "nextjs": ("README.md", "package.json", "app/page.jsx", "app/layout.jsx", "next.config.js"),
    # Astro: the manifest, an entry page, and the astro config.
    "astro": ("README.md", "package.json", "src/pages/index.astro", "astro.config.mjs"),
    # Remix (Vite): the manifest, the root document, and the index route.
    "remix": ("README.md", "package.json", "app/root.tsx", "app/routes/_index.tsx"),
    # Expo mobile app: the manifest (package.json), the Expo config (app.json),
    # and the root screen (App.tsx). No index.html — mobile has no DOM root and
    # cannot be iframe-previewed like the web stacks. (No jest test in the
    # checklist — the scaffold ships no jest runner, so scoring one was theater.)
    "react_native": ("README.md", "package.json", "app.json", "App.tsx"),
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


# Briefs implying an LLM call must NOT resolve to a server-less web stack: 'react'
# (Vite SPA) and 'static' (npx serve .) have no backend to hold OPENROUTER_API_KEY,
# so the call would run in the browser and leak the key. Such briefs are bumped to
# nextjs, whose single `npm run dev` co-hosts app/api/* route handlers in one
# process. Matched as WHOLE WORDS (so "task completion", "claudette", "email",
# "retail" don't false-bump) — word boundaries make bare "ai" safe ("with AI"
# matches; "em-ai-l" does not).
_SERVERLESS_WEB_STACKS = frozenset({"react", "static"})
_LLM_KEYWORDS: tuple[str, ...] = (
    "ai", "llm", "language model", "gpt", "chatgpt", "openai", "anthropic",
    "claude", "gemini", "openrouter", "chatbot", "chat bot",
)
_LLM_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in _LLM_KEYWORDS) + r")\b")


def detect_stack(brief: str, hint: str | None = None) -> str:
    """Detect a target stack from the brief (deterministic, offline)."""
    if hint:
        h = hint.strip().lower()
        for stack, _ in _STACK_SIGNATURES:
            if h == stack:
                return stack
    low = (brief or "").lower()
    stack = _DEFAULT_STACK
    for s, keywords in _STACK_SIGNATURES:
        if any(_kw_match(k, low) for k in keywords):
            stack = s
            break
    # LLM brief on a server-less web stack -> bump to nextjs so a server route can
    # hold the OpenRouter key (single `npm run dev` previews UI + app/api routes).
    if stack in _SERVERLESS_WEB_STACKS and _LLM_RE.search(low):
        return "nextjs"
    return stack


# Bare framework names that are substrings of common English words: match them
# only as whole words so "astrology"/"astronomy"/"gastro" don't route to astro
# and "remixing" doesn't route to remix.
_WORD_BOUNDED_KEYWORDS = frozenset({
    "astro", "remix",
    # Single-word game genres: match whole words so "troubleshooter" (≠ shooter),
    # "endgame" (≠ game) and "barcade" (≠ arcade) don't false-route to phaser.
    "arcade", "platformer", "shooter",
    # SwiftPM abbreviation: whole-word so it only fires on the standalone token,
    # never as a substring buried in an unrelated word.
    "spm",
    # RAG: whole-word so bare "rag" only routes on the standalone token, never as
    # a substring ("storage", "average", "dragging").
    "rag",
    # Agent-workflow: whole-word SINGULAR so "workflows" (a dashboard/CRUD noun)
    # and "workflowy" never route here.
    "workflow",
    # MCP (Model Context Protocol): whole-word so bare "mcp" only routes on the
    # standalone token, never as a substring inside an unrelated word.
    "mcp",
    # Swift phrases that END in "swift": whole-word so the trailing token can't be
    # "swiftly"/"swiftness" (which are NOT the Swift language).
    "macos swift", "written in swift", "built with swift",
})


def _kw_match(keyword: str, text: str) -> bool:
    if keyword in _WORD_BOUNDED_KEYWORDS:
        return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
    return keyword in text


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
