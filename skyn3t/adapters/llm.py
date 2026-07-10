"""Unified LLM client with cost-aware routing and an offline stub backend.

One entry point — :meth:`LLMClient.complete` — resolves a tier to a model via
the :class:`ModelRouter`, then dispatches to a backend:

* ``openrouter`` — real HTTP (primary) when ``OPENROUTER_API_KEY`` is set.
* ``<provider>_cli`` — shells out to a locally-installed CLI (``claude``,
  ``kimi``, ``copilot``) in headless print mode. Real generation with **no API
  key** — handy when you already have a coding-agent CLI signed in.
* ``stub`` — deterministic offline responses so the full pipeline (and the
  test suite) runs with **no keys and no network**. This is what makes
  "brief -> runnable app" demonstrable out of the box.

Backend selection (``settings.llm_backend``): ``auto`` prefers OpenRouter (if a
key is set), then a detected CLI, then the stub. It can be pinned to any
specific backend. Every call is metered and checked against budget caps —
design rules #5 (cheap by default) and #6 (degrade, don't crash).
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import importlib
import json
import math
import os
import random
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import structlog

from skyn3t.atomic_io import atomic_write_text
from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.model_router import ModelRouter, Tier
from skyn3t.core.model_router import is_free_model_id as router_is_free_model_id

# Per-asyncio-task LLM route capture. The LLMClient is SHARED across agents, but
# each agent's run() is its own task; task-local vars isolate "the completions
# THIS run produced" without a global lock (a per-AGENT lock can't — the client
# is shared, so two agents would race on shared instance attrs). The only reader
# (core/agent.py._run_locked) runs in the SAME task as the completions it reads.
_LAST_MODEL: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_last_model", default=None)
_LAST_ROUTE: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_last_route", default=None)
_ROUTES: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_routes", default=None)
_BUDGET_LEDGER_LOCK = threading.RLock()
_LEDGER_LOCK_TIMEOUT_S = 15.0

log = structlog.get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Default vision-capable model when ``settings.vision_model`` is unset but an
# image is attached. Cheap + widely available on OpenRouter — same default the
# visual loop uses (skyn3t/studio/visual_check.py).
_DEFAULT_VISION_MODEL = "openai/gpt-4o-mini"

# Substrings that mark a model id as already vision-capable, so an attached image
# can flow through the normally-resolved model instead of being forced onto the
# generic vision fallback.
_VISION_MODEL_MARKERS = ("gpt-4o", "gpt-4.1", "vision", "claude-3", "gemini",
                         "llava", "qwen2-vl", "qwen2.5-vl", "pixtral", "vl-")

# Headless print-mode invocation per CLI provider. The prompt is appended as
# the final argv. Confirmed: ``claude -p "<prompt>"`` prints the reply.
# ``--setting-sources project`` isolates codegen from the HOST user's Claude Code
# config (output-style plugins, hooks, custom themes) — otherwise e.g. an
# "explanatory" output-style plugin injects prose/insight blocks into the
# generated code. Only project-level settings load; auth still works.
_CLI_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "-p", "--setting-sources", "project"],
    "kimi": ["kimi", "-p"],
    "copilot": ["copilot", "-p"],
}
_KNOWN_CLI_PROVIDERS = ("claude", "kimi", "copilot")


def openrouter_key(settings: Settings) -> str:
    """Resolve the OpenRouter key from live settings or supported env aliases."""
    return (
        str(getattr(settings, "openrouter_api_key", "") or "").strip()
        or os.environ.get("SKYN3T_OPENROUTER_API_KEY", "").strip()
        or os.environ.get("OPENROUTER_API_KEY", "").strip()
    )

# Whole-project agentic codegen over the OpenRouter API: the model authors the
# entire app itself via tool-calls (the way bolt/v0/Aider build coherent apps with
# cheap models), instead of skyn3t's weak per-file generation.
# The agentic codegen system prompt is COMPOSED per stack: a universal engineering core +
# a stack-appropriate body + a universal tail. SkyN3t builds every kind of app (web sites,
# web apps, games, mobile/iOS/Android, desktop/Windows/macOS, APIs, CLIs, …), so codegen
# must NOT hardcode "build a React/Next marketing site" for every brief — that hardcoding
# is what dragged a game build into building a website. See `_agentic_system_for`.
_AGENTIC_SYSTEM_CORE = (
    "You are an expert full-stack engineer building a COMPLETE, runnable project. "
    "Author the WHOLE app yourself using the tools: call write_file for EVERY file "
    "with real, production-quality code (no placeholders, no TODOs); use read_file / "
    "list_files to stay coherent across files (imports must resolve, exports must "
    "exist); call finish only when the app is complete and builds/runs with its "
    "standard command. Build exactly the kind of app the brief and the pinned stack "
    "describe — never silently switch to a different kind of project (e.g. do not turn "
    "a game, mobile, desktop, API or CLI brief into a website). "
    "You are building in a project workspace that may already contain generated assets, "
    "acceptance tests, or working files from an earlier stage/session. Inspect, preserve, "
    "and reuse them; never replace a real binary asset with text or delete acceptance "
    "tests. Write every code file you create or replace IN FULL: NEVER elide code with "
    "an edit-style placeholder such as '/* ... unchanged ... */', '// rest of the file "
    "is unchanged', '// ... existing code ...', or 'unchanged from the original'. Every "
    "function and class body must be fully implemented; an elided body ships a stub that "
    "crashes at run. "
    "Work efficiently: use write_files to create 2-6 independent small or medium files "
    "in one tool call; reserve write_file for a large core file that needs its own call. "
    "Do not spend one model round-trip per tiny config, style, test, or support file. "
)
# Web sites / web apps: react_vite, nextjs, astro, remix, static_html.
_AGENTIC_SYSTEM_WEB = (
    "The homepage MUST render MULTIPLE complete sections as real, "
    "separate components (hero, services, about, testimonials, contact) with real copy "
    "and any provided images under /assets, never a thin page or data-only scaffolding. "
    "When the brief implies distinct pages (a company's Services, About, Contact, "
    "etc.), ALSO create SEPARATE ROUTES (app/<name>/page.jsx) with a shared header/nav "
    "that links real routes, not only #anchor links on one page. Use meaningful, DISTINCT "
    "icons from a real icon set (e.g. lucide-react) or relevant /assets images — never one "
    "repeated generic placeholder shape. "
    "Apply a COHESIVE DESIGN SYSTEM used consistently across every component: a small "
    "disciplined color palette (one brand primary, one accent, neutral grays) defined as "
    "tokens (CSS vars or tailwind theme) and reused everywhere; a clear TYPOGRAPHIC SCALE "
    "(distinct sizes/weights for h1/h2/h3/body with readable line-height, loaded via "
    "next/font); consistent spacing, border-radius and shadows on a fixed scale; strong "
    "visual hierarchy, generous whitespace, and WCAG-AA contrast. Aim for the polish of a "
    "top-tier modern marketing site, not a default-styled template. "
    "Build a rich, polished, multi-page app that fully satisfies the brief. "
)
# Real-time games: phaser.
_AGENTIC_SYSTEM_GAME = (
    "This is a real-time GAME, not a website or marketing page: never build a landing "
    "page, hero/section components, or content pages. Build the actual game — a real "
    "game loop (update + render every frame), sprites/entities that move and collide, "
    "responsive player input, clearly REACHABLE win AND lose conditions, an on-screen "
    "score/HUD, and a game-over plus restart flow. The entry point must mount the game "
    "canvas and start the loop immediately. Make it polished and juicy — readable art, "
    "motion, and clear visual/audio feedback — but never substitute web or marketing UI "
    "for real gameplay. "
)
# Mobile apps — iOS / Android / phone: react_native.
_AGENTIC_SYSTEM_MOBILE = (
    "This is a MOBILE app (iOS/Android), not a website: build real screens wired through "
    "native navigation (a stack/tab navigator), touch-friendly controls, and platform-"
    "appropriate components — never a marketing homepage or desktop web layout. Use a "
    "shared design system (spacing, type scale, color tokens) across screens, respect "
    "safe-area insets, handle loading/empty/error states, and make every primary screen "
    "in the brief reachable through navigation. "
)
# Desktop apps — Windows / macOS / Linux: tauri.
_AGENTIC_SYSTEM_DESKTOP = (
    "This is a DESKTOP app (Windows/macOS/Linux), not a website: build a real application "
    "UI — an app window layout, menus/toolbars and keyboard shortcuts where appropriate, "
    "and views the user navigates between — not a centered marketing page. Persist app "
    "state/data locally as the brief implies, handle loading/empty/error states, and wire "
    "the front-end to the app's native/backend commands. "
)
# Backend services / APIs: fastapi, node_express.
_AGENTIC_SYSTEM_API = (
    "This is a BACKEND service/API, not a website: build real endpoints/routes with "
    "request validation, correct status codes, consistent JSON error handling, and a "
    "clear separation of routes, business logic, and data/models. Do NOT build marketing "
    "pages or UI components. Include a health check and run with the stack's standard "
    "server command. "
)
# Command-line tools: python_cli.
_AGENTIC_SYSTEM_CLI = (
    "This is a COMMAND-LINE tool, not a website: build a real CLI with argument/subcommand "
    "parsing, helpful --help output, clear stdout/stderr separation, sensible exit codes, "
    "and the actual functionality the brief describes. Do NOT build web pages or UI "
    "components. "
)
_AGENTIC_SYSTEM_TAIL = (
    "Do NOT use native provider SDKs or keys — route any LLM calls through "
    "the OpenAI SDK at https://openrouter.ai/api/v1 reading OPENROUTER_API_KEY."
)
# Stack -> body mapping. Keep these aligned with KNOWN_STACKS in skyn3t/agents/_common.py.
_WEB_UI_STACKS = frozenset({"react_vite", "nextjs", "astro", "remix", "static_html"})
_MOBILE_STACKS = frozenset({"react_native"})
_DESKTOP_STACKS = frozenset({"tauri"})
_API_STACKS = frozenset({"fastapi", "node_express"})
_CLI_STACKS = frozenset({"python_cli"})
# The registry object (skyn3t/core/stacks.py; core is upstream of adapters — no
# cycle). The other sets above are AGENT-vocab prompt routing and stay local.
from skyn3t.core.stacks import GAME_STACKS as _GAME_STACKS  # noqa: E402


def _agentic_system_for(stack: str) -> str:
    """Compose the agentic codegen system prompt for ``stack``. SkyN3t builds every kind
    of app, so the body is stack-specific (web / game / mobile / desktop / api / cli)
    instead of the old hardcoded 'build a React marketing site' that derailed non-web
    builds. An unknown/empty stack falls back to the web body (the react_vite default)."""
    if stack in _GAME_STACKS:
        body = _AGENTIC_SYSTEM_GAME
    elif stack in _MOBILE_STACKS:
        body = _AGENTIC_SYSTEM_MOBILE
    elif stack in _DESKTOP_STACKS:
        body = _AGENTIC_SYSTEM_DESKTOP
    elif stack in _API_STACKS:
        body = _AGENTIC_SYSTEM_API
    elif stack in _CLI_STACKS:
        body = _AGENTIC_SYSTEM_CLI
    elif (not stack) or stack in _WEB_UI_STACKS:
        body = _AGENTIC_SYSTEM_WEB
    else:
        body = ""
    return _AGENTIC_SYSTEM_CORE + body + _AGENTIC_SYSTEM_TAIL


# Back-compat default (web): the composed prompt for the react_vite default stack.
_AGENTIC_SYSTEM = _AGENTIC_SYSTEM_CORE + _AGENTIC_SYSTEM_WEB + _AGENTIC_SYSTEM_TAIL

# Text tool calls cannot produce real binary media. Rejecting these extensions
# prevents a model from satisfying an image path with SVG/prose bytes under a
# misleading `.webp`/`.png` name. SVG remains allowed because it is text.
_AGENTIC_BINARY_WRITE_EXTS = frozenset({
    ".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".mp3", ".mp4",
    ".ogg", ".otf", ".pdf", ".png", ".ttf", ".wav", ".webm", ".webp",
    ".woff", ".woff2",
})
# Pushed back into the loop when a model calls finish but delivered only scaffolding
# (data/config, a thin homepage, no section components) — makes cheap models that stop
# early actually build the full UI.
_ANTISTUB_NUDGE = (
    "The app is still a minimal stub: you wrote data/config but the homepage has little "
    "real UI and few/no section components. Do NOT finish yet. Build the COMPLETE UI now "
    "as real, separate section components (hero, services, about, testimonials, service "
    "areas, why-choose-us, contact) with real copy and the images under /assets, then "
    "assemble them all in the homepage so it renders a full, content-rich, multi-section page. "
    "CRITICAL: if the primary entry/homepage (for example app/page.jsx, "
    "src/pages/index.astro, index.html, src/App.vue, or src/routes/+page.svelte) is still "
    "a placeholder or counter demo, OVERWRITE it now and wire in the real UI; never leave "
    "the scaffold."
)
# The anti-stub nudge is for user-facing web stacks only. Each stack is inspected
# using its native source format below; applying a JSX-only heuristic to Astro,
# Vue, Svelte, or static HTML falsely labels complete apps as stubs.
_ANTISTUB_NUDGE_STACKS = frozenset({
    "react", "react_vite", "react_ts", "next", "nextjs", "remix",
    "astro", "static", "static_html", "html", "vue", "vuejs",
    "svelte", "sveltekit",
})


def _agentic_project_looks_stub(root: Path, stack: str) -> bool:
    """Judge a user-facing web project using that stack's native source files."""
    low = (stack or "react_vite").strip().lower()
    if low in {"astro"}:
        suffixes = {".astro", ".html", ".css", ".scss", ".js", ".ts"}
        entry_names = {"index.astro"}
    elif low in {"static", "static_html", "html"}:
        suffixes = {".html", ".htm", ".css", ".scss", ".js", ".mjs", ".ts"}
        entry_names = {"index.html", "index.htm"}
    elif low in {"vue", "vuejs"}:
        suffixes = {".vue", ".css", ".scss", ".js", ".ts"}
        entry_names = {"app.vue"}
    elif low in {"svelte", "sveltekit"}:
        suffixes = {".svelte", ".css", ".scss", ".js", ".ts"}
        entry_names = {"+page.svelte", "app.svelte"}
    else:
        suffixes = {".jsx", ".tsx", ".js", ".ts", ".css", ".scss"}
        entry_names = {
            "page.jsx", "page.tsx", "app.jsx", "app.tsx",
            "index.jsx", "index.tsx",
        }

    ui_bytes = 0
    surface_files = 0
    entry_bytes = 0
    scaffold_entry = False
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        parts = {part.lower() for part in rel.parts}
        if {"node_modules", ".next", ".git", "dist", "build"} & parts:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        ui_bytes += size
        is_entry = path.name.lower() in entry_names
        if is_entry:
            entry_bytes = max(entry_bytes, size)
            try:
                scaffold_entry = scaffold_entry or (
                    "generated offline by skyn3t"
                    in path.read_text(encoding="utf-8", errors="replace").lower()
                )
            except OSError:
                pass
        native_static_page = (
            low in {"static", "static_html", "html"}
            and path.suffix.lower() in {".html", ".htm"}
        )
        if (
            is_entry
            or native_static_page
            or parts & {"components", "sections", "pages", "routes", "views"}
        ):
            surface_files += 1

    # A rich single-file static page is valid; component frameworks should have
    # several real UI surfaces. Both shapes still need a substantive byte floor.
    return (
        scaffold_entry
        or ui_bytes < 4000
        or (surface_files < 3 and entry_bytes < 4000)
    )

