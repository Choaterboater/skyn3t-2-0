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
    "tauri": "a cross-platform desktop app for Mac/Windows (Vite + React frontend + Tauri Rust shell)",
    "phaser": "a 2D browser game — arcade / platformer / shooter (Phaser 3 + Vite, canvas)",
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


@dataclass(slots=True)
class BuildClassification:
    app_type: str
    engine: str
    method: str
    rationale: str

    def to_dict(self) -> dict[str, str]:
        return {
            "app_type": self.app_type,
            "engine": self.engine,
            "method": self.method,
            "rationale": self.rationale,
        }


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


def classify_build(
    brief: str,
    stack: str,
    *,
    app_type_override: str = "",
    engine_override: str = "",
) -> BuildClassification:
    """Infer app type + engine metadata for docs/UI without pinning the builder.

    The stack remains the build substrate. This classification is descriptive
    metadata for the dashboard, manifests, and future routing. Env/UI overrides
    win, then the stack/brief heuristics pick a safe default.
    """
    app_type = _normalize_override(app_type_override)
    engine = _normalize_override(engine_override)
    method = "override" if app_type or engine else "inferred"
    low = (brief or "").lower()
    s = _to_real_builder(stack)

    if not app_type:
        app_type = _infer_app_type(low, s)
    if not engine:
        engine = _infer_engine(low, s)
    return BuildClassification(
        app_type=app_type,
        engine=engine,
        method=method,
        rationale=f"{method} from stack={s}",
    )


def _normalize_override(value: str) -> str:
    v = (value or "").strip().lower().replace(" ", "_")
    return "" if v in ("", "auto", "none") else v


def _infer_app_type(low: str, stack: str) -> str:
    if stack == "phaser" or any(k in low for k in ("game", "arcade", "platformer", "shooter", "rpg")):
        return "game"
    if stack == "python" or any(k in low for k in ("cli", "command line", "script", "terminal")):
        return "developer_tool"
    if stack in ("fastapi", "express") or any(k in low for k in ("api", "rest", "backend", "server")):
        return "api_service"
    if stack == "react_native":
        return "mobile_app"
    if stack == "tauri":
        return "desktop_app"
    if any(k in low for k in ("dashboard", "admin", "analytics", "metrics")):
        return "dashboard"
    if any(k in low for k in ("landing", "marketing", "homepage", "portfolio")):
        return "landing_page"
    if any(k in low for k in ("crud", "database", "records", "inventory", "form")):
        return "crud_app"
    if any(k in low for k in ("chart", "csv", "visualization", "graph")):
        return "data_viz"
    if any(k in low for k in ("saas", "billing", "settings", "onboarding")):
        return "saas_product"
    return "product_app"


def _infer_engine(low: str, stack: str) -> str:
    if stack == "phaser":
        return "phaser"
    if stack in ("react", "nextjs", "remix", "astro", "static"):
        if any(k in low for k in ("canvas", "pixi", "three", "webgl", "visual repair")):
            return "browser_native"
        return "dom"
    if stack == "react_native":
        return "expo"
    if stack == "tauri":
        return "tauri"
    if stack in ("fastapi", "express"):
        return "server"
    if stack == "python":
        return "python"
    return "none"


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
    """Pull a JSON object out of an LLM reply (fenced or inline).

    Scans for the FIRST balanced ``{...}`` object from the first ``{``, so bare
    JSON followed by a trailing fence/prose/code (a common LLM shape) is parsed
    correctly — the old fence-first split discarded the leading JSON. A leading
    fence wrapping the whole reply is still unwrapped first."""
    t = (text or "").strip()
    if t.startswith("```") and t.count("```") >= 2:
        t = t.split("```", 2)[1].removeprefix("json").strip()
    start = t.find("{")
    if start < 0:
        return t
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return t[start:]  # unbalanced — let json.loads surface the error


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
