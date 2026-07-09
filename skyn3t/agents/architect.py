"""ArchitectAgent — produces the build plan (STRONG tier).

Consumes the brief plus prior brainstorm/research outputs and emits a
structured plan: the stack, the file list to generate, and the build order.
The plan's ``files`` drive the CodeAgent. Offline, it derives a sane plan
from the detected stack's scaffold so the pipeline always has work to do.
"""

from __future__ import annotations

from typing import Any

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, knowledge_block, parse_json, slugify
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

_SYSTEM = (
    "You are a senior software architect. Design a COMPLETE, production-grade "
    "build plan for the brief — NOT a minimal stub or single-file script. "
    "Decompose into MULTIPLE files with real separation of concerns: an entry "
    "point, core/domain logic split across modules, UI components/routes (for "
    "web apps), data models, utilities, config, and tests. A real application is "
    "many cohesive files, each with one clear purpose. Respond with JSON: "
    '{"stack": str, "summary": str, "files": [{"path": str, "purpose": str}], '
    '"build_order": [str], "components": [str]}. Include EVERY file needed for a '
    "fully-featured implementation. Full applications commonly need 12-30 cohesive "
    "files; do not compress the product into a small scaffold. Keep purposes concise "
    "so the response remains valid JSON."
)

_FULL_APP_ARCHITECT_TOKENS = 12_000
_STANDARD_ARCHITECT_TOKENS = 4_096

_BINARY_ASSET_SUFFIXES = frozenset({
    ".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".mp3", ".mp4",
    ".ogg", ".otf", ".pdf", ".png", ".ttf", ".wav", ".webm", ".webp",
    ".woff", ".woff2",
})

# A model plan remains the primary architecture. These entries are the deterministic
# recovery contract when structured output is truncated or omits an explicit feature
# named in the brief. Keeping the map small and product-oriented avoids guessing an
# implementation while still making requested screens independently provable.
_FEATURE_ROUTES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("lesson", "learning path"), "lessons", "Lesson paths and progression"),
    (("drill", "practice routine"), "drills", "Practical drills and routines"),
    (("equipment", "gear"), "equipment", "Equipment guidance"),
    (("tutorial", "resource"), "resources", "Tutorial and resource library"),
    (("tee-time", "tee time", "booking", "appointment"), "book", "Booking call to action and form"),
    (("service",), "services", "Service overview"),
    (("financ",), "financing", "Financing options and call to action"),
    (("review", "testimonial"), "reviews", "Customer reviews and trust proof"),
    (("emergency",), "emergency", "Emergency contact workflow"),
    (("contact",), "contact", "Contact details and validated inquiry form"),
    (("pricing", "plans"), "pricing", "Pricing or plan comparison"),
    (("faq",), "faq", "Frequently asked questions"),
)


def _merge_file_plans(*groups: list[Any]) -> list[dict[str, str]]:
    """Merge model and recovery files by path while preserving model intent."""
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if isinstance(item, str):
                path, purpose = item.strip(), "Required project file"
            elif isinstance(item, dict):
                path = str(item.get("path") or "").strip()
                purpose = str(item.get("purpose") or "Required project file").strip()
            else:
                continue
            key = path.replace("\\", "/").lower()
            if not path or key in seen:
                continue
            seen.add(key)
            merged.append({"path": path, "purpose": purpose})
    return merged


def _drop_binary_asset_plans(files: list[Any]) -> list[Any]:
    """Binary media is generated upstream, never authored by text codegen."""
    out: list[Any] = []
    for item in files:
        raw = item.get("path") if isinstance(item, dict) else item
        path = str(raw or "").replace("\\", "/").split("?", 1)[0]
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if suffix in _BINARY_ASSET_SUFFIXES:
            continue
        out.append(item)
    return out


def _page_route_identity(raw: Any, stack: str) -> str | None:
    """Canonical URL identity for collision-prone file-routed web pages."""
    if isinstance(raw, dict):
        path = str(raw.get("path") or raw.get("file") or "")
    else:
        path = str(raw or "")
    path = path.strip().replace("\\", "/").lstrip("/").casefold()
    if stack == "astro":
        if path.startswith("src/pages/"):
            path = path[len("src/pages/"):]
        elif path.startswith("pages/"):
            path = path[len("pages/"):]
        else:
            return None
        if not path.endswith(".astro"):
            return None
        route = path[:-len(".astro")]
    elif stack == "static_html":
        if not path.endswith((".html", ".htm")):
            return None
        suffix = ".html" if path.endswith(".html") else ".htm"
        route = path[:-len(suffix)]
    else:
        return None
    if route == "index":
        route = ""
    elif route.endswith("/index"):
        route = route[:-len("/index")]
    return "/" + route.strip("/")