# Doom-loop breaker (research item 49, opencode's DOOM_LOOP_THRESHOLD=3): three
# consecutive IDENTICAL tool calls (same name + same arguments) mean the model is
# stuck, not progressing. One corrective nudge; a second trip aborts the loop.
_DOOM_LOOP_NUDGE = (
    "You have made the identical tool call 3 times in a row — you are stuck. "
    "Do NOT repeat it. Take a DIFFERENT action: write the next missing file, "
    "read a file to re-orient, or call finish if the app is complete."
)


def _agentic_verify_problems(root: Path) -> list[str]:
    """Cheap verify-on-stop scan (research item 19): unresolved local JS/Python
    imports (the workstream-1 dangling-import class) + Python syntax errors.
    Static-only — no builds, no network; milliseconds. Returns [] on any scanner
    failure (degrade-open: the verifier must never brick a build). The studio
    import is lazy to keep adapters upstream of studio at module-load time."""
    problems: list[str] = []
    try:
        from skyn3t.studio.proof_run import (
            _unresolved_local_imports,
            _unresolved_python_imports,
        )

        problems += [f"unresolved local import: {s}"
                     for s in _unresolved_local_imports(root)[:5]]
        problems += [f"unresolved local import: {s}"
                     for s in _unresolved_python_imports(root)[:5]]
    except Exception:  # noqa: BLE001 - degrade-open
        return []
    for f in root.rglob("*.py"):
        if {"node_modules", ".git", ".venv", "__pycache__"} & set(f.parts):
            continue
        try:
            compile(f.read_text(encoding="utf-8", errors="replace"), str(f), "exec")
        except SyntaxError as e:
            problems.append(
                f"python syntax error in {f.relative_to(root)} line {e.lineno}: {e.msg}")
        except Exception:  # noqa: BLE001 - unreadable file is not this gate's job
            continue
    return problems[:8]


