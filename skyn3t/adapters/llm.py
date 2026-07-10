"""Unified LLM client with cost-aware routing and an offline stub backend.

One entry point — :meth:`LLMClient.complete` — resolves a tier to a model via
the :class:`ModelRouter`, then dispatches to a backend:

* ``openrouter`` — real HTTP (primary) when ``OPENROUTER_API_KEY`` is set.
* ``<provider>_cli`` — shells out to a locally-installed CLI (``claude``,
  ``codex``, ``kimi``, ``copilot``) in headless print mode. Real generation
  with **no SkyN3t API key** — handy when you already have a coding-agent CLI
  signed in.
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
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import structlog

from skyn3t.agents._common import confined_path
from skyn3t.atomic_io import atomic_write_text
from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.model_router import ModelRouter, Tier
from skyn3t.core.model_router import is_free_model_id as router_is_free_model_id
from skyn3t.persisted_write import (
    PERSISTED_WRITE_RECEIPT_KEY as _PERSISTED_WRITE_RECEIPT_KEY,
)
from skyn3t.persisted_write import (
    PERSISTED_WRITE_RECEIPT_MAX_BYTES as _PERSISTED_WRITE_RECEIPT_MAX_BYTES,
)
from skyn3t.persisted_write import (
    is_persisted_write_receipt as _is_persisted_write_receipt,
)
from skyn3t.persisted_write import (
    is_persisted_write_receipt_body as _is_persisted_write_receipt_body,
)

# Per-asyncio-task LLM route capture. The LLMClient is SHARED across agents, but
# each agent's run() is its own task; task-local vars isolate "the completions
# THIS run produced" without a global lock (a per-AGENT lock can't — the client
# is shared, so two agents would race on shared instance attrs). The only reader
# (core/agent.py._run_locked) runs in the SAME task as the completions it reads.
_LAST_MODEL: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_last_model", default=None)
_LAST_ROUTE: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_last_route", default=None)
_ROUTES: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_routes", default=None)
_ROUTING_PROFILE: contextvars.ContextVar = contextvars.ContextVar(
    "skyn3t_llm_routing_profile", default="balanced"
)
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
    "codex": [
        "codex", "exec", "--ephemeral", "--sandbox", "read-only",
        "--color", "never", "--skip-git-repo-check",
    ],
    "kimi": [
        "kimi", "--print", "--output-format", "text",
        "--final-message-only", "--prompt",
    ],
    "copilot": ["copilot", "-p"],
}
KNOWN_CLI_PROVIDERS = ("codex", "claude", "copilot", "kimi")
SUPPORTED_LLM_BACKENDS = (
    "auto", "stub", "openrouter",
    *(f"{provider}_cli" for provider in KNOWN_CLI_PROVIDERS),
)
_KNOWN_CLI_PROVIDERS = KNOWN_CLI_PROVIDERS
_CLI_DISPLAY_NAMES = {
    "codex": "Codex CLI",
    "claude": "Claude Code CLI",
    "copilot": "GitHub Copilot CLI",
    "kimi": "Kimi Code CLI",
}
_CLI_VERSION_ARGS = {provider: ("--version",) for provider in KNOWN_CLI_PROVIDERS}
_CLI_STATUS_TTL_SECONDS = 5.0


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
    "Work efficiently: use write_files to create independent small or medium files "
    "in one tool call, batching every file that fits naturally in the response; reserve "
    "write_file for a large core file that needs its own call. "
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


class RoutingLockError(ValueError):
    """An explicitly selected LLM route cannot be honored as configured."""


def explicit_routing_lock_error(
    settings: Any,
    *,
    cli_available: Callable[[str], bool] | None = None,
) -> str:
    """Return the blocking reason for an explicit route, or ``""``.

    ``auto`` deliberately remains degrade-open: with no provider available it
    may use the offline stub. Explicit selections are locks, however, and must
    never be silently reinterpreted as a different provider. The CLI probe is
    injectable so callers and tests can validate policy without running a
    model command.
    """
    requested = str(getattr(settings, "llm_backend", "auto") or "auto").strip().lower()
    if requested not in SUPPORTED_LLM_BACKENDS:
        return f"Unsupported LLM backend {requested!r}."
    if requested == "openrouter" and not openrouter_key(settings):
        return (
            "OpenRouter was explicitly selected but "
            "SKYN3T_OPENROUTER_API_KEY is not configured."
        )

    probe = cli_available

    def provider_available(provider: str) -> bool:
        nonlocal probe
        if probe is None:
            # Availability is a class-level PATH probe; constructing a complete
            # LLMClient here would also build a model router and require unrelated
            # runtime paths. Accept both the production classmethod and tests that
            # replace it with an instance-style function.
            def default_probe(name: str) -> bool:
                raw_probe: Any = LLMClient._cli_available
                try:
                    return bool(raw_probe(name))
                except TypeError:
                    return bool(raw_probe(LLMClient, name))

            probe = default_probe
        return probe(provider)

    if requested.endswith("_cli"):
        provider = requested[:-4]
        if not provider_available(provider):
            return f"{provider} CLI was explicitly selected but is not available on PATH."

    codegen_provider = str(
        getattr(settings, "codegen_cli_provider", "") or ""
    ).strip().lower()
    if codegen_provider:
        if codegen_provider not in KNOWN_CLI_PROVIDERS:
            return f"Unsupported codegen CLI provider {codegen_provider!r}."
        if not provider_available(codegen_provider):
            return (
                f"codegen_cli_provider={codegen_provider!r} is explicitly selected "
                "but unavailable; OpenRouter fallback is disabled."
            )
    return ""


def enforce_explicit_routing_lock(
    settings: Any,
    *,
    cli_available: Callable[[str], bool] | None = None,
) -> None:
    """Raise before work begins when an explicit provider route is unusable."""
    reason = explicit_routing_lock_error(settings, cli_available=cli_available)
    if reason:
        raise RoutingLockError(reason)

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
            "Create or overwrite a batch of project files. Prefer this for "
            "independent config, style, test, documentation, and support files."
        ),
        "parameters": {"type": "object", "properties": {
            "files": {"type": "array", "minItems": 1,
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

# Historical assistant tool calls stay in the conversation sent on every later
# agentic turn. Successful write bodies are compacted to receipts below; those
# receipts are context records, never executable file content. Keep an explicit
# versioned sentinel for current/future formats and recognize the original
# marker-only format produced before the sentinel existed.
# Flags that make each headless CLI ignore the host's ambient MCP servers, so a
# skyn3t build never boots the user's whole ~/.claude / ~/.copilot MCP fleet
# (Aruba, context7, playwright, ...) on every codegen call. Claude supports an
# explicit strict/empty MCP configuration. Kimi 1.41 exposes additive MCP config
# flags but no documented disable-all flag, so do not pass an incompatible flag
# or claim isolation for it. Copilot disables its built-in GitHub MCP.
_CLI_NO_MCP_ARGS: dict[str, list[str]] = {
    "claude": ["--strict-mcp-config"],
    # Codex documents that auth still loads with --ignore-user-config. This
    # avoids loading the operator's MCP/config fleet into an isolated build.
    "codex": ["--ignore-user-config"],
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


def _usage_token_count(value: Any) -> int:
    """Normalize malformed provider token telemetry without losing exact cost."""
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


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
    generation_id: str = ""
    cost_source: str = "none"
    status: str = "succeeded"
    estimated_exposure_usd: float = 0.0
    cached_tokens: int = 0
    cache_write_tokens: int = 0


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
    deadline = time.monotonic() + _LEDGER_LOCK_TIMEOUT_S
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            try:
                os.write(fd, b"\0")
            finally:
                os.close(fd)
        # Another process can observe the newly-created filename just before
        # its creator writes the sentinel byte. Wait for completed atomic
        # initialization instead of racing writes/flushes on Windows.
        while handle is None:
            candidate = lock_path.open("r+b")
            candidate.seek(0, os.SEEK_END)
            if candidate.tell() >= 1:
                handle = candidate
                break
            candidate.close()
            if time.monotonic() >= deadline:
                raise BudgetExceeded("timed out initializing the budget ledger lock")
            time.sleep(0.02)
    except OSError as exc:
        raise BudgetExceeded(f"budget ledger lock unavailable: {exc}") from exc

    locked = False
    assert handle is not None
    try:
        if sys.platform == "win32":
            import msvcrt

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
    def begin_run_capture(self, routing_profile: str = "") -> None:
        """Start an isolated, task-local route capture for THIS run so concurrent
        agent runs that share this client don't clobber each other. Agents call
        this at run start (replacing a bare ``routes.clear()``)."""
        _LAST_MODEL.set(None)
        _LAST_ROUTE.set(None)
        _ROUTES.set([])
        _ROUTING_PROFILE.set(str(routing_profile or "balanced"))

    def _resolve_pinned_model(
        self,
        *,
        tier: Tier,
        model_override: str | None = None,
        setting_names: tuple[str, ...] = (),
        file_hint: str | None = None,
        task_type: str = "",
        profile: str | None = None,
    ) -> str:
        effective_profile = str(profile or _ROUTING_PROFILE.get() or "balanced")
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
            try:
                info = self.router.model_cost_info(pinned)
                log.info(
                    "llm.explicit_model_pin",
                    model=pinned,
                    profile=effective_profile,
                    price_class=info.get("price_class"),
                    input_per_million=info.get("prompt_usd_per_million"),
                    output_per_million=info.get("completion_usd_per_million"),
                )
            except Exception as exc:  # noqa: BLE001 - classification is advisory for explicit pins
                log.debug("llm.explicit_model_pin_unclassified", model=pinned, error=str(exc)[:120])
            return pinned
        try:
            return self.router.resolve(
                tier,
                file_hint,
                task_type=task_type,
                profile=effective_profile,
            )
        except TypeError:
            # Compatibility for externally supplied routers implementing the
            # pre-profile resolve contract.
            return self.router.resolve(tier, file_hint, task_type=task_type)

    def _openrouter_cost_details(
        self, model: str, usage: dict[str, Any], pt: int, ct: int
    ) -> tuple[float, str]:
        """Exact provider-reported cost, with catalog-rate fallback.

        OpenRouter includes ``usage.cost`` on normal responses. The previous flat
        $0.50/M estimate badly understated premium output pricing and overstated
        some value models, hiding why a supposedly cheap build was expensive.
        """
        raw_cost = usage.get("cost")
        try:
            exact = float(str(raw_cost))
        except (TypeError, ValueError):
            exact = math.nan
        if math.isfinite(exact) and exact >= 0:
            return exact, "provider"
        try:
            info = self.router.model_cost_info(model)
            prompt_rate = info.get("prompt_usd_per_token")
            completion_rate = info.get("completion_usd_per_token")
            request_rate = info.get("request_usd")
            if isinstance(prompt_rate, (int, float)) and isinstance(completion_rate, (int, float)):
                request_cost = float(request_rate) if isinstance(request_rate, (int, float)) else 0.0
                return (
                    float(pt) * float(prompt_rate)
                    + float(ct) * float(completion_rate)
                    + request_cost,
                    "catalog_estimate",
                )
        except Exception as exc:  # noqa: BLE001 - accounting fallback must not break a completion
            log.warning("llm.catalog_cost_unavailable", model=model, error=str(exc)[:120])
        # Last-resort compatibility estimate when both response cost and catalog
        # metadata are unavailable. This is intentionally loud and never called
        # a precise price.
        log.warning("llm.cost_estimated_flat", model=model, tokens=pt + ct)
        return (pt + ct) / 1_000_000.0 * 0.5, "flat_estimate"

    def _openrouter_cost(self, model: str, usage: dict[str, Any], pt: int, ct: int) -> float:
        return self._openrouter_cost_details(model, usage, pt, ct)[0]

    def _record_failed_openrouter_attempt(
        self,
        model: str,
        exc: BaseException,
        *,
        prompt_tokens: int,
        max_completion_tokens: int,
    ) -> None:
        """Persist retry evidence without claiming an unverified provider charge."""
        potentially_billed = isinstance(
            exc,
            (TimeoutError, httpx.TimeoutException, httpx.NetworkError, _AgenticTurnStall),
        )
        exposure = 0.0
        if potentially_billed and not _is_free_model_id(model):
            exposure = self._openrouter_cost(
                model,
                {},
                max(0, int(prompt_tokens)),
                max(0, int(max_completion_tokens)),
            )
        result = LLMResult(
            text="",
            model=model,
            backend="openrouter",
            generation_id="",
            cost_source="unconfirmed",
            status=f"failed_{classify_llm_error(exc)}",
            estimated_exposure_usd=max(0.0, exposure),
        )
        self.budget.record(result)
        log.warning(
            "llm.openrouter_attempt_failed",
            model=model,
            status=result.status,
            possible_billed=potentially_billed,
            max_exposure_usd=round(exposure, 6),
            error=_err_reason(exc),
        )

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
        self,
        model: str,
        data: dict[str, Any],
        sent: list[dict[str, Any]],
        msg: dict[str, Any] | None,
    ) -> LLMResult:
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        pt = _usage_token_count(usage.get("prompt_tokens"))
        ct = _usage_token_count(usage.get("completion_tokens"))
        prompt_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        cached_tokens = _usage_token_count(
            prompt_details.get("cached_tokens", usage.get("cached_tokens"))
        )
        cache_write_tokens = _usage_token_count(
            prompt_details.get(
                "cache_write_tokens", usage.get("cache_write_tokens")
            )
        )
        if not _is_free_model_id(model) and pt == 0 and ct == 0:
            pt = max(1, sum(self._msg_bytes(m) for m in sent) // 4)
            raw_response = msg if msg is not None else data
            ct = max(1, len(json.dumps(raw_response, sort_keys=True, default=str)) // 4)
            log.warning("llm.openrouter_agentic_usage_missing", model=model, est_tokens=pt + ct)
        if _is_free_model_id(model):
            cost, cost_source = 0.0, "free"
        else:
            cost, cost_source = self._openrouter_cost_details(
                model, usage, pt, ct
            )
        result = LLMResult(
            text=str((msg or {}).get("content") or ""),
            model=model,
            backend="openrouter",
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=cost,
            generation_id=str(data.get("id") or ""),
            cost_source=cost_source,
            status="provider_response",
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        self.budget.record(result)
        self.budget.check()
        return result

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
    _cli_cache_checked_at: dict[str, float] = {}
    _cli_version_cache: dict[str, tuple[str, str]] = {}

    @classmethod
    def _cli_available(cls, provider: str) -> bool:
        provider = str(provider or "").strip().lower()
        if provider not in _KNOWN_CLI_PROVIDERS:
            return False
        checked_at = cls._cli_cache_checked_at.get(provider)
        expired = (
            checked_at is not None
            and time.monotonic() - checked_at >= _CLI_STATUS_TTL_SECONDS
        )
        if provider not in cls._cli_cache or expired:
            previous = cls._cli_cache.get(provider)
            available = shutil.which(provider) is not None
            cls._cli_cache[provider] = available
            cls._cli_cache_checked_at[provider] = time.monotonic()
            if previous is not None and previous != available:
                cls._cli_version_cache.pop(provider, None)
        return cls._cli_cache[provider]

    @classmethod
    def _cli_version(cls, provider: str) -> str:
        """Return a bounded ``--version`` line without probing model/auth state."""
        provider = str(provider or "").strip().lower()
        if not cls._cli_available(provider):
            return ""
        path = str(shutil.which(provider) or "")
        cached = cls._cli_version_cache.get(provider)
        if cached is not None and cached[0] == path:
            return cached[1]
        version = ""
        try:
            result = subprocess.run(
                [path or provider, *_CLI_VERSION_ARGS[provider]],
                capture_output=True,
                check=False,
                text=True,
                timeout=3,
            )
            raw = result.stdout or result.stderr or ""
            version = next((line.strip() for line in raw.splitlines() if line.strip()), "")[:160]
        except (OSError, subprocess.SubprocessError):
            version = ""
        cls._cli_version_cache[provider] = (path, version)
        return version

    @classmethod
    def clear_cli_status_cache(cls) -> None:
        """Force the next status read to re-detect installed CLI binaries."""
        cls._cli_cache.clear()
        cls._cli_cache_checked_at.clear()
        cls._cli_version_cache.clear()

    @classmethod
    def _cli_detail(cls, provider: str) -> dict[str, Any]:
        provider = str(provider or "").strip().lower()
        available = cls._cli_available(provider)
        path = str(shutil.which(provider) or "") if available else ""
        version = cls._cli_version(provider) if available else ""
        return {
            "provider": provider,
            "label": _CLI_DISPLAY_NAMES.get(provider, provider),
            "backend": f"{provider}_cli",
            "command": provider,
            "available": available,
            "path": path,
            "version": version,
            "version_state": "reported" if version else ("unknown" if available else "missing"),
            # PATH/version checks do not prove that the CLI is signed in. The
            # CLI owns authentication and billing outside SkyN3t's API-key and
            # budget ledgers, so never label these runs provider-confirmed free.
            "account_source": "local_cli_session",
            "account_verified": False,
            "cost_source": "not_reported_by_cli",
            "cost_usd_known": False,
        }

    @property
    def backend(self) -> str:
        """Resolve the active backend from policy + availability."""
        pref = str(self.settings.llm_backend or "auto").strip().lower()
        if pref not in SUPPORTED_LLM_BACKENDS:
            return "stub"
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
        for prov in dict.fromkeys((preferred, *_KNOWN_CLI_PROVIDERS)):
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
        pref = str(self.settings.llm_backend or "auto").strip().lower()
        active = self.backend
        openrouter_configured = bool(openrouter_key(self.settings))
        preferred_cli = (getattr(self.settings, "cli_llm_provider", "") or "claude").lower()
        cli_details = {p: self._cli_detail(p) for p in _KNOWN_CLI_PROVIDERS}
        cli_available = {p: detail["available"] for p, detail in cli_details.items()}
        state = "ready"
        reason = ""
        if pref not in SUPPORTED_LLM_BACKENDS:
            state = "unsupported_backend"
            reason = f"Unsupported LLM backend {pref!r}; using the offline stub."
        elif pref == "openrouter" and not openrouter_configured:
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
            codegen_backend = "stub"
            codegen_reason = (
                f"codegen_cli_provider={codegen_provider!r} is set but unavailable; "
                "codegen uses the offline scaffold and does not fall back to OpenRouter."
            )
        elif active == "openrouter":
            codegen_backend = "openrouter"
            codegen_reason = "codegen follows the global OpenRouter backend."
        else:
            codegen_backend = active
            codegen_reason = "codegen follows the active backend."

        if active.endswith("_cli"):
            active_provider = active[:-4]
            accounting = {
                key: cli_details[active_provider][key]
                for key in (
                    "account_source", "account_verified", "cost_source", "cost_usd_known"
                )
            }
            accounting["note"] = (
                "The local CLI owns sign-in and billing; SkyN3t does not receive exact "
                "dollar usage from this backend."
            )
        elif active == "openrouter":
            accounting = {
                "account_source": "skyn3t_openrouter_api_key",
                "account_verified": False,
                "cost_source": "provider_usage_or_catalog_estimate",
                "cost_usd_known": False,
                "note": "Per-call cost evidence is recorded from OpenRouter responses when available.",
            }
        else:
            accounting = {
                "account_source": "offline",
                "account_verified": True,
                "cost_source": "offline_stub",
                "cost_usd_known": True,
                "note": "The offline stub makes no provider call.",
            }

        return {
            "requested": pref,
            "active": active,
            "state": state,
            "reason": reason,
            "openrouter_configured": openrouter_configured,
            "preferred_cli": preferred_cli,
            "cli_available": cli_available,
            "cli_details": cli_details,
            "accounting": accounting,
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
            for m in self.router.fallback_candidates(
                tier,
                primary=primary,
                profile=str(_ROUTING_PROFILE.get() or "balanced"),
            ):
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

    async def _resilient_call(
        self,
        primary: str,
        tier: Tier,
        attempt_fn,
        *,
        prompt_tokens: int = 0,
        max_completion_tokens: int = 0,
    ):
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
                    self._record_failed_openrouter_attempt(
                        model,
                        exc,
                        prompt_tokens=prompt_tokens,
                        max_completion_tokens=max_completion_tokens,
                    )
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

    @staticmethod
    def _compact_persisted_write_call(
        tool_call: dict[str, Any], args: dict[str, Any], result: str
    ) -> int:
        """Drop duplicate file bodies from a successful historical write call.

        OpenRouter receives the complete conversation on every agentic turn. Keeping
        every prior ``write_file`` argument therefore resends the app-so-far over and
        over, even though those exact bytes are already durable in the worktree and
        can be recovered with ``read_file``. Replace only confirmed, fully persisted
        writes with small semantic receipts. Rejected or failed writes retain their
        original arguments so the model can diagnose them.

        Returns the number of UTF-8 bytes removed from future request history.
        This changes neither generated files nor the number/size of future writes.
        """
        try:
            function = tool_call.get("function")
            if not isinstance(function, dict):
                return 0
            name = str(function.get("name") or "")
            outcome = str(result or "")
            if (
                name not in {"write_file", "write_files"}
                or not outcome.startswith("OK ")
                or "rejected" in outcome.lower()
            ):
                return 0

            original = str(function.get("arguments") or "{}")
            compact = dict(args)
            if name == "write_file":
                body = str(args.get("content", ""))
                size = len(body.encode("utf-8", "replace"))
                compact[_PERSISTED_WRITE_RECEIPT_KEY] = {
                    "version": 1,
                    "kind": "write_file",
                    "bytes": size,
                }
                compact["content"] = (
                    f"[persisted to workspace: {size} UTF-8 bytes; "
                    "use read_file to inspect]"
                )
            else:
                files = args.get("files")
                if not isinstance(files, list) or not files:
                    return 0
                receipts: list[dict[str, Any]] = []
                for item in files:
                    if not isinstance(item, dict):
                        return 0
                    receipt = dict(item)
                    body = str(item.get("content", ""))
                    size = len(body.encode("utf-8", "replace"))
                    receipt[_PERSISTED_WRITE_RECEIPT_KEY] = {
                        "version": 1,
                        "kind": "write_file",
                        "bytes": size,
                    }
                    receipt["content"] = (
                        f"[persisted to workspace: {size} UTF-8 bytes; "
                        "use read_file to inspect]"
                    )
                    receipts.append(receipt)
                compact["files"] = receipts
                compact[_PERSISTED_WRITE_RECEIPT_KEY] = {
                    "version": 1,
                    "kind": "write_files",
                    "files": len(receipts),
                }

            serialized = json.dumps(
                compact, ensure_ascii=False, separators=(",", ":")
            )
            function["arguments"] = serialized
            return max(
                0,
                len(original.encode("utf-8", "replace"))
                - len(serialized.encode("utf-8", "replace")),
            )
        except Exception as exc:  # noqa: BLE001 - optimization must degrade open
            log.warning("llm.write_history_compaction_failed", error=str(exc)[:120])
            return 0

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
        max_tokens: int | None = 2048,
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

    @staticmethod
    def _cli_attempt_result(
        provider: str,
        prompt: str,
        *,
        status: str,
        text: str = "",
    ) -> LLMResult:
        return LLMResult(
            text=text,
            model=f"{provider}-cli",
            backend=f"{provider}_cli",
            prompt_tokens=max(1, len(prompt) // 4),
            completion_tokens=max(0, len(text) // 4),
            cost_usd=0.0,
            cost_source="not_reported_by_cli",
            status=status,
        )

    def _record_failed_cli_attempt(
        self,
        provider: str,
        prompt: str,
        *,
        status: str,
    ) -> None:
        """Persist unknown-cost CLI usage even when no response can be returned."""
        try:
            self.budget.record(
                self._cli_attempt_result(provider, prompt, status=status)
            )
        except Exception as exc:  # noqa: BLE001 - accounting must not mask cancellation
            log.warning(
                "llm.cli_failure_accounting_failed",
                provider=provider,
                error=str(exc)[:120],
            )

    def _failed_cli_fallback(
        self,
        provider: str,
        prompt: str,
        system: str | None,
        json_mode: bool,
        *,
        status: str,
    ) -> LLMResult:
        fallback = self._stub(f"{provider}-cli", prompt, system, json_mode)
        return self._cli_attempt_result(
            provider,
            prompt,
            status=status,
            text=fallback.text,
        )

    async def _cli(self, provider, prompt, system, json_mode, images=None) -> LLMResult:
        """Run a locally-installed coding-agent CLI in headless print mode.

        Degrades to the stub backend (never raises) if the CLI fails or times
        out, so a build keeps moving (design rule #6).
        """
        full = prompt if not system else f"{system}\n\n{prompt}"
        # build-from-image: reference the image FILE(S) so the CLI reads them as a
        # visual reference ALONGSIDE the full text context — the same pattern the
        # vision judge (studio/visual_check) uses. A data: URL is written to a
        # temp file first; the image is prepended so it frames the context below.
        temp_images: list[str] = []
        paths: list[str] = []
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
        argv = [
            *_CLI_COMMANDS.get(provider, [provider, "-p"]),
            *_no_mcp_args(self.settings, provider),
        ]
        stdin_prompt = provider == "codex"
        if provider == "codex":
            for path in paths:
                argv.extend(("--image", path))
            # stdin avoids Windows' command-line length ceiling for large
            # architecture/review prompts. A literal '-' is Codex's documented
            # noninteractive stdin prompt form.
            argv.append("-")
        else:
            argv.append(full)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin_prompt else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group -> killable as a tree
            )
            communicate = (
                proc.communicate(full.encode("utf-8"))
                if stdin_prompt
                else proc.communicate()
            )
            out, err = await asyncio.wait_for(
                communicate, timeout=self.settings.cli_llm_timeout
            )
        except TimeoutError:
            log.warning("llm.cli_timeout", provider=provider, timeout=self.settings.cli_llm_timeout)
            await self._terminate(proc)  # don't orphan the CLI subprocess
            return self._failed_cli_fallback(
                provider, prompt, system, json_mode, status="failed_cli_timeout"
            )
        except asyncio.CancelledError:
            # An outer stage timeout cancelled us — kill the tree before unwinding
            # so a slow ``claude -p`` can't keep running for 90 minutes orphaned.
            await self._terminate(proc)
            self._record_failed_cli_attempt(
                provider, prompt, status="failed_cli_cancelled"
            )
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.cli_failed", provider=provider, error=str(exc)[:160])
            await self._terminate(proc)
            return self._failed_cli_fallback(
                provider, prompt, system, json_mode, status="failed_cli_spawn"
            )
        finally:
            # Don't leak the decoded data:-URL reference images into the temp dir.
            for tp in temp_images:
                try:
                    os.unlink(tp)
                except OSError:
                    pass

        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            log.warning("llm.cli_nonzero", provider=provider,
                        err=(err or b"").decode("utf-8", "replace")[:160])
            return self._failed_cli_fallback(
                provider, prompt, system, json_mode, status="failed_cli_nonzero"
            )
        if not text:
            log.warning("llm.cli_empty", provider=provider)
            return self._failed_cli_fallback(
                provider, prompt, system, json_mode, status="failed_cli_empty"
            )
        if json_mode:
            text = _strip_code_fences(text)
        approx_p = max(1, len(full) // 4)
        return LLMResult(
            text=text, model=f"{provider}-cli", backend=f"{provider}_cli",
            prompt_tokens=approx_p, completion_tokens=max(1, len(text) // 4), cost_usd=0.0,
            cost_source="not_reported_by_cli", status="cli_response",
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
            return confined_path(root, str(path))

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

        def _planned_path_ready(raw: str) -> bool:
            path = Path(raw)
            if not path.is_file():
                return False
            try:
                if path.stat().st_size > _PERSISTED_WRITE_RECEIPT_MAX_BYTES:
                    return True
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # Preserve the prior readiness behavior for an existing but
                # momentarily unreadable file; proof/build validation owns I/O
                # failures that are unrelated to compacted history receipts.
                return True
            return not _is_persisted_write_receipt_body(body)

        def _planned_missing_count() -> int:
            return sum(1 for raw in planned if not _planned_path_ready(raw))

        def _run_tool(name: str, args: dict) -> tuple[str, int]:
            """Run one tool and return ``(message, changed_file_count)``.

            The progress watchdog advances only for new content. Repeating a
            successful no-op write is activity, but it is not build progress.
            """
            if name == "write_file":
                if _is_persisted_write_receipt(args):
                    return (
                        "ERROR: persisted write receipt is history metadata, not file "
                        "content; use read_file to inspect the existing file",
                        0,
                    )
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
                if _is_persisted_write_receipt(args):
                    return (
                        "ERROR: persisted write receipt batch is history metadata and "
                        "cannot be replayed",
                        0,
                    )
                batch = args.get("files")
                if not isinstance(batch, list) or not batch:
                    return "ERROR: files must contain at least one file object", 0
                checked: list[tuple[Path, str, str]] = []
                targets: set[str] = set()
                for item in batch:
                    if not isinstance(item, dict):
                        return "ERROR: every files item must be an object", 0
                    if _is_persisted_write_receipt(item):
                        # Reject the entire batch before validating or writing any
                        # sibling. A receipt mixed with real bodies must never turn
                        # a nominally atomic model action into a partial write.
                        return (
                            "ERROR: write_files batch contains a persisted write "
                            "receipt and was rejected atomically",
                            0,
                        )
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
                    return p.read_text(encoding="utf-8", errors="replace"), 0
                except OSError as e:
                    return f"ERROR: {e}", 0
            if name == "list_files":
                out: list[str] = []
                for f in root.rglob("*"):
                    if f.is_file() and not ({"node_modules", ".git", ".next", "dist"} & set(f.parts)):
                        out.append(str(f.relative_to(root)))
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
        session_id = f"skyn3t-agentic-{uuid.uuid4().hex}"
        provider_requests = 0
        context_bytes_sent = 0
        max_context_bytes_sent = 0
        tool_call_count = 0
        write_tool_calls = 0
        single_write_calls = 0
        batch_write_calls = 0
        write_argument_bytes_compacted = 0
        cached_tokens = 0
        cache_write_tokens = 0
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
        coverage_stagnation_writes = 0
        coverage_stagnation_warned = False
        incomplete_finish_nudged = False
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
                    sent_context_bytes = sum(self._msg_bytes(item) for item in sent)

                    async def _do(
                        m,
                        _sent=sent,
                        _turn=turn,
                        _wrote=wrote,
                        _sent_context_bytes=sent_context_bytes,
                    ):
                        async def _post():
                            nonlocal provider_requests, context_bytes_sent
                            nonlocal max_context_bytes_sent
                            provider_requests += 1
                            context_bytes_sent += _sent_context_bytes
                            max_context_bytes_sent = max(
                                max_context_bytes_sent, _sent_context_bytes
                            )
                            body = {"model": m, "messages": _sent, "tools": _AGENTIC_TOOLS,
                                    "tool_choice": "auto", "session_id": session_id}
                            resp = await client.post(OPENROUTER_URL, json=body, headers=headers)
                            resp.raise_for_status()
                            return resp, m

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
                        resp, model = await asyncio.wait_for(
                            self._resilient_call(
                                model,
                                Tier.CHEAP,
                                _do,
                                prompt_tokens=max(
                                    1, sum(self._msg_bytes(item) for item in sent) // 4
                                ),
                                max_completion_tokens=16_384,
                            ),
                            timeout=max(0.01, remaining),
                        )
                    except _AgenticTurnStall:
                        # _resilient_call already recorded this exact provider
                        # attempt. Do not double-count its unconfirmed exposure
                        # when the exhausted stall propagates through wait_for.
                        timed_out = True
                        loop_error = stall_reason or "agentic provider turn stalled"
                        break
                    except TimeoutError:
                        self._record_failed_openrouter_attempt(
                            model,
                            TimeoutError("agentic request exceeded its progress window"),
                            prompt_tokens=max(
                                1, sum(self._msg_bytes(item) for item in sent) // 4
                            ),
                            max_completion_tokens=16_384,
                        )
                        timed_out = True
                        loop_error = f"agentic request made no file-write progress for {budget}s"
                        log.warning("llm.or_agentic_timeout", turns=turn, wrote=wrote)
                        break
                    if model != attempted_model:
                        fallback_model = model
                    try:
                        data = resp.json()
                    except (ValueError, TypeError) as exc:
                        data = {"_raw_response": str(getattr(resp, "text", "") or "")}
                        usage_result = self._record_openrouter_agentic_usage(
                            model, data, sent, None
                        )
                        cached_tokens += usage_result.cached_tokens
                        cache_write_tokens += usage_result.cache_write_tokens
                        usage_result.status = "malformed_response"
                        loop_error = "agentic provider returned malformed JSON after billing"
                        log.warning("llm.or_agentic_malformed", error=str(exc)[:160])
                        break
                    raw_msg: Any = None
                    if isinstance(data, dict):
                        try:
                            raw_msg = data["choices"][0]["message"]
                        except (KeyError, IndexError, TypeError):
                            raw_msg = None
                    try:
                        usage_result = self._record_openrouter_agentic_usage(
                            model,
                            data if isinstance(data, dict) else {},
                            sent,
                            raw_msg if isinstance(raw_msg, dict) else None,
                        )
                        cached_tokens += usage_result.cached_tokens
                        cache_write_tokens += usage_result.cache_write_tokens
                    except BudgetExceeded as exc:
                        budget_error = str(exc)
                        log.warning("llm.or_agentic_budget_exceeded", error=budget_error)
                        break
                    if not isinstance(raw_msg, dict):
                        usage_result.status = "malformed_response"
                        loop_error = "agentic provider response had no usable message after billing"
                        log.warning("llm.or_agentic_malformed", error=loop_error)
                        break
                    usage_result.status = "succeeded"
                    msg = raw_msg
                    # Own a shallow structural copy so successful write arguments
                    # can be compacted without mutating the provider response object.
                    tcs: list[dict[str, Any]] = []
                    for raw_tc in msg.get("tool_calls") or []:
                        if not isinstance(raw_tc, dict):
                            continue
                        tc_copy = dict(raw_tc)
                        raw_function = raw_tc.get("function")
                        if isinstance(raw_function, dict):
                            tc_copy["function"] = dict(raw_function)
                        tcs.append(tc_copy)
                    messages.append({"role": "assistant", "content": msg.get("content") or "",
                                     "tool_calls": tcs})
                    wrote_before = wrote
                    if tcs:
                        for tc in tcs:
                            tool_call_count += 1
                            fn = tc.get("function") or {}
                            name = fn.get("name", "")
                            try:
                                args = _json.loads(fn.get("arguments") or "{}")
                            except ValueError:
                                args = {}
                            if name == "finish":
                                finished, result = True, "OK"
                            else:
                                if name == "write_file":
                                    write_tool_calls += 1
                                    single_write_calls += 1
                                elif name == "write_files":
                                    write_tool_calls += 1
                                    batch_write_calls += 1
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
                                        coverage_stagnation_writes = 0
                                        coverage_stagnation_warned = False
                                        incomplete_finish_nudged = False
                                    elif current_missing > 0:
                                        # Wrapper/build-helper proliferation has
                                        # its own family-aware guard below. Do not
                                        # let this broader coverage guard preempt
                                        # its better diagnostic after a single
                                        # helper write on a one-file manifest.
                                        non_helper_changes = sum(
                                            1
                                            for changed_path in changed_paths
                                            if not _agentic_auxiliary_family(
                                                changed_path.replace("\\", "/")
                                                .strip()
                                                .lower()
                                            )
                                        )
                                        coverage_stagnation_writes += min(
                                            changed_files, non_helper_changes
                                        )
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
                                if name in {"write_file", "write_files"}:
                                    write_argument_bytes_compacted += (
                                        self._compact_persisted_write_call(tc, args, result)
                                    )
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
                    if (
                        not finished
                        and planned_missing_count > 0
                        and coverage_stagnation_writes
                        >= max(
                            _AUXILIARY_CHURN_THRESHOLD,
                            planned_missing_count,
                        )
                    ):
                        # One complete pass over the *remaining* manifest without
                        # landing another planned file is coverage stagnation,
                        # regardless of whether the model cycles through one path
                        # or many. As coverage advances the window shrinks with the
                        # work left, preventing a nearly-finished large slice from
                        # buying another original-manifest-sized rewrite cycle.
                        # This is a semantic progress guard, not a file/output cap:
                        # productive sessions reset it every time planned coverage
                        # increases and can create an arbitrarily large manifest.
                        missing_paths = sorted(
                            str(Path(raw).relative_to(root)).replace("\\", "/")
                            for raw in planned
                            if not _planned_path_ready(raw)
                        )
                        stagnant_writes = coverage_stagnation_writes
                        coverage_stagnation_writes = 0
                        if coverage_stagnation_warned:
                            log.warning(
                                "llm.or_agentic_coverage_stagnation_abort",
                                missing=planned_missing_count,
                                wrote=wrote,
                                turns=turn,
                            )
                            loop_error = (
                                "agentic aborted after two remaining-manifest write "
                                "passes made no planned-file coverage progress"
                            )
                            break
                        coverage_stagnation_warned = True
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "PLANNED-COVERAGE STALL: you changed "
                                    f"{stagnant_writes} files without creating any "
                                    f"of the {planned_missing_count} missing files in "
                                    "your assigned manifest. Stop revising existing "
                                    "files. Create these missing owned files now, in "
                                    "one or more write_files calls, then integrate and "
                                    "finish:\n- " + "\n- ".join(missing_paths)
                                ),
                            }
                        )
                        log.info(
                            "llm.or_agentic_coverage_stagnation_nudge",
                            missing=planned_missing_count,
                            stagnant_writes=stagnant_writes,
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
                        if planned and planned_missing_count > 0:
                            missing_paths = sorted(
                                str(Path(raw).relative_to(root)).replace("\\", "/")
                                for raw in planned
                                if not _planned_path_ready(raw)
                            )
                            finished = False
                            if incomplete_finish_nudged:
                                loop_error = (
                                    "agentic finish rejected because planned files are "
                                    "missing or contain persisted-write receipt metadata: "
                                    + ", ".join(missing_paths)
                                )
                                log.warning(
                                    "llm.or_agentic_incomplete_finish_abort",
                                    missing=missing_paths,
                                    wrote=wrote,
                                )
                                break
                            incomplete_finish_nudged = True
                            messages.append({
                                "role": "user",
                                "content": (
                                    "FINISH DENIED: these planned files are absent or "
                                    "contain only persisted-write receipt metadata. Write "
                                    "their real complete contents before finishing:\n- "
                                    + "\n- ".join(missing_paths)
                                ),
                            })
                            log.info(
                                "llm.or_agentic_incomplete_finish_nudge",
                                missing=missing_paths,
                                wrote=wrote,
                            )
                            continue
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
                 "turns": turn, "provider_requests": provider_requests,
                 "context_bytes_sent": context_bytes_sent,
                 "max_context_bytes_sent": max_context_bytes_sent,
                 "tool_calls": tool_call_count,
                 "write_tool_calls": write_tool_calls,
                 "single_write_calls": single_write_calls,
                 "batch_write_calls": batch_write_calls,
                 "write_argument_bytes_compacted": write_argument_bytes_compacted,
                 "cached_tokens": cached_tokens,
                 "cache_write_tokens": cache_write_tokens,
                 "session_id": session_id,
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
        # guard instead of always burning the full ceiling. Codex also emits
        # JSONL and is validated by its process exit status. Copilot has no such
        # mode, so it keeps the blocking path.
        stream = provider in ("claude", "codex", "kimi")
        # claude takes --verbose with stream-json; kimi's stream-json does not.
        stream_args = (["--output-format", "stream-json", "--verbose"] if provider == "claude"
                       else ["--output-format", "stream-json"] if provider == "kimi" else [])
        # Optional per-call model pin for CLIs that expose --model; ignored when
        # no model is given so the CLI's default applies (today's behaviour).
        model_args = (
            ["--model", model]
            if (model and provider in ("claude", "codex", "copilot", "kimi"))
            else []
        )
        # --setting-sources project isolates codegen from the host's Claude Code
        # config (output-style plugins, hooks) so they can't corrupt the build.
        argv = {
            "claude": ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
                       "--setting-sources", "project", *model_args, *stream_args, *nm],
            "codex": [
                "codex", "exec", "--ephemeral", "--sandbox", "workspace-write",
                "--color", "never", "--skip-git-repo-check", "--json",
                "--cd", workdir, *model_args, *nm, "-",
            ],
            # Kimi requires --print for noninteractive and stream-json modes.
            # It has no documented disable-all ambient-MCP flag in 1.41.
            "kimi": ["kimi", "--print", "--prompt", prompt, *model_args, *stream_args],
            "copilot": [
                "copilot", "-p", prompt, "--allow-all-tools", "--no-ask-user",
                "--no-auto-update", "--no-custom-instructions", *model_args, *nm,
            ],
        }.get(provider, [provider, "-p", prompt])
        stdin_prompt = provider == "codex"
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir,
                stdin=asyncio.subprocess.PIPE if stdin_prompt else None,
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
            if stdin_prompt and proc.stdin is not None:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
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
            self._record_failed_cli_attempt(
                provider, prompt, status="failed_cli_cancelled"
            )
            raise
        except Exception as exc:  # noqa: BLE001 - never raise into the build
            await self._terminate(proc)
            self._record_failed_cli_attempt(
                provider, prompt, status="failed_cli_exception"
            )
            log.warning("llm.agentic_failed", provider=provider, error=str(exc)[:160])
            return {
                "ok": False,
                "backend": f"{provider}_cli",
                "cost_source": "not_reported_by_cli",
                "cost_usd": None,
                "error": str(exc)[:160],
            }
        effective_model = model or f"{provider}-cli"
        self.budget.record(LLMResult(
            text="",
            model=effective_model,
            backend=f"{provider}_cli",
            prompt_tokens=max(1, len(prompt) // 4),
            completion_tokens=0,
            cost_usd=0.0,
            cost_source="not_reported_by_cli",
            status="cli_response" if ok else "failed_cli",
        ))
        self._record_completion(f"{provider}_cli", "codegen", effective_model)
        return {
            "ok": ok,
            "completed": ok,
            "backend": f"{provider}_cli",
            "model": effective_model,
            "account_source": "local_cli_session",
            "cost_source": "not_reported_by_cli",
            "cost_usd": None,
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
            body = {"model": m, "messages": messages}
            if max_tokens is not None and int(max_tokens) > 0:
                body["max_tokens"] = int(max_tokens)
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
        estimated_prompt_tokens = max(1, (len(system or "") + len(prompt or "")) // 4)
        # This completion count is used only for unconfirmed failed-attempt cost
        # exposure. It is never added to the provider request when max_tokens is
        # None/0, so cap-free codegen can use the model's native output capacity.
        exposure_completion_tokens = (
            max(0, int(max_tokens))
            if max_tokens is not None and int(max_tokens) > 0
            else 16_384
        )
        resp, model = await self._resilient_call(
            model,
            tier,
            _do,
            prompt_tokens=estimated_prompt_tokens,
            max_completion_tokens=exposure_completion_tokens,
        )
        malformed_error = ""
        try:
            data = resp.json()
        except (ValueError, TypeError) as exc:
            data = {}
            malformed_error = str(exc)[:160]

        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        pt = _usage_token_count(usage.get("prompt_tokens"))
        ct = _usage_token_count(usage.get("completion_tokens"))
        text = ""
        if not malformed_error:
            try:
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
            except (KeyError, IndexError, TypeError) as exc:
                malformed_error = str(exc)[:160]
        if malformed_error:
            # Preserve pipeline degradation behavior, but retain the provider's
            # usage/cost instead of returning a zero-cost offline stub.
            text = self._stub(model, prompt, system, json_mode).text
            log.warning("llm.openrouter_malformed", error=malformed_error)
        # OpenRouter sometimes omits `usage` on a 200. Defaulting to 0 made PAID
        # models report $0 — corrupting cost tracking + budget caps. Estimate
        # tokens from text length (~4 chars/token) so a paid call is never free.
        if not _is_free_model_id(model) and pt == 0 and ct == 0:
            pt = estimated_prompt_tokens
            raw_response = getattr(resp, "text", "") or text
            ct = max(1, len(raw_response) // 4)
            log.warning("llm.openrouter_usage_missing", model=model, est_tokens=pt + ct)
        # :free models cost $0; paid models prefer OpenRouter's exact usage cost.
        if _is_free_model_id(model):
            cost, cost_source = 0.0, "free"
        else:
            cost, cost_source = self._openrouter_cost_details(
                model, usage, int(pt or 0), int(ct or 0)
            )
        return LLMResult(text=text, model=model, backend="openrouter",
                         prompt_tokens=pt, completion_tokens=ct, cost_usd=cost,
                         generation_id=str(data.get("id") or "") if isinstance(data, dict) else "",
                         cost_source=cost_source,
                         status="malformed_response" if malformed_error else "succeeded")

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