def _augment_full_app_files(
    model_files: list[Any],
    recovery_files: list[Any],
    stack: str,
) -> list[dict[str, str]]:
    """Append recovery files without creating duplicate file-routed URLs."""
    primary = _merge_file_plans(model_files)
    route_ids = {
        route
        for item in primary
        if (route := _page_route_identity(item, stack)) is not None
    }
    additions: list[Any] = []
    for item in recovery_files:
        route = _page_route_identity(item, stack)
        if route is not None and route in route_ids:
            continue
        additions.append(item)
        if route is not None:
            route_ids.add(route)
    return _merge_file_plans(primary, additions)


def _brief_feature_routes(brief: str) -> list[tuple[str, str]]:
    text = brief.lower()
    routes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for needles, route, purpose in _FEATURE_ROUTES:
        if any(needle in text for needle in needles) and route not in seen:
            routes.append((route, purpose))
            seen.add(route)

    # Service businesses need individually navigable service detail pages, not
    # only a generic services hero. These paths are useful even when the brief
    # says only "HVAC" rather than spelling out heating and cooling separately.
    if any(term in text for term in ("hvac", "air conditioning", "heating and cooling")):
        for route, purpose in (
            ("heating", "Heating repair and installation service details"),
            ("cooling", "Cooling repair and installation service details"),
            ("maintenance", "Preventive maintenance service details"),
        ):
            if route not in seen:
                routes.append((route, purpose))
                seen.add(route)
    return routes


def _full_app_recovery_files(brief: str, stack: str) -> list[dict[str, str]]:
    """Return a brief-derived, stack-native floor for web full applications."""
    routes = _brief_feature_routes(brief)
    if stack == "astro":
        shared = [
            {"path": "src/components/SiteHeader.astro", "purpose": "Responsive site navigation"},
            {"path": "src/components/SiteFooter.astro", "purpose": "Finished footer and contact links"},
            {"path": "src/components/CallToAction.astro", "purpose": "Reusable primary conversion action"},
            {"path": "src/components/FeatureCard.astro", "purpose": "Reusable accessible content card"},
            {"path": "src/data/site.js", "purpose": "Typed product content and sample data"},
            {"path": "src/styles/global.css", "purpose": "Responsive tokens and global layout styles"},
            {"path": "src/pages/404.astro", "purpose": "Useful not-found recovery page"},
            {"path": "tests/site-contract.test.mjs", "purpose": "Brief feature and route contract tests"},
        ]
        return shared + [
            {"path": f"src/pages/{route}.astro", "purpose": purpose}
            for route, purpose in routes
        ]
    if stack == "static_html":
        shared = [
            {"path": "data/site.js", "purpose": "Structured product content and sample data"},
            {"path": "tests/site-contract.test.mjs", "purpose": "Brief feature and page contract tests"},
            {"path": "404.html", "purpose": "Useful not-found recovery page"},
        ]
        return shared + [
            {"path": f"{route}.html", "purpose": purpose}
            for route, purpose in routes
        ]
    return []

# Plain-JS Vite+React stacks whose scaffold floor is .jsx — the architect must
# plan .jsx (not .tsx), else the .jsx scaffold main + index.html render the
# counter stub instead of the real (TS) app.
_JSX_STACKS = frozenset({"react", "react_vite", "vite"})

# Next.js scaffold floor is plain-.jsx App Router (app/page.jsx, jsconfig.json) —
# the architect must plan .jsx (no tsconfig) AND a SINGLE router (App Router),
# never a pages/ tree, else it collides with the scaffold's app/page on '/'.
_NEXT_STACKS = frozenset({"nextjs", "next"})

# Phaser games are a vanilla-JS Vite app (scaffold floor is src/main.js — NO
# React). Same .ts-vs-.js mismatch class as _JSX_STACKS: a .ts plan would leave
# the .js scaffold entry rendering the starter stub. Plan plain .js + Phaser
# Scenes, never React/.tsx. Kept OUT of _JSX_STACKS so the React directive +
# .tsx->.jsx rewrite (both wrong for a non-React game) never fire.
# (The registry object — see skyn3t/core/stacks.py + the drift test.)
from skyn3t.core.stacks import GAME_STACKS as _GAME_STACKS  # noqa: E402