_AGENTIC_TOOLS = [
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a project file with its full content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "project-relative file path"},
            "content": {"type": "string", "description": "the complete file content"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "write_files",
        "description": (
            "Create or overwrite a batch of up to 8 project files. Prefer this for "
            "independent config, style, test, documentation, and support files."
        ),
        "parameters": {"type": "object", "properties": {
            "files": {"type": "array", "minItems": 1, "maxItems": 8,
                      "items": {"type": "object", "properties": {
                          "path": {"type": "string", "description": "project-relative path"},
                          "content": {"type": "string", "description": "complete file content"}},
                          "required": ["path", "content"]}}},
            "required": ["files"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file you have written, to stay coherent.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_files", "description": "List the project files written so far.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "finish", "description": "Call when the complete app is written and should build/run.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}}}},
]

# Flags that make each headless CLI ignore the host's ambient MCP servers, so a
# skyn3t build never boots the user's whole ~/.claude / ~/.copilot MCP fleet
# (Aruba, context7, playwright, ...) on every codegen call. claude + kimi (a
# claude-compatible fork): with --strict-mcp-config and no --mcp-config, zero MCP
# servers load. copilot: --disable-builtin-mcps drops its built-in github MCP.
_CLI_NO_MCP_ARGS: dict[str, list[str]] = {
    "claude": ["--strict-mcp-config"],
    "kimi": ["--strict-mcp-config"],
    "copilot": ["--disable-builtin-mcps"],
}

# StreamReader line-buffer for the agentic stream-json reader. One event line can
# be a big tool result or the full final output, far past asyncio's 64KB default.
_AGENTIC_STREAM_LIMIT = 64 * 1024 * 1024  # 64 MB (grows on demand)


def _no_mcp_args(settings, provider: str) -> list[str]:
    """MCP-disabling argv for ``provider`` when ``cli_disable_mcp`` is on (default)."""
    if not getattr(settings, "cli_disable_mcp", True):
        return []
    return list(_CLI_NO_MCP_ARGS.get(provider, []))


def _to_data_url(item: str) -> str:
    """Normalize an image reference to an OpenAI/OpenRouter-style data URL.

    A ``data:`` URL (or any http(s) URL) is passed through unchanged; a local
    file PATH is read and base64-encoded into a ``data:image/png;base64,...``
    URL. Mirrors the shape ``studio/visual_check._image_data_url`` already uses.
    """
    s = (item or "").strip()
    if s.startswith(("data:", "http://", "https://")):
        return s
    with open(s, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _is_vision_model(model: str) -> bool:
    """True when ``model`` is already known to be vision-capable."""
    low = (model or "").lower()
    return any(m in low for m in _VISION_MODEL_MARKERS)


def _is_free_model_id(model: str) -> bool:
    return router_is_free_model_id(model)


def _strip_code_fences(text: str) -> str:
    """Strip a leading/trailing ```json ... ``` fence if a CLI added one."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


# --- LLM call resilience: classify -> retry / failover / fail-fast ----------
# (deep-dive item 2/3: langchain ModelFallbackMiddleware + retry w/ backoff+jitter)
#
# One pure classifier drives both retry and model failover. It maps a raised
# LLM-call error to a recovery strategy:
#   "transient" -> retry the SAME model (429/5xx/timeout/connection); once the
#                  retry budget is spent, fail over to the next candidate model;
#   "model"     -> the MODEL is the problem (retired / invalid id / no endpoints);
#                  skip retry and fail over immediately;
#   "fatal"     -> auth / genuine bad request; a retry or a different model won't
#                  help, so fail fast and surface it.
# Status-first + provider-body aware — mirrors orchestrator.classify_error but is
# keyed on the EXCEPTION (status code + body), so it can tell a retired-model 404
# from a rate-limit 429 the coarse result-string scan can't.
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_MODEL_STATUS = frozenset({404})
_FATAL_STATUS = frozenset({401, 403})
# Provider-body markers that turn an ambiguous 400 into a MODEL error (else a 400
# is a genuine bad request -> fatal). OpenRouter phrases a dead/invalid model a
# few different ways.
_MODEL_ERROR_MARKERS = (
    "not a valid model", "is not a valid model", "no endpoints", "no allowed providers",
    "model_not_found", "unknown model", "invalid model", "model not found",
    "does not exist", "no such model",
)
# Tool-result messages at or below this size aren't file dumps (they're "OK wrote"
# / error strings), so context editing leaves them alone.
_ELIDE_MIN_BYTES = 200


def _err_status(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return int(code) if isinstance(code, int) else None


def _err_body(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        return resp.text or ""
    except Exception:  # noqa: BLE001 - a body we can't read just isn't a model marker
        return ""


def _err_reason(exc: BaseException | None) -> str:
    """Short, log-safe reason tag for a failover/retry event."""
    if exc is None:
        return "unknown"
    s = _err_status(exc)
    return f"http_{s}" if s is not None else type(exc).__name__


class _AgenticTurnStall(TimeoutError):
    """A single agentic OpenRouter turn exceeded the no-progress watchdog."""


def classify_llm_error(exc: BaseException) -> str:
    """Pure recovery-strategy classifier for a failed LLM call.

    Returns ``"transient"`` (retry same model), ``"model"`` (fail over to the next
    candidate), or ``"fatal"`` (fail fast)."""
    if isinstance(exc, _AgenticTurnStall):
        return "model"
    # Connection resets / timeouts / read errors (all httpx.TransportError) are
    # transient by nature; a bounded retry usually clears them.
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return "transient"
    status = _err_status(exc)
    if status is None:
        # Opaque, non-HTTP failure (a flaky gateway returning junk we couldn't
        # parse): retry it — the retry budget caps how long that can go.
        return "transient"
    if status in _MODEL_STATUS:
        return "model"
    if status == 400:
        return "model" if any(m in _err_body(exc).lower() for m in _MODEL_ERROR_MARKERS) else "fatal"
    if status in _FATAL_STATUS:
        return "fatal"
    if status in _TRANSIENT_STATUS:
        return "transient"
    if 400 <= status < 500:
        return "fatal"   # other client errors won't self-heal
    return "transient"    # unknown 5xx / informational -> retry


@dataclass
class LLMResult:
    text: str
    model: str
    backend: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    build_id: str | None = None


@dataclass
class _BuildBudgetState:
    """Mutable state deliberately shared by child asyncio task contexts."""

    build_id: str
    spent_usd: float = 0.0


_BUILD_BUDGET_STATE: contextvars.ContextVar[_BuildBudgetState | None] = (
    contextvars.ContextVar("skyn3t_budget_build_state", default=None)
)

# A changing rewrite can evade the identical-call breaker forever. Detect only
# strongly dominant repeated-path churn: this is deliberately not a total turn,
# file, token, or duration cap. One warning lets the model move on to missing
# files; a second dominant window aborts the session while preserving all work so
# CodeAgent's targeted in-place retry can complete the remaining contract.
_PATH_CHURN_WINDOW = 16
_PATH_CHURN_THRESHOLD = 8
_PATH_CHURN_NUDGE = (
    "You are stuck repeatedly rewriting `{path}` ({count} of the last "
    f"{_PATH_CHURN_WINDOW} changed file writes). Preserve its current working version "
    "and STOP touching it for now. Use write_files to create the remaining missing "
    "files in batches, then read and revisit this path once only if final wiring truly "
    "requires it. Call finish when the planned app is complete."
)

# A model can evade same-path detection by inventing a new filename for the same
# redundant install/build wrapper on every turn. This guard is plan-aware and
# resets whenever an approved file appears, so it does not cap app size or useful
# exploratory work. It only acts on a sustained family of unplanned helper scripts
# while approved architecture files are still missing.
_AUXILIARY_CHURN_THRESHOLD = 6
_AUXILIARY_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".cjs", ".js", ".mjs", ".ps1", ".sh"})
_AUXILIARY_SCRIPT_TOKENS = frozenset(
    {"boot", "build", "ci", "dev", "install", "run", "setup", "start", "test", "verify"}
)
_AUXILIARY_CHURN_NUDGE = (
    "You are creating many redundant unplanned install/build wrapper scripts while "
    "approved architecture files are still missing. STOP inventing helper variants. "
    "Preserve the current app, use write_files to create the missing planned files "
    "directly, and call finish only after they are wired."
)

_PLAN_COMPLETE_NUDGE = (
    "Every approved architecture file now exists. Make one final wiring and "
    "consistency pass across the delivered app, fixing imports or integration "
    "issues you find. Do not add redundant helper variants. Then call finish."
)


def _agentic_auxiliary_family(path: str) -> str:
    """Classify path-variant build wrappers without matching normal app files."""
    normalized = str(path or "").replace("\\", "/").strip().lower().strip("/")
    if not normalized:
        return ""
    candidate = Path(normalized)
    if candidate.suffix not in _AUXILIARY_SCRIPT_SUFFIXES:
        return ""
    if "/" in normalized and not normalized.startswith("scripts/"):
        return ""
    tokens = {token for token in re.split(r"[^a-z0-9]+", candidate.stem) if token}
    return "build_helper" if tokens & _AUXILIARY_SCRIPT_TOKENS else ""


@contextmanager
def _exclusive_ledger_lock(ledger_path: Path) -> Iterator[None]:
    """Lock a sibling file across processes without optional dependencies."""
    lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise BudgetExceeded(f"budget ledger lock unavailable: {exc}") from exc

    locked = False
    deadline = time.monotonic() + _LEDGER_LOCK_TIMEOUT_S
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while not locked:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise BudgetExceeded("timed out waiting for the budget ledger lock") from exc
                    time.sleep(0.02)
        else:
            fcntl = importlib.import_module("fcntl")

            while not locked:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise BudgetExceeded("timed out waiting for the budget ledger lock") from exc
                    time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


@dataclass
class BudgetTracker:
    """Hard backstop on spend, safe across tasks and local processes."""
    per_build_cap: float
    daily_cap: float
    token_cap: int
    ledger_path: Path | None = None
    spent_build: float = 0.0
    spent_day: float = 0.0
    tokens_day: int = 0
    calls: list[LLMResult] = field(default_factory=list)
    ledger_day: str = field(default_factory=lambda: date.today().isoformat())

    def __post_init__(self) -> None:
        with self._ledger_guard():
            self._load_ledger()

    def record(self, r: LLMResult) -> None:
        with self._ledger_guard():
            self._rollover_if_needed()
            # Disk is authoritative inside the process-wide transaction. Using
            # max(local, disk) here loses updates when two processes start with
            # the same stale snapshot and then perform read-modify-write.
            self._load_ledger()
            self.spent_day += r.cost_usd
            self.tokens_day += r.prompt_tokens + r.completion_tokens
            state = _BUILD_BUDGET_STATE.get()
            if state is None:
                self.spent_build += r.cost_usd
            else:
                state.spent_usd += r.cost_usd
                self.spent_build = state.spent_usd
                if r.build_id is None:
                    r.build_id = state.build_id
            self.calls.append(r)
            self._save_ledger()

    def check(self) -> None:
        with self._ledger_guard():
            self._rollover_if_needed()
            self._load_ledger(merge=True)
            spent_build = self.current_build_spend()
            if self.per_build_cap > 0 and spent_build > self.per_build_cap:
                raise BudgetExceeded(
                    f"per-build cap ${self.per_build_cap} exceeded (${spent_build:.4f})"
                )
            if self.daily_cap > 0 and self.spent_day > self.daily_cap:
                raise BudgetExceeded(
                    f"daily cap ${self.daily_cap} exceeded (${self.spent_day:.4f})"
                )
            if self.token_cap > 0 and self.tokens_day > self.token_cap:
                raise BudgetExceeded(
                    f"daily token cap {self.token_cap} exceeded ({self.tokens_day})"
                )

    def reset_build(self) -> None:
        # Scoped builds are started by CostTracker.begin_build. A private helper
        # client may call reset_build while it is running inside that context;
        # resetting the shared state there would let the build escape its cap.
        if _BUILD_BUDGET_STATE.get() is not None:
            return
        self.spent_build = 0.0

    def begin_build(self, build_id: str) -> contextvars.Token:
        """Begin task-local accounting shared with any child asyncio tasks."""
        self.spent_build = 0.0
        return _BUILD_BUDGET_STATE.set(_BuildBudgetState(build_id=build_id))

    def end_build(self, token: contextvars.Token) -> float:
        """Close a build context and return its final spend."""
        state = _BUILD_BUDGET_STATE.get()
        spent = state.spent_usd if state is not None else self.spent_build
        _BUILD_BUDGET_STATE.reset(token)
        self.spent_build = spent
        return spent

    def current_build_spend(self) -> float:
        state = _BUILD_BUDGET_STATE.get()
        return state.spent_usd if state is not None else self.spent_build

    @contextmanager
    def _ledger_guard(self) -> Iterator[None]:
        with _BUDGET_LEDGER_LOCK:
            if self.ledger_path is None:
                yield
            else:
                with _exclusive_ledger_lock(self.ledger_path):
                    yield

    def _load_ledger(self, *, merge: bool = False) -> None:
        if self.ledger_path is None:
            return
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        today = date.today().isoformat()
        if data.get("day") != today:
            return
        try:
            spent_day = float(data.get("spent_day", 0.0) or 0.0)
            tokens_day = int(data.get("tokens_day", 0) or 0)
            if not math.isfinite(spent_day) or spent_day < 0 or tokens_day < 0:
                raise ValueError("invalid negative or non-finite budget ledger")
            if merge:
                self.spent_day = max(self.spent_day, spent_day)
                self.tokens_day = max(self.tokens_day, tokens_day)
            else:
                self.spent_day = spent_day
                self.tokens_day = tokens_day
            self.ledger_day = today
        except (TypeError, ValueError):
            if not merge:
                self.spent_day = 0.0
                self.tokens_day = 0

    def _save_ledger(self) -> None:
        if self.ledger_path is None:
            return
        try:
            atomic_write_text(
                self.ledger_path,
                json.dumps({
                    "day": self.ledger_day,
                    "spent_day": self.spent_day,
                    "tokens_day": self.tokens_day,
                }, sort_keys=True),
            )
        except OSError:
            pass

    def _rollover_if_needed(self) -> None:
        today = date.today().isoformat()
        if self.ledger_day == today:
            return
        # A long-lived tracker may wake after midnight after another tracker has
        # already recorded usage for the new day. Adopt that ledger before
        # deciding whether a zero-value rollover needs to be initialized.
        self._load_ledger()
        if self.ledger_day == today:
            return
        self.ledger_day = today
        self.spent_day = 0.0
        self.tokens_day = 0
        self._save_ledger()


class BudgetExceeded(RuntimeError):
    pass


def _build_router(settings: Settings) -> ModelRouter:
    """The deterministic base router, or the learned router when BOTH
    ``model_evolution`` and ``auto_route`` are enabled (opt-in).

    The learned router prefers models the :class:`ModelTournament` has seen win;
    with no evidence it abstains and behaves exactly like the base router. Any
    failure degrades safely to the base router (design rules #4 safe, #6 degrade).
    """
    if getattr(settings, "model_evolution", False) and getattr(settings, "auto_route", False):
        try:
            from skyn3t.intelligence.model_tournament import ModelTournament
            from skyn3t.intelligence.routing_recommendations import (
                LearnedModelRouter,
                RoutingRecommender,
            )

            tournament = ModelTournament(settings.data_dir / "model_tournament.json")
            return LearnedModelRouter(RoutingRecommender(tournament), settings=settings)
        except Exception:  # noqa: BLE001 - routing must never break the client
            pass
    return ModelRouter(settings)


class LLMClient:
    def __init__(self, settings: Settings | None = None, router: ModelRouter | None = None) -> None:
        self.settings = settings or get_settings()
        self.router = router or _build_router(self.settings)
        self.budget = BudgetTracker(
            per_build_cap=self.settings.per_build_usd_cap,
            daily_cap=self.settings.daily_usd_cap,
            token_cap=self.settings.daily_token_cap,
            ledger_path=self.settings.data_dir / "budget" / "daily_usage.json",
        )
        # Route capture is HYBRID: a task-local contextvar (so concurrent agent
        # runs sharing this client don't clobber each other) plus a global
        # last-write (so a SYNC / cross-context reader — e.g. `asyncio.run(
        # complete())` then read last_model — still sees the result). In-task
        # reads prefer the contextvar; out-of-task reads fall back to the globals.
        self._g_last_model: str | None = None
        self._g_last_route: tuple[str, str] | None = None
        self._g_routes: list[tuple[str, str, str]] = []
        self._unhealthy_models: dict[str, float] = {}

    # ---- per-run route capture (task-local + global fallback) ---------------
    def begin_run_capture(self) -> None:
        """Start an isolated, task-local route capture for THIS run so concurrent
        agent runs that share this client don't clobber each other. Agents call
        this at run start (replacing a bare ``routes.clear()``)."""
        _LAST_MODEL.set(None)
        _LAST_ROUTE.set(None)
        _ROUTES.set([])

    def _resolve_pinned_model(
        self,
        *,
        tier: Tier,
        model_override: str | None = None,
        setting_names: tuple[str, ...] = (),
        file_hint: str | None = None,
        task_type: str = "",
    ) -> str:
        requested: list[str] = []
        if model_override:
            requested.append(str(model_override).strip())
        for name in setting_names:
            val = str(getattr(self.settings, name, "") or "").strip()
            if val:
                requested.append(val)
        for pinned in requested:
            if bool(getattr(self.settings, "free_only", False)) and not _is_free_model_id(pinned):
                log.warning("llm.free_only_ignored_paid_pin", model=pinned)
                continue
            return pinned
        return self.router.resolve(tier, file_hint, task_type=task_type)

    def _record_completion(self, tier: str, task_type: str, model: str) -> None:
        """Record one completion's route into both the task-local capture (if a
        run is active) and the bounded global last-write."""
        self._g_last_model = model
        self._g_last_route = (tier, task_type)
        self._g_routes.append((tier, task_type, model))
        if len(self._g_routes) > 256:  # bound the global; agents read the task-local slice
            del self._g_routes[: len(self._g_routes) - 256]
        _LAST_MODEL.set(model)
        _LAST_ROUTE.set((tier, task_type))
        tl = _ROUTES.get()
        if tl is not None:
            tl.append((tier, task_type, model))

    def _record_openrouter_agentic_usage(
        self, model: str, data: dict[str, Any], sent: list[dict[str, Any]], msg: dict[str, Any]
    ) -> None:
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        if not _is_free_model_id(model) and pt == 0 and ct == 0:
            pt = max(1, sum(self._msg_bytes(m) for m in sent) // 4)
            ct = max(1, len(json.dumps(msg, sort_keys=True, default=str)) // 4)
            log.warning("llm.openrouter_agentic_usage_missing", model=model, est_tokens=pt + ct)
        cost = 0.0 if _is_free_model_id(model) else (pt + ct) / 1_000_000 * 0.5
        result = LLMResult(
            text=str(msg.get("content") or ""),
            model=model,
            backend="openrouter",
            prompt_tokens=int(pt or 0),
            completion_tokens=int(ct or 0),
            cost_usd=cost,
        )
        self.budget.record(result)
        self.budget.check()

    @property
    def last_model(self) -> str | None:
        """Model id of the most recent completion (real model for openrouter,
        ``<provider>-cli`` for CLI, resolved model for stub). In-task: this run's;
        out-of-task: the global last-write. Agents stamp ``TaskResult.model_id``."""
        v = _LAST_MODEL.get()
        return v if v is not None else self._g_last_model

    @last_model.setter
    def last_model(self, value: str | None) -> None:
        _LAST_MODEL.set(value)
        self._g_last_model = value

    @property
    def last_route(self) -> tuple[str, str] | None:
        """(tier, task_type) the most recent completion routed through."""
        v = _LAST_ROUTE.get()
        return v if v is not None else self._g_last_route

    @last_route.setter
    def last_route(self, value: tuple[str, str] | None) -> None:
        _LAST_ROUTE.set(value)
        self._g_last_route = value

    @property
    def routes(self) -> list[tuple[str, str, str]]:
        """Every (tier, task_type, model) routed, in order. In-task: this run's
        isolated slice (see begin_run_capture); out-of-task: the global list."""
        v = _ROUTES.get()
        return v if v is not None else self._g_routes

    @routes.setter
    def routes(self, value: list[tuple[str, str, str]]) -> None:
        _ROUTES.set(list(value))
        self._g_routes = list(value)

    _cli_cache: dict[str, bool] = {}

    @classmethod
    def _cli_available(cls, provider: str) -> bool:
        if provider not in cls._cli_cache:
            cls._cli_cache[provider] = shutil.which(provider) is not None
        return cls._cli_cache[provider]

    @property
    def backend(self) -> str:
        """Resolve the active backend from policy + availability."""
        pref = (self.settings.llm_backend or "auto").lower()
        if pref == "stub":
            return "stub"
        if pref == "openrouter":
            return "openrouter" if openrouter_key(self.settings) else "stub"
        if pref.endswith("_cli"):
            prov = pref[:-4]
            return f"{prov}_cli" if self._cli_available(prov) else "stub"
        # auto: OpenRouter key wins, else a detected CLI, else stub.
        if openrouter_key(self.settings):
            return "openrouter"
        preferred = (self.settings.cli_llm_provider or "claude").lower()
        for prov in (preferred, *_KNOWN_CLI_PROVIDERS):
            if self._cli_available(prov):
                return f"{prov}_cli"
        return "stub"

    def backend_status(self) -> dict:
        """Explain backend resolution without exposing secret values.

        ``backend`` remains execution-oriented for compatibility: explicit
        OpenRouter without a key still executes through the stub. This status
        object is the operator-facing truth: it records the requested backend,
        active backend, and why a fallback happened.
        """
        pref = (self.settings.llm_backend or "auto").lower()
        active = self.backend
        openrouter_configured = bool(openrouter_key(self.settings))
        preferred_cli = (getattr(self.settings, "cli_llm_provider", "") or "claude").lower()
        cli_available = {p: self._cli_available(p) for p in _KNOWN_CLI_PROVIDERS}
        state = "ready"
        reason = ""
        if pref == "openrouter" and not openrouter_configured:
            state = "missing_key"
            reason = "OpenRouter was selected but SKYN3T_OPENROUTER_API_KEY is not configured."
        elif pref.endswith("_cli") and active == "stub":
            state = "cli_missing"
            reason = f"{pref[:-4]} CLI was selected but is not available on PATH."
        elif pref == "auto" and active == "stub":
            state = "auto_stub"
            reason = "Auto found no OpenRouter key and no supported local CLI."

        codegen_provider = str(getattr(self.settings, "codegen_cli_provider", "") or "").strip().lower()
        codegen_model = str(getattr(self.settings, "codegen_cli_model", "") or "").strip()
        openrouter_codegen_model = str(
            getattr(self.settings, "openrouter_codegen_model", "") or ""
        ).strip()
        codegen_provider_available = (
            self._cli_available(codegen_provider) if codegen_provider else False
        )
        if codegen_provider and codegen_provider_available:
            codegen_backend = f"{codegen_provider}_cli"
            codegen_reason = "codegen_cli_provider overrides the global backend for codegen."
        elif codegen_provider:
            codegen_backend = active
            codegen_reason = (
                f"codegen_cli_provider={codegen_provider!r} is set but unavailable; "
                "codegen follows the active backend."
            )
        elif active == "openrouter":
            codegen_backend = "openrouter"
            codegen_reason = "codegen follows the global OpenRouter backend."
        else:
            codegen_backend = active
            codegen_reason = "codegen follows the active backend."

        return {
            "requested": pref,
            "active": active,
            "state": state,
            "reason": reason,
            "openrouter_configured": openrouter_configured,
            "preferred_cli": preferred_cli,
            "cli_available": cli_available,
            "free_only": bool(getattr(self.settings, "free_only", False)),
            "no_claude": bool(getattr(self.settings, "no_claude", False)),
            "codegen": {
                "backend": codegen_backend,
                "reason": codegen_reason,
                "cli_provider": codegen_provider,
                "cli_provider_available": codegen_provider_available,
                "cli_model": codegen_model,
                "openrouter_model": openrouter_codegen_model,
            },
        }

    # ---- call resilience: transient retry + ordered model failover ----------
    def _retry_budget(self) -> int:
        if not getattr(self.settings, "llm_retry_enabled", True):
            return 0
        return max(0, int(getattr(self.settings, "llm_max_retries", 3)))

    def _retry_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter (jitter staggers concurrent retries off
        a shared failing endpoint — the same shape as orchestrator._backoff_delay,
        but tuned for a single LLM call)."""
        base = float(getattr(self.settings, "llm_retry_base_delay", 0.5))
        cap = float(getattr(self.settings, "llm_retry_max_delay", 8.0))
        d = min((2 ** attempt) * base, cap)
        return d + random.uniform(0.0, d * 0.5)

    def _fallback_models(self, primary: str, tier: Tier) -> list[str]:
        """Ordered fallback model ids AFTER ``primary``: the operator override
        (``llm_fallback_models`` comma-list) first, then the router's per-tier
        candidates. De-duped, ``primary`` removed. ``[]`` when failover is off."""
        if not getattr(self.settings, "llm_fallback_enabled", True):
            return []
        out: list[str] = []
        free_only = bool(getattr(self.settings, "free_only", False))
        for m in str(getattr(self.settings, "llm_fallback_models", "") or "").split(","):
            m = m.strip()
            if not m:
                continue
            if free_only and not _is_free_model_id(m):
                log.warning("llm.free_only_ignored_paid_fallback", model=m)
                continue
            if m and m != primary and m not in out:
                out.append(m)
        try:
            for m in self.router.fallback_candidates(tier, primary=primary):
                if m and m != primary and m not in out:
                    out.append(m)
        except Exception as exc:  # noqa: BLE001 - fallback resolution must never crash a call
            log.warning("llm.fallback_resolve_error", error=str(exc)[:120])
        return out

    def _model_unhealthy(self, model: str) -> bool:
        model = str(model or "").strip()
        if not model:
            return False
        until = self._unhealthy_models.get(model)
        if not until:
            return False
        if until > time.monotonic():
            return True
        self._unhealthy_models.pop(model, None)
        return False

    def _healthy_fallback_models(self, primary: str, tier: Tier) -> list[str]:
        return [m for m in self._fallback_models(primary, tier) if not self._model_unhealthy(m)]

    def _mark_model_unhealthy(self, model: str, exc: BaseException | None) -> None:
        model = str(model or "").strip()
        if not model:
            return
        ttl = max(0.0, float(getattr(self.settings, "llm_unhealthy_ttl_seconds", 300.0) or 0.0))
        if ttl <= 0:
            return
        until = time.monotonic() + ttl
        previous = self._unhealthy_models.get(model, 0.0)
        self._unhealthy_models[model] = max(previous, until)
        log.warning("llm.model_quarantined", model=model, ttl_s=ttl, reason=_err_reason(exc))

    async def _resilient_call(self, primary: str, tier: Tier, attempt_fn):
        """Run ``attempt_fn(model)`` with bounded transient retry + ordered model
        failover. ``attempt_fn`` is an async callable taking a model id and returning
        the call result or raising.

        Fallback candidates are resolved LAZILY (only once the primary misses) so the
        happy path never pays the router lookup. A ``fatal`` error short-circuits
        immediately; on total exhaustion the ORIGINAL (primary) error is re-raised —
        so the caller sees the real root cause, not a downstream fallback's noise."""
        max_retries = self._retry_budget()
        first_exc: BaseException | None = None
        candidates = [primary]
        if self._model_unhealthy(primary):
            fallbacks = self._healthy_fallback_models(primary, tier)
            if fallbacks:
                log.info("llm.model_quarantine_skip",
                         model=primary,
                         to=fallbacks[0],
                         tier=getattr(tier, "value", str(tier)))
                candidates = fallbacks
        ci = 0
        while ci < len(candidates):
            model = candidates[ci]
            model_exc: BaseException | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await attempt_fn(model)
                except Exception as exc:  # noqa: BLE001 - classified for recovery
                    if first_exc is None:
                        first_exc = exc
                    model_exc = exc
                    cat = classify_llm_error(exc)
                    if cat == "fatal":
                        raise
                    if cat == "transient" and attempt < max_retries:
                        log.info("llm.retry", model=model, attempt=attempt + 1,
                                 reason=_err_reason(exc))
                        await asyncio.sleep(self._retry_delay(attempt))
                        continue
                    break  # transient budget spent, or a model error -> try a fallback
            # Resolve fallbacks lazily the first time the primary misses.
            if ci == 0 and len(candidates) == 1:
                candidates.extend(self._healthy_fallback_models(primary, tier))
            if ci + 1 < len(candidates):
                self._mark_model_unhealthy(model, model_exc or first_exc)
                log.info("llm.model_failover",
                         **{"from": model, "to": candidates[ci + 1],
                            "tier": getattr(tier, "value", str(tier)),
                            "reason": _err_reason(first_exc)})
            ci += 1
        assert first_exc is not None  # the loop only exits here after a failure
        raise first_exc

    # ---- agentic context editing (deep-dive item 4: ClearToolUsesEdit) ------
    @staticmethod
    def _msg_bytes(m: dict) -> int:
        try:
            return len(json.dumps(m, ensure_ascii=False).encode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return len(str(m).encode("utf-8", "replace"))

    def _edit_context(self, messages: list[dict]) -> list[dict]:
        """Return a possibly-shrunk COPY of the agentic history for SENDING.

        When the serialized history exceeds ``agentic_context_budget_bytes``, OLD
        tool-result messages (read_file/list_files file dumps) are replaced with a
        short stub — oldest first, until back under budget — while preserving the
        system prompt, the user goal, all assistant/tool-call structure, and the
        most recent ``agentic_context_keep_last`` (K) tool results intact. The
        caller's canonical ``messages`` is NEVER mutated. Degrade-open: the feature
        off, or any error, returns the original list unchanged."""
        try:
            if not getattr(self.settings, "agentic_context_editing", True):
                return messages
            budget = int(getattr(self.settings, "agentic_context_budget_bytes", 200_000))
            if budget <= 0:
                return messages
            total = sum(self._msg_bytes(m) for m in messages)
            if total <= budget:
                return messages
            keep = max(0, int(getattr(self.settings, "agentic_context_keep_last", 6)))
            tool_idxs = [i for i, m in enumerate(messages)
                         if isinstance(m, dict) and m.get("role") == "tool"]
            protected = set(tool_idxs[-keep:]) if keep else set()
            edited = [dict(m) for m in messages]  # shallow per-message copy
            for i in tool_idxs:
                if i in protected or total <= budget:
                    continue
                content = messages[i].get("content")
                if not isinstance(content, str):
                    continue
                n = len(content.encode("utf-8", "replace"))
                if n <= _ELIDE_MIN_BYTES:
                    continue  # a short result (error / "OK wrote") isn't a dump
                before_b = self._msg_bytes(edited[i])
                edited[i] = {**edited[i],
                             "content": f"[{n} bytes elided — re-read the file if needed]"}
                total -= before_b - self._msg_bytes(edited[i])  # consistent w/ `total` units
            return edited
        except Exception as exc:  # noqa: BLE001 - context editing must never break a build
            log.warning("llm.context_edit_error", error=str(exc)[:120])
            return messages

    async def complete(
        self,
        prompt: str,
        tier: Tier = Tier.CHEAP,
        *,
        system: str | None = None,
        file_hint: str | None = None,
        max_tokens: int = 2048,
        json_mode: bool = False,
        task_type: str = "",
        model_override: str | None = None,
        images: list[str] | None = None,
    ) -> LLMResult:
        # Refuse before dispatch when a persisted/shared counter is already over
        # cap. The post-record check still catches the call that crosses a cap.
        self.budget.check()
        # Pass task_type so the LearnedModelRouter can serve per-task picks. It
        # was dead-wired (resolve(tier, file_hint) only) -> the learned router
        # always queried the empty task bucket and could never serve.
        # ``model_override`` pins a specific model (best-of-N cross-model sampling)
        # and normally bypasses the router; the (tier, task_type) bucket is
        # unchanged. ``free_only`` is the hard cost guard: a paid manual/preferred
        # pin is ignored unless it is itself an OpenRouter ":free" model.
        requested_override = (model_override or "").strip()
        model = self._resolve_pinned_model(
            tier=tier,
            model_override=requested_override,
            setting_names=("preferred_model",),
            file_hint=file_hint,
            task_type=task_type,
        )
        vision_override = requested_override if requested_override and model == requested_override else None
        backend = self.backend
        # An attached image only matters to the openrouter backend (the only one
        # that speaks the multimodal message shape). stub/CLI ignore it and behave
        # exactly as today — degrade, don't crash (design rule #6).
        if backend == "openrouter" and images:
            model, send_images = self._resolve_vision(model, vision_override)
            if send_images:
                result = await self._openrouter(model, prompt, system, max_tokens, json_mode, images, tier=tier)
            else:
                # free_only with no usable free vision model: stay text-only rather
                # than silently billing a paid model (design rule #5 cheap-by-default).
                result = await self._openrouter(model, prompt, system, max_tokens, json_mode, tier=tier)
        elif backend == "openrouter":
            result = await self._openrouter(model, prompt, system, max_tokens, json_mode, tier=tier)
        elif backend.endswith("_cli"):
            result = await self._cli(backend[:-4], prompt, system, json_mode, images)
        else:
            result = self._stub(model, prompt, system, json_mode)
        tier_s = getattr(tier, "value", str(tier))
        self._record_completion(tier_s, task_type, result.model)
        self.budget.record(result)
        self.budget.check()
        return result

    def _resolve_vision(self, resolved: str, model_override: str | None) -> tuple[str, bool]:
        """Choose the model for an image-bearing call AND whether to send images.

        Returns ``(model, send_images)``. Honors cheap-by-default (design rule #5):
        under ``free_only`` we only spend on a paid vision model when the operator
        EXPLICITLY configured ``settings.vision_model`` — otherwise we keep the free
        resolved model and DON'T send the image (text-only), rather than silently
        billing the paid default. Honors ``no_claude``. A model already vision-capable
        (or an explicit vision ``model_override``) is used as-is."""
        if model_override and _is_vision_model(model_override):
            return model_override, True
        if _is_vision_model(resolved):
            return resolved, True

        def _is_claude(m: str) -> bool:
            ml = m.lower()
            return "claude" in ml or "anthropic" in ml

        def _is_free(m: str) -> bool:
            return _is_free_model_id(m)

        configured = str(getattr(self.settings, "vision_model", "") or "").strip()
        no_claude = bool(getattr(self.settings, "no_claude", False))
        free_only = bool(getattr(self.settings, "free_only", False))

        if configured:  # operator opted into a specific vision model — honor it
            if no_claude and _is_claude(configured):
                return resolved, False  # forbidden by policy -> drop the image
            if free_only and not _is_free(configured):
                log.warning("llm.vision_paid_under_free_only", model=configured)
            return configured, True
        # No vision model configured.
        if free_only:
            # Don't silently bill the paid default; keep the free model, drop image.
            log.info("llm.vision_skipped_free_only",
                     note="no free vision model configured; reference image not sent")
            return resolved, False
        if no_claude and _is_claude(_DEFAULT_VISION_MODEL):
            return resolved, False
        return _DEFAULT_VISION_MODEL, True

    @property
    def supports_image_input(self) -> bool:
        """Whether the active backend can accept an INPUT reference image
        ("build from a picture"). OpenRouter speaks the multimodal HTTP shape;
        the CLI agents (claude/kimi) read an image FILE referenced in the prompt.
        The stub backend cannot — agents check this before attaching an image."""
        b = self.backend
        return b == "openrouter" or b.endswith("_cli")

    @staticmethod
    def _ensure_image_path(image: str) -> str | None:
        """Return a local FILE PATH for an image reference a CLI agent can read.

        A ``data:`` URL is decoded to a temp file (the CLI can't read inline data);
        an existing local path is used as-is; a remote URL is skipped (the CLI
        can't fetch it, and we won't make the host fetch an arbitrary URL).
        Returns ``None`` when there is nothing usable. Never raises.
        """
        s = (image or "").strip()
        if not s:
            return None
        if s.startswith("data:"):
            try:
                header, _, b64 = s.partition(",")
                if not b64:
                    return None
                raw = base64.b64decode(b64, validate=False)
                ext = "png"
                if "image/jpeg" in header or "image/jpg" in header:
                    ext = "jpg"
                elif "image/webp" in header:
                    ext = "webp"
                fd, path = tempfile.mkstemp(suffix=f".{ext}", prefix="skyn3t_ref_")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(raw)
                return path
            except Exception:  # noqa: BLE001 - an image must never break a build
                return None
        if s.startswith(("http://", "https://")):
            return None  # CLI can't fetch remote; don't make the host fetch arbitrary URLs
        return s if os.path.exists(s) else None

    async def _cli(self, provider, prompt, system, json_mode, images=None) -> LLMResult:
        """Run a locally-installed coding-agent CLI in headless print mode.

        Degrades to the stub backend (never raises) if the CLI fails or times
        out, so a build keeps moving (design rule #6).
        """
        argv = [*_CLI_COMMANDS.get(provider, [provider, "-p"]),
                *_no_mcp_args(self.settings, provider)]
        full = prompt if not system else f"{system}\n\n{prompt}"
        # build-from-image: reference the image FILE(S) so the CLI reads them as a
        # visual reference ALONGSIDE the full text context — the same pattern the
        # vision judge (studio/visual_check) uses. A data: URL is written to a
        # temp file first; the image is prepended so it frames the context below.
        temp_images: list[str] = []
        if images:
            paths = [p for p in (self._ensure_image_path(i) for i in images) if p]
            # Only the data:-URL temp files WE created (skyn3t_ref_*) get cleaned
            # up below — never a user-supplied local image path.
            temp_images = [p for p in paths if os.path.basename(p).startswith("skyn3t_ref_")]
            if paths:
                refs = "; ".join(f"the image file at {p}" for p in paths)
                full = (f"View {refs} as a visual reference for the request below, "
                        f"then:\n\n{full}")
        if json_mode:
            full += "\n\nRespond with ONLY valid JSON — no prose, no code fences."
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, full,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group -> killable as a tree
            )
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=self.settings.cli_llm_timeout
            )
        except TimeoutError:
            log.warning("llm.cli_timeout", provider=provider, timeout=self.settings.cli_llm_timeout)
            await self._terminate(proc)  # don't orphan the CLI subprocess
            return self._stub(f"{provider}-cli", prompt, system, json_mode)
        except asyncio.CancelledError:
            # An outer stage timeout cancelled us — kill the tree before unwinding
            # so a slow ``claude -p`` can't keep running for 90 minutes orphaned.
            await self._terminate(proc)
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.cli_failed", provider=provider, error=str(exc)[:160])
            await self._terminate(proc)
            return self._stub(f"{provider}-cli", prompt, system, json_mode)
        finally:
            # Don't leak the decoded data:-URL reference images into the temp dir.
            for tp in temp_images:
                try:
                    os.unlink(tp)
                except OSError:
                    pass

        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0 and not text:
            log.warning("llm.cli_nonzero", provider=provider,
                        err=(err or b"").decode("utf-8", "replace")[:160])
            return self._stub(f"{provider}-cli", prompt, system, json_mode)
        if json_mode:
            text = _strip_code_fences(text)
        approx_p = max(1, len(full) // 4)
        return LLMResult(
            text=text, model=f"{provider}-cli", backend=f"{provider}_cli",
            prompt_tokens=approx_p, completion_tokens=max(1, len(text) // 4), cost_usd=0.0,
        )

    async def _openrouter_agentic(
        self,
        prompt: str,
        workdir: str,
        model: str,
        timeout: int | None = None,
        stack: str = "",
        allowed_paths: list[str] | tuple[str, ...] | set[str] | None = None,
        planned_paths: list[str] | tuple[str, ...] | set[str] | None = None,
        enforce_antistub: bool = True,
        verify_on_stop: bool | None = None,
    ) -> dict:
        """Whole-project agentic codegen on an OpenRouter model: the model writes the
        app itself via tool-calls (write_file/read_file/list_files/finish) with full
        context — coherent like bolt/v0/Aider, vs skyn3t's weak per-file gen. Files
        are confined to ``workdir``. Returns {ok, backend, error}. Never raises."""
        import json as _json
        import time as _t
        root = Path(workdir).resolve()

        def _safe(path: str) -> Path | None:
            try:
                p = (root / str(path)).resolve()
            except Exception:  # noqa: BLE001
                return None
            return p if (p == root or str(p).startswith(str(root) + os.sep)) else None

        allowed: set[str] | None = None
        if allowed_paths is not None:
            allowed = set()
            for raw in allowed_paths:
                target = _safe(str(raw))
                if target is not None and target != root:
                    allowed.add(os.path.normcase(str(target)))

        def _owned(path: Path) -> bool:
            return allowed is None or os.path.normcase(str(path)) in allowed

        planned: set[str] = set()
        if planned_paths is not None:
            for raw in planned_paths:
                target = _safe(str(raw))
                if target is not None and target != root:
                    planned.add(os.path.normcase(str(target)))

        def _planned_missing_count() -> int:
            return sum(1 for raw in planned if not Path(raw).is_file())

        def _run_tool(name: str, args: dict) -> tuple[str, int]:
            """Run one tool and return ``(message, changed_file_count)``.

            The progress watchdog advances only for new content. Repeating a
            successful no-op write is activity, but it is not build progress.
            """
            if name == "write_file":
                rel = str(args.get("path", "")).strip()
                p = _safe(rel)
                if not rel or not p or p == root:
                    return "ERROR: path escapes the project", 0
                if not _owned(p):
                    return f"ERROR: path is outside this slice's owned files: {rel}", 0
                if p.suffix.lower() in _AGENTIC_BINARY_WRITE_EXTS:
                    return (
                        f"ERROR: {rel} is binary media and cannot be written by a text "
                        "tool; preserve/use generated assets already in the workspace",
                        0,
                    )
                try:
                    body = str(args.get("content", ""))
                    if p.is_file() and p.read_text(
                        encoding="utf-8", errors="replace"
                    ) == body:
                        return f"OK unchanged {rel} ({len(body)} bytes)", 0
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(body, encoding="utf-8")
                    return f"OK wrote {rel} ({len(body)} bytes)", 1
                except OSError as e:
                    return f"ERROR: {e}", 0
            if name == "write_files":
                batch = args.get("files")
                if not isinstance(batch, list) or not 1 <= len(batch) <= 8:
                    return "ERROR: files must contain 1..8 file objects", 0
                checked: list[tuple[Path, str, str]] = []
                targets: set[str] = set()
                for item in batch:
                    if not isinstance(item, dict):
                        return "ERROR: every files item must be an object", 0
                    rel = str(item.get("path", "")).strip()
                    p = _safe(rel)
                    if not rel or not p or p == root:
                        return f"ERROR: path escapes the project: {rel}", 0
                    target_key = os.path.normcase(str(p))
                    if target_key in targets:
                        return f"ERROR: duplicate batch path: {rel}", 0
                    targets.add(target_key)
                    checked.append((p, rel, str(item.get("content", ""))))
                rejected = [rel for p, rel, _body in checked if not _owned(p)]
                checked = [item for item in checked if _owned(item[0])]
                binary_rejected = [
                    rel
                    for p, rel, _body in checked
                    if p.suffix.lower() in _AGENTIC_BINARY_WRITE_EXTS
                ]
                checked = [
                    item
                    for item in checked
                    if item[0].suffix.lower() not in _AGENTIC_BINARY_WRITE_EXTS
                ]
                if not checked:
                    if binary_rejected:
                        return (
                            "ERROR: binary media cannot be written by a text tool; "
                            "preserve/use generated assets already in the workspace: "
                            + ", ".join(binary_rejected[:8]),
                            0,
                        )
                    return (
                        "ERROR: every batch path is outside this slice's owned files: "
                        + ", ".join(rejected[:8]),
                        0,
                    )
                try:
                    changed: list[tuple[Path, str, str]] = []
                    for p, rel, body in checked:
                        if p.is_file() and p.read_text(
                            encoding="utf-8", errors="replace"
                        ) == body:
                            continue
                        changed.append((p, rel, body))
                    for p, _rel, body in changed:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(body, encoding="utf-8")
                    total = sum(len(body) for _p, _rel, body in changed)
                    if not changed:
                        return f"OK unchanged batch ({len(checked)} files)", 0
                    message = (
                        f"OK wrote batch ({len(changed)} changed of {len(checked)} files, "
                        f"{total} bytes)"
                    )
                    if rejected:
                        message += "; rejected out-of-scope: " + ", ".join(rejected[:8])
                    if binary_rejected:
                        message += "; rejected binary media: " + ", ".join(
                            binary_rejected[:8]
                        )
                    return message, len(changed)
                except OSError as e:
                    return f"ERROR: {e}", 0
            if name == "read_file":
                p = _safe(args.get("path", ""))
                if not p or not p.is_file():
                    return "ERROR: not found", 0
                try:
                    return p.read_text(encoding="utf-8", errors="replace")[:8000], 0
                except OSError as e:
                    return f"ERROR: {e}", 0
            if name == "list_files":
                out: list[str] = []
                for f in root.rglob("*"):
                    if f.is_file() and not ({"node_modules", ".git", ".next", "dist"} & set(f.parts)):
                        out.append(str(f.relative_to(root)))
                        if len(out) >= 200:
                            break
                return "\n".join(sorted(out)) or "(empty)", 0
            return "ERROR: unknown tool", 0

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _agentic_system_for(stack)},
            {"role": "user", "content": prompt},
        ]
        headers = {"Authorization": f"Bearer {openrouter_key(self.settings)}",
                   "HTTP-Referer": "https://github.com/skyn3t", "X-Title": "SkyN3t"}
        max_turns = max(4, int(getattr(self.settings, "openrouter_agentic_max_turns", 60)))
        budget = max(
            0.01,
            float(timeout or int(getattr(self.settings, "agentic_build_timeout", 1800))),
        )
        wrote, finished, start = 0, False, _t.monotonic()
        # ``budget`` is a NO-WRITE-PROGRESS window, not a total build ceiling.
        # Every successful write batch extends it, so a productive full-app build
        # can run past the nominal profile duration without being truncated.
        progress_deadline = start + budget
        budget_error = ""
        loop_error = ""
        timed_out = False
        attempted_model = model
        fallback_model = ""
        turn_timeouts = 0
        stall_reason = ""
        idle_timeout = float(getattr(self.settings, "agentic_idle_timeout", 0) or 0)
        stub_nudges, _MAX_STUB_NUDGES = 0, 2
        verify_on_stop_enabled = (
            bool(getattr(self.settings, "agentic_verify_on_stop", True))
            if verify_on_stop is None
            else bool(verify_on_stop)
        )
        verify_denials, _MAX_VERIFY_DENIALS = 0, 2
        doom_recent: list[tuple[str, str]] = []  # last 3 (tool, raw-args) signatures
        doom_warned = False
        changed_path_window: list[str] = []
        churn_warned_paths: set[str] = set()
        auxiliary_churn_paths: set[str] = set()
        auxiliary_churn_warned = False
        planned_missing_count = _planned_missing_count()
        plan_complete_nudged = False
        auto_converged = False
        # The anti-stub nudge below is web-marketing-specific; only let it fire for those
        # stacks (an empty/unknown stack keeps the react_vite-default behaviour). Game,
        # mobile, desktop, API and CLI builds must never be nudged toward a web UI.
        stub_nudge_applies = enforce_antistub and (
            (not stack) or (stack in _ANTISTUB_NUDGE_STACKS)
        )

        def _looks_stub() -> bool:
            return _agentic_project_looks_stub(root, stack)
        turn = 0
        no_progress_turns = 0
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                while no_progress_turns < max_turns:
                    remaining = progress_deadline - _t.monotonic()
                    if remaining <= 0:
                        log.warning("llm.or_agentic_timeout", turns=turn, wrote=wrote)
                        timed_out = True
                        loop_error = f"agentic made no file-write progress for {budget}s"
                        break
                    # Context editing: send a shrunk COPY when the history blows the
                    # byte budget (stub OLD read-dumps, keep the last K) — the
                    # canonical `messages` keeps growing untouched.
                    sent = self._edit_context(messages)

                    async def _do(m, _sent=sent, _turn=turn, _wrote=wrote):
                        async def _post():
                            body = {"model": m, "messages": _sent, "tools": _AGENTIC_TOOLS,
                                    "tool_choice": "auto", "max_tokens": 16384}
                            resp = await client.post(OPENROUTER_URL, json=body, headers=headers)
                            resp.raise_for_status()
                            return resp.json(), m

                        if idle_timeout > 0:
                            try:
                                return await asyncio.wait_for(_post(), timeout=idle_timeout)
                            except TimeoutError as exc:
                                nonlocal turn_timeouts, stall_reason
                                turn_timeouts += 1
                                stall_reason = (
                                    f"OpenRouter agentic turn stalled after {idle_timeout:g}s"
                                )
                                log.warning(
                                    "llm.or_agentic_stalled",
                                    model=m,
                                    turn=_turn,
                                    idle_s=idle_timeout,
                                    wrote=_wrote,
                                )
                                raise _AgenticTurnStall(stall_reason) from exc
                        return await _post()

                    # Retry + model failover: a mid-build retired model fails over to
                    # a live one and PINS it for the rest of the session (`model`),
                    # instead of the whole agentic build dying.
                    try:
                        data, model = await asyncio.wait_for(
                            self._resilient_call(model, Tier.CHEAP, _do),
                            timeout=max(0.01, remaining),
                        )
                    except TimeoutError:
                        timed_out = True
                        loop_error = f"agentic request made no file-write progress for {budget}s"
                        log.warning("llm.or_agentic_timeout", turns=turn, wrote=wrote)
                        break
                    if model != attempted_model:
                        fallback_model = model
                    msg = data["choices"][0]["message"]
                    try:
                        self._record_openrouter_agentic_usage(model, data, sent, msg)
                    except BudgetExceeded as exc:
                        budget_error = str(exc)
                        log.warning("llm.or_agentic_budget_exceeded", error=budget_error)
                        break
                    messages.append({"role": "assistant", "content": msg.get("content") or "",
                                     "tool_calls": msg.get("tool_calls") or []})
                    tcs = msg.get("tool_calls") or []
                    wrote_before = wrote
                    if tcs:
                        for tc in tcs:
                            fn = tc.get("function") or {}
                            name = fn.get("name", "")
                            try:
                                args = _json.loads(fn.get("arguments") or "{}")
                            except ValueError:
                                args = {}
                            if name == "finish":
                                finished, result = True, "OK"
                            else:
                                result, changed_files = _run_tool(name, args)
                                if changed_files:
                                    wrote += changed_files
                                    changed_paths: list[str] = []
                                    if name == "write_file":
                                        changed_paths = [str(args.get("path") or "")]
                                    elif name == "write_files":
                                        changed_paths = [
                                            str(item.get("path") or "")
                                            for item in (args.get("files") or [])
                                            if isinstance(item, dict)
                                        ]
                                    changed_path_window.extend(
                                        path.replace("\\", "/").strip().lower()
                                        for path in changed_paths
                                        if path.strip()
                                    )
                                    changed_path_window = changed_path_window[
                                        -_PATH_CHURN_WINDOW:
                                    ]
                                    current_missing = _planned_missing_count()
                                    if current_missing < planned_missing_count:
                                        # A real architecture file landed: this is
                                        # productive exploration, not sustained churn.
                                        auxiliary_churn_paths.clear()
                                        auxiliary_churn_warned = False
                                    planned_missing_count = current_missing
                                    for changed_path in changed_paths:
                                        target = _safe(changed_path)
                                        target_key = (
                                            os.path.normcase(str(target)) if target is not None else ""
                                        )
                                        normalized = changed_path.replace("\\", "/").strip().lower()
                                        if (
                                            target_key
                                            and target_key not in planned
                                            and _agentic_auxiliary_family(normalized)
                                        ):
                                            auxiliary_churn_paths.add(normalized)
                                    # Live per-file progress so a tailed build log shows
                                    # codegen advancing (the agentic phase was silent).
                                    log.info("llm.agentic_wrote",
                                             file=str(args.get("path", ""))[:80], n=wrote)
                                doom_recent = (doom_recent
                                               + [(name, fn.get("arguments") or "")])[-3:]
                            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                             "content": result})
                    else:
                        finished = True  # final text answer -> done
                    if wrote > wrote_before:
                        progress_deadline = _t.monotonic() + budget
                        no_progress_turns = 0
                    else:
                        no_progress_turns += 1
                    turn += 1
                    if (not finished and len(doom_recent) == 3
                            and len(set(doom_recent)) == 1):
                        # Item 49: the model repeated one identical call 3x — stuck.
                        if doom_warned:
                            log.warning("llm.or_agentic_doom_abort", wrote=wrote, turns=turn)
                            loop_error = (
                                "agentic aborted after repeating an identical tool call "
                                "despite corrective feedback"
                            )
                            break
                        doom_warned, doom_recent = True, []
                        messages.append({"role": "user", "content": _DOOM_LOOP_NUDGE})
                        log.info("llm.or_agentic_doom_nudge", wrote=wrote)
                        continue
                    if not finished and len(changed_path_window) == _PATH_CHURN_WINDOW:
                        dominant = max(
                            set(changed_path_window), key=changed_path_window.count
                        )
                        dominant_count = changed_path_window.count(dominant)
                        if dominant_count >= _PATH_CHURN_THRESHOLD:
                            changed_path_window = []
                            if dominant in churn_warned_paths:
                                log.warning(
                                    "llm.or_agentic_path_churn_abort",
                                    path=dominant,
                                    count=dominant_count,
                                    wrote=wrote,
                                    turns=turn,
                                )
                                loop_error = (
                                    "agentic aborted after repeated changed rewrites of "
                                    f"{dominant} despite corrective feedback"
                                )
                                break
                            churn_warned_paths.add(dominant)
                            messages.append(
                                {
                                    "role": "user",
                                    "content": _PATH_CHURN_NUDGE.format(
                                        path=dominant, count=dominant_count
                                    ),
                                }
                            )
                            log.info(
                                "llm.or_agentic_path_churn_nudge",
                                path=dominant,
                                count=dominant_count,
                                wrote=wrote,
                            )
                            continue
                    if (
                        not finished
                        and planned_missing_count > 0
                        and len(auxiliary_churn_paths) >= _AUXILIARY_CHURN_THRESHOLD
                    ):
                        churn_count = len(auxiliary_churn_paths)
                        examples = sorted(auxiliary_churn_paths)[:4]
                        auxiliary_churn_paths.clear()
                        if auxiliary_churn_warned:
                            log.warning(
                                "llm.or_agentic_auxiliary_churn_abort",
                                count=churn_count,
                                examples=examples,
                                missing_planned=planned_missing_count,
                                wrote=wrote,
                                turns=turn,
                            )
                            loop_error = (
                                "agentic aborted after continued unplanned build-helper "
                                "path churn despite corrective feedback"
                            )
                            break
                        auxiliary_churn_warned = True
                        messages.append({"role": "user", "content": _AUXILIARY_CHURN_NUDGE})
                        log.info(
                            "llm.or_agentic_auxiliary_churn_nudge",
                            count=churn_count,
                            examples=examples,
                            missing_planned=planned_missing_count,
                            wrote=wrote,
                        )
                        continue
                    if not finished and planned and planned_missing_count == 0:
                        # Planned scope is a semantic completion contract, not a
                        # size/turn/token cap. Give the model one final integration
                        # pass, then accept a clean tree even if it forgets to call
                        # finish. This stops productive full-app sessions from
                        # drifting into endless rewrites after every approved file
                        # already exists.
                        if not plan_complete_nudged:
                            plan_complete_nudged = True
                            messages.append({"role": "user", "content": _PLAN_COMPLETE_NUDGE})
                            log.info(
                                "llm.or_agentic_plan_complete_nudge",
                                planned=len(planned),
                                wrote=wrote,
                            )
                            continue
                        if (
                            stub_nudge_applies
                            and stub_nudges < _MAX_STUB_NUDGES
                            and _looks_stub()
                        ):
                            stub_nudges += 1
                            messages.append({"role": "user", "content": _ANTISTUB_NUDGE})
                            log.info(
                                "llm.or_agentic_antistub",
                                nudge=stub_nudges,
                                wrote=wrote,
                            )
                            continue
                        if verify_on_stop_enabled and verify_denials < _MAX_VERIFY_DENIALS:
                            problems = _agentic_verify_problems(root)
                            if problems:
                                verify_denials += 1
                                messages.append({"role": "user", "content": (
                                    "VERIFY-ON-STOP: the project has defects that WILL "
                                    "fail the build. Fix ONLY these, then call finish "
                                    "again:\n- " + "\n- ".join(problems))})
                                log.info(
                                    "llm.or_agentic_verify_denied",
                                    denial=verify_denials,
                                    problems=len(problems),
                                )
                                continue
                        finished = True
                        auto_converged = True
                        log.info(
                            "llm.or_agentic_plan_auto_converged",
                            planned=len(planned),
                            wrote=wrote,
                        )
                        break
                    if finished:
                        # Refuse a thin result: push the model to build the real UI instead
                        # of stopping at data/config scaffolding (cheap models do this).
                        if stub_nudge_applies and stub_nudges < _MAX_STUB_NUDGES and _looks_stub():
                            stub_nudges += 1
                            finished = False
                            messages.append({"role": "user", "content": _ANTISTUB_NUDGE})
                            log.info("llm.or_agentic_antistub", nudge=stub_nudges, wrote=wrote)
                            continue
                        # Item 19: verify-on-stop. Deny the finish with the REAL
                        # defect list while codegen context is warm; degrade open
                        # after 2 denials (the pipeline fix-loop takes over).
                        if verify_on_stop_enabled and verify_denials < _MAX_VERIFY_DENIALS:
                            problems = _agentic_verify_problems(root)
                            if problems:
                                verify_denials += 1
                                finished = False
                                messages.append({"role": "user", "content": (
                                    "VERIFY-ON-STOP: the project has defects that WILL "
                                    "fail the build. Fix ONLY these, then call finish "
                                    "again:\n- " + "\n- ".join(problems))})
                                log.info("llm.or_agentic_verify_denied",
                                         denial=verify_denials, problems=len(problems))
                                continue
                        break
        except Exception as exc:  # noqa: BLE001 - never crash the build
            loop_error = f"agentic loop failed: {str(exc)[:160]}"
            log.warning("llm.or_agentic_failed", error=str(exc)[:200], wrote=wrote)
        log.info("llm.or_agentic_done", model=model, files_written=wrote, finished=finished)
        self._record_completion("backend", "codegen", model)
        if not finished and not loop_error and not budget_error:
            loop_error = (
                f"agentic stopped after {max_turns} consecutive turns without a file write"
            )
        error = budget_error or loop_error or ("" if wrote > 0 else "agentic loop wrote no files")
        return {"ok": finished and wrote > 0 and not error, "completed": finished,
                "timed_out": timed_out, "files_written": wrote, "backend": "openrouter",
                "model": model, "attempted_model": attempted_model,
                "fallback_model": fallback_model, "stalled": bool(turn_timeouts),
                "stall_reason": stall_reason, "turn_timeouts": turn_timeouts,
                "auto_converged": auto_converged,
                "error": error}

    @property
    def supports_agentic(self) -> bool:
        """Whole-project codegen: CLI backends are coding agents; OpenRouter gets the
        agentic tool-loop when ``openrouter_agentic`` is on (cheap models, full app)."""
        b = self.backend
        return b.endswith("_cli") or (
            b == "openrouter" and bool(getattr(self.settings, "openrouter_agentic", True)))

    async def agentic_build(
        self,
        prompt: str,
        workdir: str,
        timeout: int | None = None,
        model: str | None = None,
        provider: str | None = None,
        stack: str = "",
        allowed_paths: list[str] | tuple[str, ...] | set[str] | None = None,
        planned_paths: list[str] | tuple[str, ...] | set[str] | None = None,
        enforce_antistub: bool = True,
        verify_on_stop: bool | None = None,
    ) -> dict:
        """Run a local coding-agent CLI that writes files directly into workdir.

        This is the RIGHT way to use claude/kimi/copilot for codegen: one
        agentic session that authors a coherent multi-file app, instead of N
        slow per-file completion calls that spin up an agent each and time out.
        ``model`` optionally pins the CLI to a specific model (used by parallel
        code-slicing to route cheap/strong tiers per slice). ``provider`` forces a
        specific CLI (e.g. "claude") regardless of the global backend — this is how
        codegen-only CLI routing works: the global backend stays cheap (OpenRouter)
        while ONLY codegen runs on the claude CLI. Returns {ok, backend, error}.
        """
        backend = self.backend
        try:
            self.budget.check()
        except BudgetExceeded as exc:
            return {"ok": False, "backend": backend, "error": str(exc)}
        provider = (provider or (backend[:-4] if backend.endswith("_cli") else "")).lower()
        if provider and not model:
            model = str(getattr(self.settings, "codegen_cli_model", "") or "").strip() or None
        if not provider:
            # No CLI agent: OpenRouter models get the agentic tool-loop (cheap,
            # whole-project codegen) instead of the weak per-file path.
            if backend == "openrouter" and bool(getattr(self.settings, "openrouter_agentic", True)):
                m = self._resolve_pinned_model(
                    tier=Tier.BACKEND,
                    model_override=model,
                    setting_names=("openrouter_codegen_model", "preferred_model"),
                    task_type="codegen",
                )
                return await self._openrouter_agentic(
                    prompt,
                    workdir,
                    m,
                    timeout=timeout,
                    stack=stack,
                    allowed_paths=allowed_paths,
                    planned_paths=planned_paths,
                    enforce_antistub=enforce_antistub,
                    verify_on_stop=verify_on_stop,
                )
            return {"ok": False, "backend": backend, "error": "agentic unsupported"}
        if not self._cli_available(provider):
            return {"ok": False, "backend": backend, "error": "agentic unsupported"}
        # acceptEdits lets the headless agent write files without prompting.
        # _no_mcp_args keeps the agent from loading the host's ambient MCP fleet.
        nm = _no_mcp_args(self.settings, provider)
        # Stream the agent's NDJSON event log (claude/kimi) so we can detect the
        # terminal `result` event (an accurate success signal — claude -p can
        # exit 0 on a reported error) and watch for a stalled session via an idle
        # guard instead of always burning the full ceiling. copilot has no such
        # mode, so it keeps the blocking path.
        stream = provider in ("claude", "kimi")
        # claude takes --verbose with stream-json; kimi's stream-json does not.
        stream_args = (["--output-format", "stream-json", "--verbose"] if provider == "claude"
                       else ["--output-format", "stream-json"] if provider == "kimi" else [])
        # Optional per-call model pin (claude/kimi accept --model); ignored when
        # no model is given so the CLI's default applies (today's behaviour).
        model_args = ["--model", model] if (model and provider in ("claude", "kimi")) else []
        # --setting-sources project isolates codegen from the host's Claude Code
        # config (output-style plugins, hooks) so they can't corrupt the build.
        argv = {
            "claude": ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
                       "--setting-sources", "project", *model_args, *stream_args, *nm],
            # kimi-code's -p is non-interactive (rejects -y/--auto/--permission-mode)
            # and has no --strict-mcp-config. Needs `kimi login` (OAuth) to run.
            "kimi": ["kimi", "-p", prompt, *model_args, *stream_args],
            "copilot": ["copilot", "-p", prompt, *nm],
        }.get(provider, [provider, "-p", prompt])
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group -> killable as a tree
                # A single stream-json event line (a big tool result, or the final
                # `result` event carrying the whole output) routinely exceeds the
                # 64KB asyncio StreamReader default, which makes readline() raise
                # "Separator is found, but chunk is longer than limit" and degrades
                # the whole build. Give it real headroom (grows on demand, not
                # preallocated).
                limit=_AGENTIC_STREAM_LIMIT,
            )
            # For streaming CLIs this is an INACTIVITY fallback, not a total
            # duration ceiling. Every stream event proves the agent is alive and
            # resets _consume_agentic_stream's idle wait, so productive full-app
            # builds may continue until their terminal result event.
            agentic_timeout = (
                timeout
                or int(getattr(self.settings, "agentic_build_timeout", 0))
                or (self.settings.cli_llm_timeout * 3)
            )
            if stream:
                idle_timeout = (
                    int(getattr(self.settings, "agentic_idle_timeout", 0))
                    or agentic_timeout
                )
                ok = await self._consume_agentic_stream(proc, provider, idle_timeout)
            else:
                out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=agentic_timeout,
                )
                ok = proc.returncode == 0
                if not ok:
                    log.warning("llm.agentic_nonzero", provider=provider,
                                err=(err or b"").decode("utf-8", "replace")[:160])
        except asyncio.CancelledError:
            # Outer stage timeout / build cancellation: kill the whole agent tree
            # before unwinding so it can't keep building orphaned for an hour.
            await self._terminate(proc)
            raise
        except Exception as exc:  # noqa: BLE001 - never raise into the build
            await self._terminate(proc)
            log.warning("llm.agentic_failed", provider=provider, error=str(exc)[:160])
            return {"ok": False, "backend": backend, "error": str(exc)[:160]}
        effective_model = model or f"{provider}-cli"
        self._record_completion(f"{provider}_cli", "codegen", effective_model)
        return {
            "ok": ok,
            "completed": ok,
            "backend": f"{provider}_cli",
            "model": effective_model,
            "error": "" if ok else "agentic CLI ended without a successful result",
        }

    async def _consume_agentic_stream(self, proc, provider: str, idle_timeout: int) -> bool:
        """Drive a stream-json agentic CLI to completion and report success.

        Reads the NDJSON event stream to EOF (the agent closes stdout when it
        exits), capturing the terminal ``result`` event's error flag for an
        accurate success signal. When ``idle_timeout`` > 0, a gap of that many
        seconds with NO stream activity means the agent has stalled — we kill the
        whole tree and report failure. Each event resets that wait, so there is no
        total-duration ceiling on productive work. stderr is drained concurrently
        so a chatty agent cannot deadlock on a full pipe. Returns ``ok``.
        """
        saw_result = False
        result_is_error = False
        events = 0

        # Drain stderr concurrently (a full stderr pipe would block the agent);
        # keep only a short tail for diagnostics.
        err_tail: list[str] = []

        async def _drain_err() -> None:
            try:
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        return
                    err_tail.append(line.decode("utf-8", "replace"))
                    if len(err_tail) > 20:
                        del err_tail[:-20]
            except Exception:  # noqa: BLE001 - draining is best-effort
                return

        err_task = asyncio.create_task(_drain_err())
        try:
            while True:
                try:
                    if idle_timeout > 0:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=idle_timeout
                        )
                    else:
                        line = await proc.stdout.readline()
                except TimeoutError:
                    log.warning("llm.agentic_stalled", provider=provider, idle_s=idle_timeout)
                    await self._terminate(proc)
                    return False
                except ValueError:
                    # Defence in depth: a single line past even the 64MB buffer
                    # makes readline() raise. Kill the tree (else the agent can
                    # block on a full stdout pipe we've stopped draining) and fall
                    # back to the returncode below.
                    log.warning("llm.agentic_stream_overrun", provider=provider)
                    await self._terminate(proc)
                    break
                if not line:
                    break  # EOF — the agent exited
                events += 1
                try:
                    evt = json.loads(line)
                except (ValueError, TypeError):
                    continue  # stray non-JSON log line — ignore
                if isinstance(evt, dict) and evt.get("type") == "result":
                    saw_result = True
                    result_is_error = bool(evt.get("is_error"))
        finally:
            err_task.cancel()
            try:
                await err_task
            except asyncio.CancelledError:
                pass

        # stdout EOF means the agent is exiting; reap it (bounded) so returncode
        # is set for the fallback success check.
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:  # noqa: BLE001 - never hang on reap
            pass

        ok = (not result_is_error) if saw_result else (proc.returncode == 0)
        if not ok:
            log.warning("llm.agentic_nonzero", provider=provider,
                        events=events, err="".join(err_tail)[-160:])
        return ok

    @staticmethod
    async def _terminate(proc) -> None:
        """Kill + reap a subprocess TREE so it is never orphaned.

        The CLI agents (``claude -p`` etc.) spawn children; killing only the
        parent leaves those running. We kill the whole process group (the
        subprocess was started with ``start_new_session=True``) and reap it under
        a bounded wait so a stuck process can't re-hang us here.
        """
        if proc is None or proc.returncode is not None:
            return
        try:
            if sys.platform == "win32":
                try:
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(proc.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    taskkill_returncode = await asyncio.wait_for(killer.wait(), timeout=5)
                    if taskkill_returncode != 0 and proc.returncode is None:
                        proc.kill()
                except (TimeoutError, FileNotFoundError, OSError):
                    proc.kill()
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()  # fall back to single-process kill
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError):
            pass
        except Exception:  # noqa: BLE001
            pass

    # ---- backends --------------------------------------------------------
    async def _openrouter(self, model, prompt, system, max_tokens, json_mode,
                          images: list[str] | None = None,
                          tier: Tier = Tier.CHEAP) -> LLMResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if images:
            # Multimodal user message: a text part + one image_url part each. This
            # is the OpenAI/OpenRouter shape studio/visual_check already uses.
            content: list[dict] = [{"type": "text", "text": prompt}]
            for img in images:
                try:
                    content.append({"type": "image_url",
                                    "image_url": {"url": _to_data_url(img)}})
                except OSError as exc:  # unreadable path -> skip that image, don't crash
                    log.warning("llm.image_unreadable", error=str(exc)[:160])
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": prompt})
        headers = {
            "Authorization": f"Bearer {openrouter_key(self.settings)}",
            "HTTP-Referer": "https://github.com/skyn3t",
            "X-Title": "SkyN3t",
        }

        async def _do(m):
            body = {"model": m, "messages": messages, "max_tokens": max_tokens}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(OPENROUTER_URL, json=body, headers=headers)
                # HTTP errors (429/5xx retryable, 404/invalid-model -> failover)
                # raise here; the resilience layer retries or fails over, and a
                # persistent failure re-raises to the orchestrator's own retries.
                resp.raise_for_status()
            return resp, m

        # Transient retry + ordered model failover around the call — survives a
        # retired default model (the deepseek-*:free 404 incident) instead of
        # degrading the whole build to the offline stub.
        resp, model = await self._resilient_call(model, tier, _do)
        # A 200 with a malformed/empty body must degrade, not crash the build —
        # this includes a body that isn't valid JSON (truncation, gateway HTML),
        # so resp.json() is inside the guard too (it raises ValueError/JSONDecodeError).
        try:
            data = resp.json()
            # content can be JSON null (tool-only/empty completions) — coalesce to
            # "" so the contract "text is always a str" holds (matches the CLI path)
            # and downstream len()/json parsing never hits None.
            text = data["choices"][0]["message"]["content"] or ""
            # OpenRouter models wrap structured output in a ```json fence; the CLI
            # path strips it (see _cli_complete) but this one didn't — so the
            # reviewer/intent/critic json.loads silently failed and the whole
            # scoring brain went dark on the OpenRouter backend. Strip it here too.
            if json_mode:
                text = _strip_code_fences(text)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("llm.openrouter_malformed", error=str(exc)[:160])
            return self._stub(model, prompt, system, json_mode)
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        # OpenRouter sometimes omits `usage` on a 200. Defaulting to 0 made PAID
        # models report $0 — corrupting cost tracking + budget caps. Estimate
        # tokens from text length (~4 chars/token) so a paid call is never free.
        if not _is_free_model_id(model) and pt == 0 and ct == 0:
            pt = max(1, (len(system or "") + len(prompt or "")) // 4)
            ct = max(1, len(text or "") // 4)
            log.warning("llm.openrouter_usage_missing", model=model, est_tokens=pt + ct)
        # :free models cost $0; otherwise rough estimate.
        cost = 0.0 if _is_free_model_id(model) else (pt + ct) / 1_000_000 * 0.5
        return LLMResult(text=text, model=model, backend="openrouter",
                         prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)

    def _stub(self, model, prompt, system, json_mode) -> LLMResult:
        """Deterministic offline response. Good enough to exercise the pipeline."""
        if json_mode:
            text = json.dumps({"stub": True, "echo": prompt[:200]})
        else:
            text = (
                f"[stub:{model}] Offline response. "
                f"Set SKYN3T_OPENROUTER_API_KEY for real generation.\n"
                f"Prompt summary: {prompt[:160]}"
            )
        approx = max(1, len(prompt) // 4)
        return LLMResult(text=text, model=model, backend="stub",
                         prompt_tokens=approx, completion_tokens=len(text) // 4, cost_usd=0.0)
