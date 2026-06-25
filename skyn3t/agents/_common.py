"""Shared helpers for the generative agents.

Pure, side-effect-free utilities: stack detection, JSON-from-LLM parsing,
and small text helpers. Importing this module performs no I/O and no network
calls (design rule #4).
"""

from __future__ import annotations

import json
import re
from typing import Any


# Canonical stacks the CodeAgent can scaffold offline.
KNOWN_STACKS = (
    "react_vite",
    "react_native",
    "nextjs",
    "astro",
    "remix",
    "static_html",
    "python_cli",
    "fastapi",
    "node_express",
)

DEFAULT_STACK = "react_vite"


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
    if any(k in text for k in ("fastapi", "uvicorn", "rest api", "http api", "endpoint")):
        return "fastapi"
    if any(k in text for k in ("express", "node server", "node.js server")):
        return "node_express"
    if any(k in text for k in ("cli", "command line", "command-line", "terminal tool", "script")):
        return "python_cli"
    # Mobile must precede react_vite: "mobile app" / "react native" / "ios app"
    # are mobile, not the web React scaffold.
    if any(k in text for k in (
        "mobile app", "react native", "react-native", "expo",
        "ios app", "android app", "mobile application",
    )):
        return "react_native"
    # Next.js / Astro / Remix must precede the generic ``react`` check — they are
    # React-family frameworks whose briefs often also say "react", but they have
    # their own real builder now (not the plain Vite scaffold).
    if any(k in text for k in ("next.js", "nextjs", "next js")):
        return "nextjs"
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
    """Assemble injected prior knowledge into an advisory prompt preamble.

    Pulls skill advice, graded lessons, and RAG recall (which is fed by past
    build outcomes AND ingested GitHub repos) out of the stage payload so the
    generative agents actually USE what the system has learned. Returns "" when
    there is nothing to inject. Advisory only — the model applies what fits.
    """
    if not isinstance(payload, dict):
        return ""
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    parts: list[str] = []

    advice = extra.get("skills_advice") or payload.get("skills_advice")
    if advice:
        # Cap injected skill advice: full skill-doc bodies (up to tens of KB) bloat
        # the codegen prompt, slowing claude -p enough to blow the agentic timeout
        # and ship a stub. Keep an advisory excerpt — like recall/lessons below.
        parts.append(str(advice).strip()[:_MAX_SKILL_ADVICE])

    lessons = payload.get("lessons") or []
    if lessons:
        lines = [
            f"- {(l.get('text') if isinstance(l, dict) else l)}"[:280]
            for l in lessons[:5]
            if (l.get("text") if isinstance(l, dict) else l)
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

    # Generated image assets (Replicate): real line-art/coloring images already
    # written into the project under assets/. Tell the app to USE them instead of
    # drawing crappy art itself. Surfaced from the asset-gen step's manifest.
    assets = extra.get("assets") or payload.get("assets") or []
    if isinstance(assets, list) and assets:
        lines = [
            f"- {a.get('file')} ({a.get('subject')})"
            for a in assets[:8]
            if isinstance(a, dict) and a.get("file")
        ]
        if lines:
            parts.append(
                "REAL generated image assets are already in this project under "
                "`assets/` — reference these files directly (e.g. <img src> / "
                "import) instead of generating placeholder or low-quality art:\n"
                + "\n".join(lines)
            )

    if not parts:
        return ""
    return "## Prior knowledge (advisory — reuse what fits)\n" + "\n\n".join(parts) + "\n\n"


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