# Swift / SwiftUI native macOS (Swift Package Manager): plan a SwiftPM package
# (Package.swift + Sources/App + a pure Sources/AppCore + Tests), never web files.
_SWIFT_STACKS = frozenset({"swift"})


def _jsx_only(files: list[Any]) -> list[Any]:
    """Rewrite .tsx->.jsx / .ts->.js and drop tsconfig for a plain-JS React plan."""
    out: list[Any] = []
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            out.append(f)
            continue
        path = str(f["path"])
        low = path.lower()
        if low.endswith("tsconfig.json") or low.endswith(".d.ts"):
            continue  # no TS config in a plain-JS project
        if path.endswith(".tsx"):
            path = path[:-4] + ".jsx"
        elif path.endswith(".ts"):
            path = path[:-3] + ".js"
        out.append({**f, "path": path})
    return out


def _plain_js(files: list[Any]) -> list[Any]:
    """Rewrite .tsx/.ts->.js and drop tsconfig for a plain-JS, non-React plan
    (Phaser games): the entry is src/main.js, so .jsx would be just as wrong."""
    out: list[Any] = []
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            out.append(f)
            continue
        path = str(f["path"])
        low = path.lower()
        if low.endswith("tsconfig.json") or low.endswith(".d.ts"):
            continue
        if path.endswith(".tsx"):
            path = path[:-4] + ".js"
        elif path.endswith(".ts"):
            path = path[:-3] + ".js"
        out.append({**f, "path": path})
    return out


