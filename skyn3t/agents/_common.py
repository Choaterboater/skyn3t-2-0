"""Shared helpers for the generative agents.

Pure, side-effect-free utilities: stack detection, JSON-from-LLM parsing,
and small text helpers. Importing this module performs no I/O and no network
calls (design rule #4).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Canonical stacks the CodeAgent can scaffold offline.
KNOWN_STACKS = (
    "react_vite",
    "react_native",
    "nextjs",
    "astro",
    "remix",
    "vue",
    "sveltekit",
    "react_ts",
    "static_html",
    "python_cli",
    "fastapi",
    "node_express",
    "tauri",
    "phaser",
    "swift",
    "mcp",
    "rag",
    "workflow",
    "agent_pack",
)

DEFAULT_STACK = "react_vite"


def confined_path(root: Path | str, rel: str) -> Path | None:
    """Resolve ``root/rel`` and return it only if it stays inside ``root``
    (symlink-safe), else ``None``. The shared guard for agent file-write paths —
    even hardcoded rel paths can escape a GENERATED tree through a symlinked
    subdirectory. Same idiom as proof_run._confine / code_improver._confined."""
    try:
        base = Path(root).resolve()
        target = (base / rel).resolve()
        if os.path.commonpath([str(base), str(target)]) != str(base):
            return None
    except (ValueError, OSError):
        # resolve() itself can raise (e.g. an embedded NUL byte) — fail closed,
        # exactly like proof_run._confine.
        return None
    return target


def detect_stack(brief: str = "", plan: Any = None, explicit: str = "") -> str:
    """Best-effort stack detection from an explicit hint, the plan, then the brief.

    Always returns one of :data:`KNOWN_STACKS`; defaults to ``react_vite``.
    """
    if explicit:
        norm = _normalize_stack(explicit)
        if norm:
            return norm

    # The plan may carry a structured stack field.
    if isinstance(plan, dict):
        for key in ("stack", "tech_stack", "framework"):
            val = plan.get(key)
            if isinstance(val, str):
                norm = _normalize_stack(val)
                if norm:
                    return norm

    text = f"{brief} {_plan_text(plan)}".lower()
    # Order matters: more specific signals first.
    # MCP server (Model Context Protocol) must precede fastapi/express/react: an
    # "mcp server" brief contains "server"/"tool" (which brush those stacks) but is
    # a Python stdio tool server, not a web/HTTP app. Bare "mcp" is matched
    # WHOLE-WORD so it never fires as a substring; "model context protocol" is the
    # unambiguous spelled-out form.
    if "model context protocol" in text or re.search(r"\bmcp\b", text):
        return "mcp"
    # RAG "chat with your documents" app must precede fastapi/react: a RAG brief
    # brushes "api"/"app"/"service" but is a retrieval app, not the generic web
    # scaffold. Bare "rag" is WHOLE-WORD ("storage"/"dragging" never match); the
    # other keywords are RAG-distinctive multi-word phrases — bare "search" and
    # "document" are deliberately NOT claimed (the phaser bare-"game" lesson).
    if re.search(r"\brag\b", text) or any(k in text for k in (
        "retrieval augmented", "retrieval-augmented",
        "chat with my doc", "chat with your doc", "chat with my pdf",
        "chat with my notes", "chat with my files",
        "knowledge base", "document q&a", "doc q&a", "semantic search",
        # Memory-augmented chat (§3.10) rides rag; phrases only (never bare
        # "memory" — "memory game"/"memory profiler" are other stacks).
        "remembers me", "remembers our", "assistant with memory",
        "chat with memory", "chatbot with memory", "stateful chat",
    )):
        return "rag"
    # Agent team pack (persona ROSTER product) must precede workflow: pack
    # briefs name the static deliverable; workflow briefs describe runtime
    # behavior. Bare "persona"/"agents" deliberately not claimed.
    if any(k in text for k in (
        "agent personas", "personas for", "persona pack", "agent pack",
        "agents pack", "team pack", "agent team pack", "subagents",
        "subagent pack", "agents for my", "agent roster",
    )):
        return "agent_pack"
    # Agent-workflow app must precede fastapi/react: a workflow brief brushes
    # "api"/"app"/"service" but is a multi-step runner, not the generic web
    # scaffold. Bare "workflow" is WHOLE-WORD SINGULAR ("workflows" is a
    # dashboard noun); bare "automation"/"pipeline"/"scheduled" are not claimed.
    if re.search(r"\bworkflow\b", text) or any(k in text for k in (
        "agent workflow", "team of agents", "multi-agent", "multi agent",
        "agent that", "automation pipeline", "monitor and notify",
        "scheduled briefing", "daily briefing",
    )):
        return "workflow"
    if any(k in text for k in ("fastapi", "uvicorn", "rest api", "http api", "endpoint")):
        return "fastapi"
    if any(k in text for k in ("express", "node server", "node.js server")):
        return "node_express"
    # Phaser game must precede the cli/"script" check ("script" is a substring of
    # "javascript") AND the generic react/static checks below — a "browser game"
    # would otherwise route to python_cli or the React/Vite scaffold. Bare "game"
    # is intentionally NOT a keyword (it steals "board game tracker", "video game
    # blog", "game of thrones wiki"); the single-word genres are matched WHOLE-WORD
    # so "troubleshooter"≠shooter and "endgame"≠game. The LLM selector + an explicit
    # "game"/"phaser" pin (REAL_BUILDER_STACKS / _normalize_stack) cover the rest.
    if any(k in text for k in (
        "phaser", "2d game", "html5 game", "browser game", "game engine",
        "side-scroller", "endless runner", "tower defense", "tilemap",
    )) or any(re.search(rf"\b{k}\b", text) for k in ("arcade", "platformer", "shooter")):
        return "phaser"
    if any(k in text for k in (
        "react typescript", "typescript react", "vite react typescript",
        "vite typescript", "typescript spa", "typescript web app", "tsx app",
    )):
        return "react_ts"
    if any(k in text for k in ("cli", "command line", "command-line", "terminal tool", "script")):
        return "python_cli"
    # Mobile must precede react_vite: "mobile app" / "react native" / "ios app"
    # are mobile, not the web React scaffold.
    if any(k in text for k in (
        "mobile app", "react native", "react-native", "expo",
        "ios app", "android app", "mobile application",
    )):
        return "react_native"
    # Swift / SwiftUI native macOS must precede Tauri (which owns the ambiguous
    # bare "macos app"/"mac app" phrases): a "swiftui macos app" is native Swift,
    # not a cross-platform Tauri shell. Keywords are Swift-DISTINCTIVE so an
    # ordinary "a swift/fast X" brief never routes here; "spm" is matched
    # whole-word so it doesn't fire as a substring.
    # swift-LEADING phrases are safe as plain substrings; phrases ENDING in "swift"
    # (plus "spm") are word-bounded so "swiftly"/"swiftness"/"raspmelody" don't match.
    if any(k in text for k in (
        "swiftui", "swift app", "swift macos", "swift package", "swiftpm",
        "swift native",
    )) or re.search(r"\b(?:spm|macos swift|written in swift|built with swift)\b", text):
        return "swift"
    # Desktop (Tauri): a native Mac/Windows app. Must precede the generic react/vite
    # check — a desktop app's brief usually says "app" and uses a React frontend.
    if any(k in text for k in (
        "desktop app", "tauri", "native app", "mac app", "macos app",
        "windows app", "electron", "standalone app", "desktop application",
        "cross-platform app", "menu bar app",
    )):
        return "tauri"
    # Next.js / Astro / Remix must precede the generic ``react`` check — they are
    # React-family frameworks whose briefs often also say "react", but they have
    # their own real builder now (not the plain Vite scaffold).
    if any(k in text for k in ("next.js", "nextjs", "next js")):
        return "nextjs"
    if any(k in text for k in ("sveltekit", "svelte kit")) or re.search(r"\bsvelte\b", text):
        return "sveltekit"
    if re.search(r"\bvue(?:\.js|js)?\b", text):
        return "vue"
    if any(k in text for k in (
        "react typescript", "typescript react", "vite typescript",
        "typescript spa", "typescript web app", "tsx app",
    )):
        return "react_ts"
    # Whole-word only: "astrology"/"astronomy"/"gastro" must not route to astro,
    # nor "remixing" to remix.
    if re.search(r"\bastro\b", text):
        return "astro"
    if re.search(r"\bremix\b", text):
        return "remix"
    if any(k in text for k in ("react", "vite", "spa", "frontend", "front-end", "single page")):
        return "react_vite"
    if any(k in text for k in ("static site", "landing page", "plain html", "static html")):
        return "static_html"
    return DEFAULT_STACK


def _normalize_stack(value: str) -> str:
    v = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "react": "react_vite",
        "vite": "react_vite",
        "react_vite": "react_vite",
        "vite_react": "react_vite",
        "spa": "react_vite",
        "react_ts": "react_ts",
        "react_typescript": "react_ts",
        "typescript_react": "react_ts",
        "vite_ts": "react_ts",
        "vite_typescript": "react_ts",
        "tsx": "react_ts",
        "typescript": "react_ts",
        # Tauri cross-platform desktop (Vite/React frontend + Rust shell).
        "tauri": "tauri",
        "desktop": "tauri",
        "desktop_app": "tauri",
        "electron": "tauri",
        "macos": "tauri",
        # Phaser 3 + Vite 2D browser game (vanilla JS, not React).
        "phaser": "phaser",
        "phaser3": "phaser",
        "phaserjs": "phaser",
        "game": "phaser",
        # Swift / SwiftUI native macOS (Swift Package Manager). Distinct from the
        # "macos"->tauri alias below, which is intentionally left untouched: only
        # explicit swift/swiftui/spm signals map here.
        "swift": "swift",
        "swiftui": "swift",
        "swiftpm": "swift",
        "spm": "swift",
        "swift_package": "swift",
        "macos_native": "swift",
        "swift_macos": "swift",
        "swift_native": "swift",
        # MCP server (Model Context Protocol) — Python stdio tool server. A real
        # builder stack; only explicit mcp/model-context-protocol signals map here.
        "mcp": "mcp",
        "mcp_server": "mcp",
        "mcpserver": "mcp",
        "model_context_protocol": "mcp",
        "mcp_tool_server": "mcp",
        # RAG "chat with your documents" app (FastAPI + pure retrieval core). A
        # real builder stack; only explicit RAG/retrieval signals map here.
        "rag": "rag",
        "rag_app": "rag",
        "ragapp": "rag",
        "retrieval_augmented_generation": "rag",
        "knowledge_base": "rag",
        "chat_with_docs": "rag",
        "chat_with_documents": "rag",
        "document_qa": "rag",
        "doc_qa": "rag",
        # Agent-workflow app (multi-step runner, FastAPI + pure engine). A real
        # builder stack; only explicit workflow/agent signals map here.
        "workflow": "workflow",
        "workflow_app": "workflow",
        "agent_workflow": "workflow",
        "agent_team": "workflow",
        "multi_agent": "workflow",
        "automation": "workflow",
        # Agent team pack (persona roster product, zero runtime deps).
        "agent_pack": "agent_pack",
        "agents_pack": "agent_pack",
        "agent_team_pack": "agent_pack",
        "persona_pack": "agent_pack",
        "personas": "agent_pack",
        "subagents": "agent_pack",
        "agent_roster": "agent_pack",
        "mobile": "react_native",
        "expo": "react_native",
        "react_native": "react_native",
        "reactnative": "react_native",
        "ios": "react_native",
        "android": "react_native",
        # Next.js / Astro / Remix — real builder stacks (no longer aliased to react).
        # ``next.js`` keeps its dot (only spaces/hyphens are normalized to _).
        "next": "nextjs",
        "next.js": "nextjs",
        "nextjs": "nextjs",
        "next_js": "nextjs",
        "astro": "astro",
        "remix": "remix",
        "vue": "vue",
        "vuejs": "vue",
        "vue.js": "vue",
        "vue_3": "vue",
        "svelte": "sveltekit",
        "sveltekit": "sveltekit",
        "svelte_kit": "sveltekit",
        "html": "static_html",
        "static": "static_html",
        "static_html": "static_html",
        "python": "python_cli",
        "python_cli": "python_cli",
        "cli": "python_cli",
        "fastapi": "fastapi",
        "python_api": "fastapi",
        "node": "node_express",
        "express": "node_express",
        "node_express": "node_express",
    }
    return aliases.get(v, v if v in KNOWN_STACKS else "")


def _plan_text(plan: Any) -> str:
    if plan is None:
        return ""
    if isinstance(plan, str):
        return plan
    try:
        return json.dumps(plan)
    except (TypeError, ValueError):
        return str(plan)


def parse_json(text: str) -> Any:
    """Parse JSON from an LLM response, tolerating code fences and prose.

    Returns the parsed object, or ``None`` if nothing parseable is found.
    """
    if not text:
        return None
    # Strip markdown code fences.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass
    # Fall back to the first balanced {...} or [...] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def slugify(text: str, fallback: str = "app") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or fallback


# Max chars of injected skill advice in a generative prompt. Skill bodies can be
# tens of KB each; the full set otherwise dwarfs the build instruction AND slows
# the codegen agent (a larger prompt degrades wall-clock — the idle stall-guard
# resets on every stream event, so only the hard ceiling bounds it, not this). Keep
# it conservative: design guidance is carried by the explicit DESIGN directive in
# the codegen prompt (code_agent._DESIGN_DIRECTIVE), not by un-truncated skill bodies.
_MAX_SKILL_ADVICE = 6000


def knowledge_block(payload: Any) -> str:
    """Assemble injected prior knowledge into a prompt preamble.

    Pulls skill advice, graded lessons, and RAG recall (which is fed by past
    build outcomes AND ingested GitHub repos) out of the stage payload so the
    generative agents actually USE what the system has learned. Returns "" when
    there is nothing to inject. Skills are labeled as a quality contract so they
    do not disappear as vague background text in long build prompts.
    """
    if not isinstance(payload, dict):
        return ""
    raw_extra = payload.get("extra")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    parts: list[str] = []

    if extra.get("full_app_contract"):
        parts.append(_full_app_contract(payload, extra))

    advice = extra.get("skills_advice") or payload.get("skills_advice")
    if advice:
        # Cap injected skill advice: full skill-doc bodies (up to tens of KB) bloat
        # the codegen prompt, slowing claude -p enough to blow the agentic timeout
        # and ship a stub. Keep a bounded contract excerpt — like recall/lessons below.
        parts.append(
            "SKILL QUALITY CONTRACT (apply these stack-specific rules):\n"
            + str(advice).strip()[:_MAX_SKILL_ADVICE]
        )

    role_guidance = extra.get("role_guidance") or payload.get("role_guidance")
    if role_guidance:
        parts.append(
            "STAGE ROLE GUIDANCE (external/catalog roles for this stage only):\n"
            + str(role_guidance).strip()[:3000]
        )

    lessons = payload.get("lessons") or []
    if lessons:
        lines = [
            f"- {(lesson.get('text') if isinstance(lesson, dict) else lesson)}"[:280]
            for lesson in lessons[:5]
            if (lesson.get("text") if isinstance(lesson, dict) else lesson)
        ]
        if lines:
            parts.append("Lessons from past builds (apply where they fit):\n" + "\n".join(lines))

    recall = extra.get("recall") or payload.get("recall") or []
    if isinstance(recall, list):
        lines = [
            f"- {(r.get('text') if isinstance(r, dict) else r)}"[:280]
            for r in recall[:5]
            if (r.get("text") if isinstance(r, dict) else r)
        ]
        recall = "\n".join(lines)
    if recall:
        parts.append(
            "Relevant patterns from the knowledge base (past builds + ingested "
            "GitHub repos):\n" + str(recall)[:1600]
        )

    # Generated image assets (Replicate): real current-build images already
    # written into the project (public/assets for web stacks, assets/ otherwise).
    # Tell the app to USE them instead of drawing crappy art itself. Surfaced
    # from the asset-gen step's manifest after brief relevance filtering.
    assets = extra.get("assets") or payload.get("assets") or []
    if isinstance(assets, list) and assets:
        lines = [
            f"- {a.get('file')} ({a.get('subject')})"
            for a in assets[:8]
            if isinstance(a, dict) and a.get("file")
        ]
        if lines:
            parts.append(
                "REAL generated image assets for THIS brief are already in the "
                "project and served at these paths. Reference only these matching "
                "files directly (e.g. <img src>) instead of inventing unrelated "
                "assets or drawing placeholder art:\n"
                + "\n".join(lines)
            )

    if not parts:
        return ""
    return "## Prior knowledge (advisory — reuse what fits)\n" + "\n\n".join(parts) + "\n\n"


def _full_app_contract(payload: dict[str, Any], extra: dict[str, Any]) -> str:
    """Contract used by the Full App build profile.

    The intent is to prevent "semi-app" deliveries: codegen must build core
    workflows, real content, state, and polished screens in one pass instead of a
    thin shell that needs repeated manual follow-ups.
    """
    stack = str(payload.get("stack") or extra.get("stack") or "").lower()
    brief = str(payload.get("brief") or "").lower()
    webish = stack in {
        "react", "nextjs", "static", "astro", "remix", "express", "fastapi",
        "rag", "workflow", "phaser",
    } or any(k in brief for k in ("website", "web app", "site", "page", "dashboard"))
    contentish = any(
        k in brief
        for k in (
            "website", "site", "landing", "tutorial", "tutorials", "course",
            "lesson", "lessons", "beginner", "learn", "guide", "golf",
        )
    )
    lines = [
        "FULL APP CONTRACT (this profile must ship a complete product, not a thin scaffold):",
        "- Build the primary user workflows end-to-end with meaningful state, validation, empty/loading/error states, and useful sample data.",
        "- Include enough screens/sections for the brief to feel complete on first run; do not leave follow-up features as placeholders.",
        "- Make the first screen the actual usable app experience, not a generic hero-only shell.",
        "- Use any generated /assets images listed above; never reference unrelated or invented assets.",
    ]
    if webish:
        lines.extend([
            "- For UI/web builds, include responsive navigation, polished dense content sections, real CTAs/forms or interactive controls, and a finished footer/settings/help area when relevant.",
            "- Verify text hierarchy, spacing, and mobile layout in the implementation; avoid placeholder copy and repeated generic cards.",
        ])
    if contentish:
        lines.extend([
            "- For content/tutorial sites, create a full editorial page: audience-specific learning path, at least 5 substantial sections, practical drills/checklists, FAQ, and next-step resources.",
            "- When the brief asks for tutorials or learning, include a tutorials/resources section with at least 3 curated external-resource cards or video/embed placeholders labeled with specific search-ready titles.",
            "- For beginner education, organize content by progression from first-time basics to practice routines and common mistakes.",
        ])
    return "\n".join(lines)


def extract_code(text: str) -> str:
    """Pull a single code block out of an LLM response, or return it verbatim."""
    if not text:
        return ""
    # The newline after the language marker is OPTIONAL: a fence like
    # ```json{...}``` (no newline) must still extract its body rather than fall
    # through and return the raw fence markup. [^\S\n]* eats trailing spaces.
    fenced = re.search(r"```[a-zA-Z0-9_+-]*[^\S\n]*\n?(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).rstrip() + "\n"
    return text
