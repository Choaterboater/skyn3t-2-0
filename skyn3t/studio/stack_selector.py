# skyn3t/studio/stack_selector.py
"""Best-fit stack selection: explicit pin -> LLM best-fit -> keyword fallback.
Works in PLANNER vocab, restricted to stacks that have a real builder. Never
raises; degrades to keyword/default."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from skyn3t.studio.planner import detect_stack as _planner_detect

# Planner-vocab stacks that map to a real builder, with one-line "best for" hints.
REAL_BUILDER_STACKS: dict[str, str] = {
    "react": "a browser web app / SPA / dashboard UI (Vite + React)",
    "react_native": "a mobile app for iOS/Android (Expo)",
    "nextjs": "a full-stack React web app with routing/SSR (Next.js App Router)",
    "astro": "a fast content-focused site / blog / docs (Astro)",
    "remix": "a full-stack web app with nested routes + data loading (Remix)",
    "fastapi": "a Python web app or HTTP/REST API with a server + storage",
    "static": "a static website / landing page (HTML/CSS/JS, no backend)",
    "python": "a Python CLI tool, script, or library (no web UI)",
    "express": "a Node.js web server / API",
}

# Planner stacks that have NO builder of their own -> collapse to a real one.
# "cli" MUST map to python (not the react default below) — a command-line brief
# is a python_cli, never a React app. (nextjs/astro/remix are real builders now.)
_COLLAPSE = {"flask": "fastapi", "django": "fastapi", "cli": "python"}


@dataclass(slots=True)
class StackChoice:
    stack: str
    method: str  # pin|llm|keyword|default
    confidence: float
    rationale: str


def _to_real_builder(stack: str) -> str:
    s = (stack or "").strip().lower()
    s = _COLLAPSE.get(s, s)
    # Unknown/unmapped -> python (the planner's own default). Defaulting to react
    # mis-stacked CLI/ambiguous briefs as a web app.
    return s if s in REAL_BUILDER_STACKS else "python"


def _validate_pin(pin: str) -> str:
    s = (pin or "").strip().lower()
    s = _COLLAPSE.get(s, s)
    return s if s in REAL_BUILDER_STACKS else ""


def keyword_choice(brief: str) -> StackChoice:
    raw = _planner_detect(brief)
    stack = _to_real_builder(raw)
    return StackChoice(stack, "keyword", 0.5, f"keyword heuristic → {stack}")


async def _llm_choice(brief: str, llm: Any) -> StackChoice | None:
    menu = "\n".join(f"- {k}: {v}" for k, v in REAL_BUILDER_STACKS.items())
    prompt = (
        "Pick the single best stack for this build from the menu.\n\n"
        f"Brief: {brief}\n\nMenu:\n{menu}\n\n"
        'Respond ONLY as JSON: {"stack": "<one menu key>", '
        '"confidence": <0..1>, "rationale": "<one sentence>"}'
    )
    try:
        res = await llm.complete(prompt, json_mode=True, max_tokens=300)
        if getattr(res, "backend", "") == "stub":
            return None
        data = json.loads(_extract_json(res.text))
        stack = _to_real_builder(str(data.get("stack", "")))
        if stack not in REAL_BUILDER_STACKS:
            return None
        conf = float(data.get("confidence", 0.7))
        return StackChoice(stack, "llm", conf, str(data.get("rationale", ""))[:300])
    except Exception:  # noqa: BLE001 - any failure -> caller falls back
        return None


def _extract_json(text: str) -> str:
    """Pull a JSON object out of an LLM reply (fenced or inline). The brace
    scan is the workhorse; an optional ``` fence and `json` tag are stripped first."""
    t = (text or "").strip()
    if "```" in t:
        t = t.split("```")[1].removeprefix("json").strip()
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start >= 0 and end > start else t


async def select_stack(
    brief: str, *, pin: str = "", llm: Any | None = None, attended: bool = False
) -> StackChoice:
    # `attended` is reserved for the deferred clarify-on-low-confidence gate; currently unused.
    norm = _validate_pin(pin)
    if norm:
        return StackChoice(norm, "pin", 1.0, "explicit pin")
    if llm is not None:
        choice = await _llm_choice(brief, llm)
        if choice is not None:
            return choice
    return keyword_choice(brief)