class ArchitectAgent(BaseAgent):
    def __init__(self, name: str = "architect", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="architecture", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="architecture", description="Design the build plan and file list",
            tags=("generative", "planning")))
        self.llm = llm or LLMClient()

    async def initialize(self) -> None:
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        brief = p.get("brief", "") or p.get("slug", "app")
        raw_extra = p.get("extra")
        extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
        full_app = bool(extra.get("full_app_contract"))
        prior = p.get("prior", {}) if isinstance(p.get("prior"), dict) else {}
        research = prior.get("research", {}) if isinstance(prior.get("research"), dict) else {}
        stack = detect_stack(
            brief=brief, plan=p.get("plan"),
            explicit=p.get("stack", "") or research.get("stack", ""),
        )

        prompt = (
            knowledge_block(p)
            + f"Brief: {brief}\n"
            + f"Detected stack: {stack}\n"
            + f"Research: {research}\n\n"
            + "Design the COMPLETE, multi-file build plan as JSON — every module, "
            + "component, model, utility, config, and test needed for a real, "
            + "fully-featured implementation of the brief. Not a minimal stub."
            + " Do NOT plan binary image, font, audio, video, or PDF files: those "
            + "are generated upstream and codegen must use the exact asset paths "
            + "listed in prior knowledge instead of fabricating media bytes."
        )
        # Reference image ("build from a picture") is intentionally NOT attached
        # here: forcing a vision model would downgrade this Tier.STRONG planning
        # call to a weaker generic vision model. The DesignerAgent consumes the
        # image (visual matching is its job); the architect keeps full strength.
        if stack in _JSX_STACKS:
            prompt += ("\n\nIMPORTANT: this is a plain JavaScript Vite + React project — "
                       "use ONLY .jsx/.js files, NEVER .tsx/.ts, and do NOT include a tsconfig.")
        if stack in _NEXT_STACKS:
            prompt += ("\n\nIMPORTANT: this is a Next.js App Router project — put ALL routes "
                       "under app/ (app/page.jsx, app/layout.jsx, app/<route>/page.jsx, "
                       "app/api/<route>/route.js). NEVER create a pages/ directory or any "
                       "Pages Router files (no pages/index, pages/_app, pages/_document, "
                       "pages/api) — mixing the two routers breaks `next build`. Use ONLY "
                       ".jsx/.js, NEVER .tsx/.ts, and do NOT include a tsconfig.")
        if stack in _GAME_STACKS:
            prompt += ("\n\nIMPORTANT: this is a Phaser 3 + Vite browser game in VANILLA "
                       "JavaScript, structured as a PURE SIMULATION CORE plus a render-only "
                       "scene. Put ALL game logic in src/sim.js as pure functions with NO "
                       "Phaser import: createState(seed), step(state, input, dt), "
                       "isWin(state), isLose(state). Keep ONE authoritative state advanced "
                       "only by step(); use a SEEDED rng carried in state (NEVER Math.random "
                       "or Date.now); honor state.paused (freeze the sim) and state.over "
                       "(ignore input). The Phaser scene in src/main.js owns NO logic — each "
                       "frame it reads input, calls step(), and renders the returned state. "
                       "Use ONLY .js — NEVER .ts/.tsx/.jsx, no React, no tsconfig. This split "
                       "is required so the headless invariant gate can verify the game.")
        if stack in _SWIFT_STACKS:
            prompt += ("\n\nIMPORTANT: this is a NATIVE macOS app in Swift + SwiftUI built by "
                       "Swift Package Manager — NOT a web app. Plan a root Package.swift "
                       "(executables 'App' and 'AppCLI' + library 'AppCore' + test target), "
                       "SwiftUI sources under Sources/App/ (a @main App in MainApp.swift + "
                       "views), the PURE logic (models/state, NO SwiftUI import) under "
                       "Sources/AppCore/, an offline deterministic prompt loop over AppCore "
                       "under Sources/AppCLI/main.swift, `.skyn3t-cli-playtest.json`, and "
                       "XCTests under Tests/AppCoreTests/. Use ONLY "
                       ".swift files — NEVER package.json, index.html, JS/TS, or any web files.")
        ref = p.get("reference_image")
        if ref:
            prompt += "\n\nNote: the user provided a reference image that informs the visual design."
        result = await self.llm.complete(
            prompt,
            tier=Tier.STRONG,
            system=self.system_prompt(_SYSTEM),
            max_tokens=(
                _FULL_APP_ARCHITECT_TOKENS
                if full_app
                else _STANDARD_ARCHITECT_TOKENS
            ),
            json_mode=True,
            task_type=self.agent_type,
        )
        parsed = parse_json(result.text)

        # Some otherwise-correct models wrap the requested object in {"plan":
        # {...}}. Accept that harmless shape instead of discarding a paid plan.
        if (
            isinstance(parsed, dict)
            and not parsed.get("files")
            and isinstance(parsed.get("plan"), dict)
        ):
            parsed = parsed["plan"]

        if (not isinstance(parsed, dict) or parsed.get("stub") is True
                or result.backend == "stub" or not parsed.get("files")):
            parsed = self._offline_plan(
                brief, stack, p.get("slug"), full_app=full_app,
            )
        elif full_app:
            recovery = self._offline_plan(
                brief, stack, p.get("slug"), full_app=True,
            )
            parsed["files"] = _augment_full_app_files(
                list(parsed.get("files") or []),
                list(recovery.get("files") or []),
                stack,
            )
            parsed["build_order"] = [
                item["path"] for item in parsed["files"]
            ]

        # Always include the stack so downstream stages agree.
        parsed.setdefault("stack", stack)
        parsed["stack"] = parsed.get("stack") or stack
        files = parsed.get("files") or []
        # Keep the plan .jsx-consistent with the scaffold floor (D8): a .tsx plan
        # leaves the .jsx scaffold entry rendering the counter stub. Next.js shares
        # the .jsx floor, so normalize it the same way.
        if parsed["stack"] in _JSX_STACKS or parsed["stack"] in _NEXT_STACKS:
            files = _jsx_only(files)
        elif parsed["stack"] in _GAME_STACKS:
            files = _plain_js(files)
        files = _drop_binary_asset_plans(files)
        plan = {
            "stack": parsed["stack"],
            "summary": parsed.get("summary", f"Plan for {stack}: {brief}"),
            "files": files,
            "build_order": [f.get("path") for f in files if isinstance(f, dict)],
            "components": parsed.get("components", []),
        }
        return TaskResult(task_id=task.task_id, success=True,
                          output={"plan": plan, "stack": plan["stack"],
                                  "model": result.model, "backend": result.backend})

    def _offline_plan(
        self,
        brief: str,
        stack: str,
        slug: Any,
        *,
        full_app: bool = False,
    ) -> dict[str, Any]:
        app_name = slugify(slug or brief, "app")
        scaffold = scaffold_for(stack, app_name, brief)
        files = [{"path": path, "purpose": f"{stack} project file"} for path in scaffold]
        if full_app:
            files = _merge_file_plans(
                files,
                _full_app_recovery_files(brief, stack),
            )
        return {
            "stack": stack,
            "summary": (
                f"Recovery full-app plan for '{brief}' using {stack}."
                if full_app
                else f"Offline plan: scaffold a runnable {stack} project for '{brief}'."
            ),
            "files": files,
            "build_order": [item["path"] for item in files],
            "components": [item["path"] for item in files],
        }

    async def health_check(self) -> bool:
        return self.llm is not None
