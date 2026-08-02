"""Unified LLM client with cost-aware routing and an offline stub backend.

One entry point — :meth:`LLMClient.complete` — resolves a tier to a model via
the :class:`ModelRouter`, then dispatches to a backend:

* ``openrouter`` — real HTTP only when explicitly selected and configured.
* ``<provider>_cli`` — shells out to a locally-installed CLI (``claude``,
  ``codex``, ``kimi``, ``copilot``) in headless print mode. Real generation
  with **no SkyN3t API key** — handy when you already have a coding-agent CLI
  signed in.
* ``stub`` — deterministic offline responses so the full pipeline (and the
  test suite) runs with **no keys and no network**. This is what makes
  "brief -> runnable app" demonstrable out of the box.

Backend selection (``settings.llm_backend``): ``auto`` is intentionally local
only. It walks ``settings.auto_cli_priority`` (default ``codex,kimi``)
and resolves to the first signed-in CLI, otherwise staying offline on the stub.
It never selects OpenRouter merely because a key happens to be configured — a
key is configuration, not consent to spend; hosted routing requires either an
explicit ``openrouter`` backend or the ``auto_allow_openrouter`` opt-in. Every
call is metered and checked against budget caps — design rules #5 (cheap by
default) and #6 (degrade, don't crash).

A per-call ``provider_override`` pins one request to a specific backend
regardless of the above, which is what lets a Mixture-of-Agents council fan out
across several providers at once (see :mod:`skyn3t.intelligence.council`).
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
from copy import copy as shallow_copy
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import structlog

from skyn3t.adapters.provider_limits import provider_slot, resolve_provider_limit
from skyn3t.adapters.reasoning_timeouts import apply_reasoning_floor, reasoning_timeout_floor
from skyn3t.adapters.stall import (
    STALL_PROMPT_ACCEPTANCE_TIMEOUT,
    STALL_PROMPT_MISDELIVERY,
    StallEvidence,
    stall_report,
)
from skyn3t.agents._common import confined_path
from skyn3t.atomic_io import atomic_write_text
from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.model_router import ModelRouter, Tier
from skyn3t.core.model_router import is_free_model_id as router_is_free_model_id
from skyn3t.exec_paths import executable_shim as _executable_shim
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
from skyn3t.security.secrets import filter_env, mask_secrets

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
# A build is submitted against one resolved route. Keep that route task-local
# so changing the dashboard setting while a background build is running affects
# the next build, not the in-flight one. The mapping also carries the immutable
# requested/effective labels used by build diagnostics.
_BUILD_ROUTING: contextvars.ContextVar = contextvars.ContextVar(
    "skyn3t_llm_build_routing", default=None
)
_BUILD_SETTINGS: contextvars.ContextVar = contextvars.ContextVar(
    "skyn3t_llm_build_settings", default=None
)
_BUILD_ROUTER: contextvars.ContextVar = contextvars.ContextVar(
    "skyn3t_llm_build_router", default=None
)
# These settings can change from the dashboard while a background build is
# running. Snapshot every input that can alter provider-model selection so an
# in-flight task remains one coherent routing decision.
_BUILD_MODEL_POLICY_FIELDS = (
    "preferred_model",
    "free_only",
    "no_claude",
    "model_evolution",
    "auto_route",
    "openrouter_codegen_model",
    "codegen_cli_provider",
    "codegen_cli_model",
    "model_cheap",
    "model_ui",
    "model_backend",
    "model_strong",
    "model_docs",
    "llm_fallback_enabled",
    "llm_fallback_models",
    "llm_retry_enabled",
    "llm_max_retries",
    "llm_retry_base_delay",
    "llm_retry_max_delay",
    "vision_model",
    "openrouter_agentic",
    "slice_tier_models",
    "tournament_model_pool",
    "best_of_n_across_models",
)
# A streamed CLI execution runs in the same task as ``agentic_build``. Keep its
# compact evidence task-local so concurrent build stages never overwrite each
# other's Codex/Claude/Kimi trace before the caller persists it.
_AGENTIC_STREAM_EVIDENCE: contextvars.ContextVar = contextvars.ContextVar(
    "skyn3t_agentic_stream_evidence", default=None
)
# Heal-attempt counter for the streamed CLI execution, task-local for the same
# reason as the receipt above. ``agentic_build`` sets it before each consume
# so a stall bundle records which attempt stalled WITHOUT changing
# ``_consume_agentic_stream``'s signature (older test/custom consumers call it
# positionally and must keep working).
_AGENTIC_STREAM_ATTEMPT: contextvars.ContextVar = contextvars.ContextVar(
    "skyn3t_agentic_stream_attempt", default=0
)
# Bounded assistant-prose accumulator for the vent channel: friction reports
# ride the session's plain text output, so the tail of it must survive the
# session. Kept OUT of the cli_execution receipt (that receipt is persisted in
# durable manifests and must not carry raw agent prose — see agentic_build).
_AGENTIC_OUTPUT_TEXT_LIMIT = 16_000


def _mask_agentic_text(text: str) -> str:
    """Output-side secret masking for text leaving the host trust boundary.

    Tool observations (a read_file dump can echo a credential that reached the
    worktree) and the bounded prose tail (vent channel / cli_execution
    output_text) are returned to the agent loop and recorded in run metadata.
    ``filter_env`` keeps host keys out of subprocess environments; this is the
    output-side complement. Guarded: masking must never break a build.
    """
    try:
        return mask_secrets(text)
    except Exception:  # noqa: BLE001
        return text


def _capture_agentic_output_text(evt: dict, parts: list[str], budget: list[int]) -> None:
    """Accumulate assistant prose from one stream-json event, bounded by budget.

    Handles the claude/kimi ``assistant`` message shape (content blocks) and the
    terminal ``result`` event's final text. Best-effort: unrecognised shapes
    contribute nothing. Prose is masked on capture so the vent channel and the
    returned ``output_text`` never carry a registered host secret."""
    if budget[0] <= 0:
        return
    text = ""
    evt_type = evt.get("type")
    if evt_type == "assistant":
        message = evt.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            text = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        elif isinstance(content, str):
            text = content
    elif evt_type == "result":
        raw = evt.get("result")
        text = raw if isinstance(raw, str) else ""
    if text:
        parts.append(_mask_agentic_text(text)[: budget[0]])
        budget[0] -= len(parts[-1])
_BUDGET_LEDGER_LOCK = threading.RLock()
_LEDGER_LOCK_TIMEOUT_S = 15.0

log = structlog.get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# One keep-alive HTTP client per running event loop for the plain OpenRouter
# completion path. ``_openrouter``'s per-attempt ``httpx.AsyncClient`` used to
# pay a fresh SSLContext/CA-bundle construction (CPU on the event-loop thread)
# plus a new TCP+TLS handshake to openrouter.ai on EVERY attempt — every retry,
# every failover rung, and every one of a build's many small ``complete()``
# calls. Keyed by running loop (the house pattern of
# ``provider_limits._ADMISSIONS``) because a client binds to the loop that
# first uses it and this repo calls ``asyncio.run()`` from the CLI and many
# tests. Entries for closed loops are evicted WITHOUT ``aclose()`` — their
# connections died with the loop; the pool is process-lifetime by design. The
# agentic loop (``_openrouter_agentic``) already shares one client per session
# and does not route through this cache.
_OPENROUTER_CLIENTS: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


def reset_openrouter_clients() -> None:
    """Drop every cached per-loop OpenRouter client. Test hook; mirrors
    :func:`skyn3t.adapters.provider_limits.reset_provider_limits`."""
    _OPENROUTER_CLIENTS.clear()


def _shared_openrouter_client() -> httpx.AsyncClient:
    """Return the current loop's shared keep-alive OpenRouter client.

    Construction goes through the module attribute ``httpx.AsyncClient`` so
    tests that monkeypatch ``llm.httpx.AsyncClient`` keep intercepting it. The
    constructor timeout is only a safety net: every request passes its own
    per-request ``timeout=`` (the httpx-supported override), which is how the
    per-model reasoning floor keeps its exact semantics on a shared client.
    """
    loop = asyncio.get_running_loop()
    for stale in [cached for cached in _OPENROUTER_CLIENTS if cached.is_closed()]:
        _OPENROUTER_CLIENTS.pop(stale, None)
    client = _OPENROUTER_CLIENTS.get(loop)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(timeout=120)
        _OPENROUTER_CLIENTS[loop] = client
    return client

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
    "kimi": ["kimi", "--output-format", "text"],
    "copilot": ["copilot"],
}
# Flags whose VALUE is the prompt. These must be the last argv before the
# prompt itself, otherwise any flag appended in between (``--model``, the
# MCP-disabling args) is consumed as the prompt's value and the real prompt
# becomes a stray positional — which is exactly how ``copilot -p --no-mcp
# "<prompt>"`` produced "Invalid command format". ``claude``'s ``-p`` is a
# boolean print flag and ``codex`` reads the prompt from stdin, so neither
# appears here.
_CLI_PROMPT_FLAG: dict[str, str] = {
    "kimi": "--prompt",
    "copilot": "-p",
}
# CLIs that receive the prompt over STDIN instead of as an argv element.
#
# This is a correctness requirement on Windows, not an optimisation. npm ships
# ``claude`` as ``claude.CMD``, a batch shim, so every argument is re-parsed by
# cmd.exe — and a multi-kilobyte, multi-line prompt gets mangled or truncated
# there. The observed symptom was an MoA advisor receiving only the leading
# system-prompt portion and replying "Ready as reference advisor. Send me the
# task" instead of advising, which then counted as a SUCCESSFUL advisor and was
# injected into the codegen prompt as if it were real guidance. Codex already
# used stdin for the same reason (command-line length ceiling).
_CLI_STDIN_PROMPT: frozenset[str] = frozenset({"codex", "claude"})
# kimi and copilot CANNOT read the prompt from stdin: kimi's ``--prompt`` is a
# commander option requiring an argv value (verified against the binary and
# ``--help``), and copilot documents that piped stdin is IGNORED when ``-p``
# is present (github/copilot-cli#1046 tracks stdin support). So when argv
# cannot carry the prompt intact (see ``_argv_prompt_hazard``) the agentic
# path hands it over via a transient FILE plus a short bootstrap prompt, and
# the plain completion path — which cannot rely on file-read tools — fails
# loudly instead of letting a silently truncated prompt count as success.
_CLI_PROMPT_FILENAME = "skyn3t-agentic-prompt.md"


# Characters cmd.exe re-interprets while re-parsing a .cmd/.bat shim's argv:
# newlines terminate the command outright; the rest expand or redirect.
_CMD_UNSAFE_CHARS = ("\n", "\r", "%", "^", "&", "|", "<", ">", '"')


def _argv_prompt_hazard(command: str, text: str, *, os_name: str | None = None) -> str | None:
    """Why ``text`` cannot safely travel as one argv element of ``command``.

    Returns a reason string, or ``None`` when argv is safe. Three regimes:

    - A Windows ``.cmd``/``.bat`` npm shim re-executes through cmd.exe, which
      treats a newline as a command terminator, expands/redirects on
      ``% ^ & | < > "``, and caps its command line at 8191 chars — a large or
      metacharacter-bearing prompt is truncated or mangled SILENTLY and the
      CLI answers the surviving fragment plausibly enough to count as success
      (the exact failure ``_CLI_STDIN_PROMPT`` documents).
    - A native Windows executable is bounded by the 32767-char CreateProcess
      command-line ceiling (loud failure, but still a failure).
    - POSIX allows ~128KB per element (Linux MAX_ARG_STRLEN).

    Each threshold keeps headroom for the executable path and other flags.
    ``os_name`` exists so tests can pin every regime on any platform.
    """
    platform = os_name if os_name is not None else os.name
    if platform == "nt" and command.lower().endswith((".cmd", ".bat")):
        if len(text) > 6_000 or any(ch in text for ch in _CMD_UNSAFE_CHARS):
            return "cmd_shim_reparse"
        return None
    if platform == "nt":
        return "createprocess_limit" if len(text) > 30_000 else None
    return "posix_arg_strlen" if len(text) > 100_000 else None


def _prompt_file_bootstrap(prompt_path: str) -> str:
    """Single-line argv prompt pointing the agent at the real prompt file.

    Deliberately newline-free: it must survive the same cmd.exe re-parse that
    forced the real prompt off argv in the first place.
    """
    return (
        f"Your complete build instructions are in the file {prompt_path} - "
        "read that file now and carry out everything in it exactly as if its "
        "contents had been given to you directly as this prompt. The file is "
        "transient build scaffolding outside your output directory: never "
        "copy it, reference it from generated code, or count it as part of "
        "the app."
    )


KNOWN_CLI_PROVIDERS = ("codex", "claude", "copilot", "kimi")
SUPPORTED_LLM_BACKENDS = (
    "auto", "stub", "openrouter",
    *(f"{provider}_cli" for provider in KNOWN_CLI_PROVIDERS),
)
_KNOWN_CLI_PROVIDERS = KNOWN_CLI_PROVIDERS
# Reasoning-effort levels accepted on ``complete(effort=...)`` and passed to
# OpenRouter as ``{"reasoning": {"effort": X}}``. Duplicated from
# ``adapters.model_slot.EFFORT_LEVELS`` on purpose — the two modules stay
# independent by design (see KNOWN_CLI_PROVIDERS above), and the same drift
# test that pins the provider lists pins these too.
_REASONING_EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})
# The CLI an unattended build REQUIRES on PATH before it will run (the routing
# lock at ``explicit_routing_lock_error``). This is a separate question from
# which CLI ``auto`` then executes on — that is ``auto_cli_priority`` below.
# Keeping both separate from ``cli_llm_provider`` is deliberate: the latter is
# for manually selected tooling (for example a vision workflow), while automatic
# builds must never jump to a hosted API without explicit consent.
_AUTO_EXECUTION_CLI_PROVIDER = "codex"
# Fallback order when ``settings.auto_cli_priority`` is empty or all-unknown.
# Codex leads only because it was the historical single choice, so existing
# hosts see no behaviour change; nothing about the design privileges it.
# ``copilot`` is intentionally not in the default chain — it stays fully
# supported and explicitly selectable, just not auto-preferred.
_AUTO_EXECUTION_CLI_PRIORITY: tuple[str, ...] = ("codex", "claude", "kimi")
_CLI_DISPLAY_NAMES = {
    "codex": "Codex CLI",
    "claude": "Claude Code CLI",
    "copilot": "GitHub Copilot CLI",
    "kimi": "Kimi Code CLI",
}
_CLI_VERSION_ARGS = {provider: ("--version",) for provider in KNOWN_CLI_PROVIDERS}
_CLI_STATUS_TTL_SECONDS = 5.0
# Minimum seconds between repeated "backend degraded to stub" warnings for the
# same (preference, reason) pair — backend resolution is a hot path.
_STUB_DEGRADE_LOG_INTERVAL_S = 300.0


def _cli_subprocess_env() -> dict[str, str]:
    """Return the non-secret environment for a local coding CLI child.

    The CLI keeps PATH, HOME, CODEX_HOME, and local subscription-login files,
    while generated-workspace instructions cannot read Foundry provider, deploy,
    or control-plane credentials.
    """
    return filter_env()


def openrouter_key(settings: Settings) -> str:
    """Resolve the OpenRouter key from live settings or supported env aliases."""
    # A GUI disconnect must be durable even when a parent shell supplies the
    # standard OPENROUTER_API_KEY alias. Keep that external credential intact;
    # this per-app setting only controls whether SkyN3t is allowed to use it.
    if not bool(getattr(settings, "openrouter_enabled", True)):
        return ""
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
# vue/sveltekit/react_ts are first-class KNOWN_STACKS web stacks; omitting them
# here silently dropped the ENTIRE web-engineering prompt body from their builds.
_WEB_UI_STACKS = frozenset({
    "react_vite", "nextjs", "astro", "remix", "static_html",
    "vue", "sveltekit", "react_ts",
})
_MOBILE_STACKS = frozenset({"react_native"})
_DESKTOP_STACKS = frozenset({"tauri"})
_API_STACKS = frozenset({"fastapi", "node_express"})
_CLI_STACKS = frozenset({"python_cli"})
# The registry object (skyn3t/core/stacks.py; core is upstream of adapters — no
# cycle). The other sets above are AGENT-vocab prompt routing and stay local.
from skyn3t.core.stacks import GAME_STACKS as _GAME_STACKS  # noqa: E402


def _resolved_agentic_idle_timeout(settings, total_timeout: float) -> float:
    """One meaning for ``agentic_idle_timeout`` across BOTH agentic paths.

    The documented convention (see the settings field): a positive value is
    the inactivity window; 0 falls back to the TOTAL agentic budget as the
    window. Negative values are clamped into the 0 case — previously a ``-5``
    was truthy on the CLI path and instantly killed every build (the first
    readline "timed out" immediately), while the OpenRouter path treated
    0/negative as no watchdog at all, contradicting the docstring (audit M13).
    """
    try:
        value = float(getattr(settings, "agentic_idle_timeout", 0) or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return value
    return float(total_timeout)


def _agentic_system_for(stack: str) -> str:
    """Compose the agentic codegen system prompt for ``stack``. SkyN3t builds every kind
    of app, so the body is stack-specific (web / game / mobile / desktop / api / cli)
    instead of the old hardcoded 'build a React marketing site' that derailed non-web
    builds. An EMPTY stack falls back to the web body (the react_vite default); an
    unrecognized stack gets only the core+tail so wrong-genre guidance can't derail it."""
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


def _auto_priority_order(settings: Any) -> tuple[str, ...]:
    """``auto``'s CLI preference order, filtered to known providers.

    Unknown entries are dropped with a warning rather than raising: a typo in
    one entry must not take unattended builds offline. Narrow settings doubles
    may omit the field entirely, so it is read defensively. Shared by
    ``LLMClient._auto_cli_priority`` and the routing lock so preflight and
    resolution can never disagree about which chain ``auto`` searches.
    """
    raw = str(getattr(settings, "auto_cli_priority", "") or "")
    order: list[str] = []
    for token in raw.split(","):
        name = token.strip().lower().removesuffix("_cli").removesuffix("-cli")
        if not name:
            continue
        if name not in _KNOWN_CLI_PROVIDERS:
            log.warning("llm.auto_priority_unknown_provider", provider=name)
            continue
        if name not in order:
            order.append(name)
    return tuple(order) or _AUTO_EXECUTION_CLI_PRIORITY


def explicit_routing_lock_error(
    settings: Any,
    *,
    cli_available: Callable[[str], bool] | None = None,
    require_codex_for_auto: bool = False,
) -> str:
    """Return the blocking reason for an explicit route, or ``""``.

    Explicit selections are locks and must never be silently reinterpreted as
    a different provider. Callers that are about to start an unattended build
    can also require a usable ``auto`` executor — any CLI in the operator's
    ``auto_cli_priority`` chain, or hosted OpenRouter only under the explicit
    ``auto_allow_openrouter`` opt-in; this turns a missing executor into an
    upfront, zero-spend failure instead of a fake offline scaffold. The CLI
    probe is injectable so callers and tests can validate policy without
    running a model command.
    """
    # Older narrow test doubles and third-party callers may provide only the
    # fields they need for one feature. They never explicitly opted into the
    # build-routing policy, so keep their historical offline behavior instead
    # of treating a missing field as an intentional ``auto`` selection.
    has_explicit_backend = hasattr(settings, "llm_backend")
    build_routing = _BUILD_ROUTING.get()
    requested = str(
        build_routing.get("requested_backend")
        if isinstance(build_routing, dict) and build_routing.get("requested_backend")
        else (getattr(settings, "llm_backend", "auto") or "auto")
    ).strip().lower()
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

    locked_codegen = (
        build_routing.get("codegen")
        if isinstance(build_routing, dict)
        and isinstance(build_routing.get("codegen"), dict)
        else None
    )
    if isinstance(locked_codegen, dict):
        requested_codegen_backend = str(
            locked_codegen.get("requested_backend") or ""
        ).strip().lower()
        codegen_provider = (
            requested_codegen_backend[:-4]
            if locked_codegen.get("source") == "codegen_cli_pin"
            and requested_codegen_backend.endswith("_cli")
            else ""
        )
    else:
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

    # An explicitly selected codegen provider is the tighter routing lock and
    # is intentionally validated above. Only then apply the automatic executor
    # requirement, and only when the settings object actually exposes an LLM
    # backend field (real Settings does; legacy minimal doubles may not).
    if require_codex_for_auto and has_explicit_backend and requested == "auto":
        # Mirror ``LLMClient._resolve_backend``: ``auto`` executes on the first
        # available CLI in the operator's priority order, and a configured
        # OpenRouter key is consent to spend only under the explicit opt-in.
        order = _auto_priority_order(settings)
        if not any(provider_available(provider) for provider in order):
            hosted_ok = bool(
                getattr(settings, "auto_allow_openrouter", False)
            ) and bool(openrouter_key(settings))
            if not hosted_ok:
                return (
                    "Automatic builds require one of these CLIs on PATH: "
                    f"{', '.join(order)}. Hosted OpenRouter fallback requires "
                    "the auto_allow_openrouter opt-in. Select a backend "
                    "explicitly to use another provider."
                )
    return ""


def enforce_explicit_routing_lock(
    settings: Any,
    *,
    cli_available: Callable[[str], bool] | None = None,
    require_codex_for_auto: bool = False,
) -> None:
    """Raise before work begins when a selected route is unusable.

    ``require_codex_for_auto`` is used by executable build paths. It preserves
    the offline ``LLMClient`` behavior for library callers while making an
    automatic application build fail closed before any model/API work starts.
    """
    reason = explicit_routing_lock_error(
        settings,
        cli_available=cli_available,
        require_codex_for_auto=require_codex_for_auto,
    )
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

# Stall auto-heal (claw-code Phase 1.5 port): a prompt that never reached the
# agent (misdelivery) or was accepted but never produced a first token is worth
# exactly ONE silent resend inside the same agentic_build call — the classic
# transient CLI hiccup. Every other stall kind escalates immediately with its
# classified error. The resend's idle window is clamped to the build's
# remaining budget so the heal can never double the wall-clock silently.
_STALL_HEALABLE_KINDS = frozenset({
    STALL_PROMPT_MISDELIVERY,
    STALL_PROMPT_ACCEPTANCE_TIMEOUT,
})
_STALL_MAX_HEALS = 1
# Below this much remaining budget a resend could not even deliver a first
# token — escalate instead of spending the tail of the budget on a doomed heal.
_STALL_HEAL_MIN_REMAINING_S = 30.0

# Stream event types that prove the session was ACCEPTED but carry no agent
# content (claude's system/init, codex's thread.started/turn.started). The
# first event outside this set is the "first token" the acceptance stall kinds
# wait for.
_AGENTIC_LIFECYCLE_EVENT_TYPES = frozenset({
    "system", "init", "thread.started", "turn.started", "session",
    "session.started", "ready",
})


def _agentic_stall_report(
    *,
    provider: str,
    lifecycle_state: str,
    bytes_received: int,
    events_received: int,
    content_events: int,
    seconds_since_last_output: float,
    prompt_sent_at: float,
    process_alive: bool,
    exit_code,
    idle_timeout: float,
    output_tail: str,
    stderr_tail: str,
    attempt: int = 0,
) -> dict:
    """Classify one detected stall into ``{stall_kind, stall_evidence}``.

    Thin defensive wrapper over :func:`skyn3t.adapters.stall.stall_report`:
    tails are masked before they can touch a log line or run metadata, and any
    failure degrades to ``unknown`` — classification must never break a build.
    """
    try:
        evidence = StallEvidence(
            provider=str(provider or "")[:32],
            attempt=int(attempt),
            lifecycle_state=str(lifecycle_state or "")[:64],
            bytes_received=max(0, int(bytes_received or 0)),
            events_received=max(0, int(events_received or 0)),
            content_events=max(0, int(content_events or 0)),
            seconds_since_last_output=float(seconds_since_last_output or 0.0),
            prompt_sent_at=float(prompt_sent_at or 0.0),
            process_alive=bool(process_alive),
            exit_code=exit_code if isinstance(exit_code, int) else None,
            idle_timeout=float(idle_timeout or 0.0),
            output_tail=_mask_agentic_text(str(output_tail or "")[-600:]),
            stderr_tail=_mask_agentic_text(str(stderr_tail or "")[-600:]),
        )
        return stall_report(evidence)
    except Exception:  # noqa: BLE001
        return {"stall_kind": "unknown", "stall_evidence": "classifier unavailable"}


# CLI stream payloads can contain complete prompts, tool output, and source
# files. Persist only compact operational evidence: safe identifiers that can
# help an operator resume a CLI session, plus bounded counters/status. Never
# retain arbitrary event fields or event bodies in a build manifest.
_CLI_EXECUTION_EVIDENCE_SCHEMA = 1
_CLI_EXECUTION_MAX_EVENT_TYPES = 32
_CLI_EXECUTION_MAX_EVENT_TYPE_LENGTH = 80
_CLI_EXECUTION_MAX_IDENTIFIER_LENGTH = 256
_CLI_EXECUTION_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_CLI_EXECUTION_EVENT_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")


def _safe_cli_execution_identifier(value: Any) -> str:
    """Return a bounded non-secret stream identifier, or an empty string.

    JSONL is emitted by an external executable. IDs are useful resume evidence,
    but copying free-form fields would accidentally persist prompts or tool
    results. Codex thread IDs and the supported CLIs' session IDs fit this
    conservative ASCII shape.
    """
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    identifier = str(value).strip()
    if not identifier or len(identifier) > _CLI_EXECUTION_MAX_IDENTIFIER_LENGTH:
        return ""
    return identifier if _CLI_EXECUTION_IDENTIFIER_RE.fullmatch(identifier) else ""


def _safe_cli_execution_event_type(value: Any) -> str:
    """Return a bounded event type without retaining arbitrary CLI output."""
    if not isinstance(value, str):
        return ""
    event_type = value.strip()
    if not event_type or len(event_type) > _CLI_EXECUTION_MAX_EVENT_TYPE_LENGTH:
        return ""
    return event_type if _CLI_EXECUTION_EVENT_TYPE_RE.fullmatch(event_type) else ""


def _new_cli_execution_evidence(
    provider: str,
    *,
    streamed: bool,
    cli_version: str = "",
) -> dict[str, Any]:
    """Create the fixed-shape, bounded record persisted for a CLI build."""
    evidence: dict[str, Any] = {
        "schema_version": _CLI_EXECUTION_EVIDENCE_SCHEMA,
        "provider": str(provider or "")[:32],
        "streamed": bool(streamed),
        "event_count": 0,
        "parsed_event_count": 0,
        "invalid_line_count": 0,
        "event_type_counts": {},
        "event_type_overflow_count": 0,
        "result_event_seen": False,
        "timed_out": False,
        "timeout_kind": "",
        "termination_reason": "",
        "terminal_event_type": "",
        "exit_code": None,
        "exit_status": "not_started",
    }
    version = str(cli_version or "").strip().replace("\x00", "")[:160]
    if version:
        evidence["cli_version"] = version
    if provider == "codex":
        # ``agentic_build`` uses ``codex exec --ephemeral``. A thread ID is
        # still useful forensic/resume-relevant evidence, but this flag prevents
        # callers from treating it as a durable resumable session claim.
        evidence["session_persistence"] = "ephemeral"
    return evidence


def _record_cli_execution_event(evidence: dict[str, Any], event: dict[str, Any]) -> None:
    """Accumulate one parsed JSONL event without retaining its payload."""
    evidence["parsed_event_count"] = int(evidence.get("parsed_event_count", 0)) + 1
    event_type = _safe_cli_execution_event_type(event.get("type"))
    if event_type:
        counts = evidence.get("event_type_counts")
        if not isinstance(counts, dict):
            counts = {}
            evidence["event_type_counts"] = counts
        if event_type in counts:
            counts[event_type] = int(counts[event_type]) + 1
        elif len(counts) < _CLI_EXECUTION_MAX_EVENT_TYPES:
            counts[event_type] = 1
        else:
            evidence["event_type_overflow_count"] = (
                int(evidence.get("event_type_overflow_count", 0)) + 1
            )

    # Codex currently emits ``thread.started`` with ``thread_id``. Support the
    # common snake/camel alternatives without ever copying a generic ``id`` or
    # nested arbitrary object that may contain agent content.
    for source_key, target_key in (
        ("thread_id", "thread_id"),
        ("threadId", "thread_id"),
        ("session_id", "session_id"),
        ("sessionId", "session_id"),
    ):
        identifier = _safe_cli_execution_identifier(event.get(source_key))
        if identifier and not evidence.get(target_key):
            evidence[target_key] = identifier

    if event_type.startswith("thread."):
        thread = event.get("thread")
        if isinstance(thread, dict):
            identifier = _safe_cli_execution_identifier(thread.get("id"))
            if identifier and not evidence.get("thread_id"):
                evidence["thread_id"] = identifier
    elif event_type.startswith("session."):
        session = event.get("session")
        if isinstance(session, dict):
            identifier = _safe_cli_execution_identifier(session.get("id"))
            if identifier and not evidence.get("session_id"):
                evidence["session_id"] = identifier

    if event_type == "result":
        evidence["result_event_seen"] = True
        evidence["result_is_error"] = bool(event.get("is_error"))
        evidence["terminal_event_type"] = event_type
    elif event_type in {
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
        "turn.interrupted",
    }:
        # This is evidence only. Keep the established success contract: only a
        # Claude/Kimi ``result`` error overrides a zero process exit.
        evidence["terminal_event_type"] = event_type


def _no_mcp_args(settings, provider: str) -> list[str]:
    """MCP-disabling argv for ``provider`` when ``cli_disable_mcp`` is on (default)."""
    if not getattr(settings, "cli_disable_mcp", True):
        return []
    return list(_CLI_NO_MCP_ARGS.get(provider, []))


def _isolated_codex_windows_sandbox_args(no_mcp_args: list[str]) -> list[str]:
    """Keep Codex workspace-write usable when its user config is isolated.

    ``--ignore-user-config`` intentionally keeps ambient MCP servers and desktop
    plugins out of a SkyN3t build. On Windows it also omits the user's native
    sandbox selection, causing Codex to mount the worktree read-only despite an
    explicit ``--sandbox workspace-write``. Carry only the sandbox mode needed
    for the isolated process; this neither loads user MCP config nor disables
    Codex's own restricted workspace boundary.
    """
    if os.name == "nt" and "--ignore-user-config" in no_mcp_args:
        return ["-c", 'windows.sandbox="elevated"']
    return []


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
                raise BudgetLedgerError("timed out initializing the budget ledger lock")
            time.sleep(0.02)
    except OSError as exc:
        raise BudgetLedgerError(f"budget ledger lock unavailable: {exc}") from exc

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
                        raise BudgetLedgerError(
                            "timed out waiting for the budget ledger lock"
                        ) from exc
                    time.sleep(0.02)
        else:
            fcntl = importlib.import_module("fcntl")

            while not locked:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise BudgetLedgerError(
                            "timed out waiting for the budget ledger lock"
                        ) from exc
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

    # Unannotated on purpose (not a dataclass field): process-wide throttle
    # stamp for the ledger-write-failure warning in _save_ledger.
    _last_save_warn = -1e9

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
            self._record_locked(r)

    def record_and_check(self, r: LLMResult) -> None:
        """Record ``r`` and enforce caps in ONE cross-process transaction.

        Semantically identical to ``record(r)`` followed by ``check()``:
        after ``_save_ledger`` the ledger file equals memory, so check()'s
        merge re-load would be a no-op. Fusing them matters because every
        completion pays the ledger guard — a cross-process lock file plus a
        JSON read/rewrite, and up to ``_LEDGER_LOCK_TIMEOUT_S`` under
        contention — once here instead of twice.
        """
        with self._ledger_guard():
            self._record_locked(r)
            self._check_caps()

    def _record_locked(self, r: LLMResult) -> None:
        """Apply one result to the ledger. Callers hold ``_ledger_guard``."""
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
            self._check_caps()

    def _check_caps(self) -> None:
        """Raise :class:`BudgetExceeded` when any configured cap is crossed."""
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
                return
            lock = _exclusive_ledger_lock(self.ledger_path)
            try:
                lock.__enter__()
            except BudgetLedgerError as exc:
                # Fail OPEN, loudly: cross-process cap enforcement degrades
                # for this one operation (the in-process lock above still
                # holds). Failing closed here used to surface as
                # BudgetExceeded — the orchestrator classified the "timed
                # out" text as transient, burned the whole task-retry budget,
                # and reported the build as a spend-cap hit.
                log.warning("llm.budget_ledger_unavailable", error=str(exc)[:160])
                yield
                return
            try:
                yield
            finally:
                lock.__exit__(None, None, None)

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
        except OSError as exc:
            # Cross-process daily-cap enforcement silently stopped here for
            # quota/AV-file-lock failures; every other degrade path in this
            # file logs. Throttled: record() calls this on every LLM result.
            now = time.monotonic()
            if now - BudgetTracker._last_save_warn >= 60.0:
                BudgetTracker._last_save_warn = now
                log.warning("llm.budget_ledger_write_failed", error=str(exc)[:160])

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
    """A configured spend/token cap was genuinely hit. Nothing else may raise
    this: callers (and the orchestrator's error classifier) treat it as a
    spend-cap verdict, so infrastructure noise dressed in this type burned
    task-retry budgets and misreported builds as over-budget."""


class BudgetLedgerError(RuntimeError):
    """The cross-process budget ledger (its lock file) is unavailable.

    Deliberately NOT a ``BudgetExceeded``: a lock hiccup is infrastructure
    noise, not a spend verdict. Handled inside ``BudgetTracker._ledger_guard``
    (fail-open with a loud warning) so it never escapes to callers."""


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
        # CLI providers already told (once each) that a requested reasoning
        # effort is a no-op for them — see _log_cli_effort_noop.
        self._cli_effort_noop_logged: set[str] = set()

    # ---- per-run route capture (task-local + global fallback) ---------------
    def begin_run_capture(self, routing_profile: str = "") -> None:
        """Start an isolated, task-local route capture for THIS run so concurrent
        agent runs that share this client don't clobber each other. Agents call
        this at run start (replacing a bare ``routes.clear()``)."""
        _LAST_MODEL.set(None)
        _LAST_ROUTE.set(None)
        _ROUTES.set([])
        _ROUTING_PROFILE.set(str(routing_profile or "balanced"))

    def _routing_settings(self) -> Settings:
        """Settings view frozen for the current build task, when one is active."""
        locked = _BUILD_SETTINGS.get()
        return locked if locked is not None else self.settings

    def _routing_router(self) -> ModelRouter:
        """Router clone bound to the current build's immutable settings view."""
        locked = _BUILD_ROUTER.get()
        return locked if locked is not None else self.router

    def _resolve_pinned_model(
        self,
        *,
        tier: Tier,
        model_override: str | None = None,
        setting_names: tuple[str, ...] = (),
        file_hint: str | None = None,
        task_type: str = "",
        profile: str | None = None,
        allow_live_catalog: bool | None = None,
    ) -> str:
        effective_profile = str(profile or _ROUTING_PROFILE.get() or "balanced")
        settings = self._routing_settings()
        router = self._routing_router()
        requested: list[str] = []
        if model_override:
            requested.append(str(model_override).strip())
        for name in setting_names:
            val = str(getattr(settings, name, "") or "").strip()
            if val:
                requested.append(val)
        for pinned in requested:
            if bool(getattr(settings, "free_only", False)) and not _is_free_model_id(pinned):
                log.warning("llm.free_only_ignored_paid_pin", model=pinned)
                continue
            if allow_live_catalog is not False:
                try:
                    info = router.model_cost_info(pinned)
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
            return router.resolve(
                tier,
                file_hint,
                task_type=task_type,
                profile=effective_profile,
                allow_live_catalog=allow_live_catalog,
            )
        except TypeError:
            # Compatibility for externally supplied routers implementing the
            # pre-profile resolve contract.
            return router.resolve(tier, file_hint, task_type=task_type)

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
    # (preference, reason) -> monotonic time of the last degraded-to-stub
    # warning; see _degraded_to_stub. Class-level like the CLI caches so the
    # throttle holds across the many short-lived clients a build creates.
    _stub_degrade_logged: dict[tuple[str, str], float] = {}

    @classmethod
    def _cli_executable(cls, provider: str) -> str:
        """Resolve the executable used for a local CLI invocation.

        On Windows the desktop-app launcher can be on ``PATH`` while the
        matching standalone runtime (and its sandbox helper binaries) live
        under ``CODEX_HOME``. Running the launcher directly can leave Codex
        unable to start its Windows sandbox helper, so prefer the same
        standalone executable its own ``doctor`` command reports when the
        detected path is that desktop shim. Custom and package-manager Codex
        installs remain untouched.
        """
        provider = str(provider or "").strip().lower()
        resolved = str(shutil.which(provider) or "")
        resolved = cls._windows_executable_shim(resolved)
        if provider != "codex" or os.name != "nt":
            return resolved

        normalized = resolved.replace("/", "\\").lower()
        desktop_shim = "\\openai\\codex\\bin\\codex.exe"
        if not resolved or desktop_shim not in normalized:
            return resolved

        try:
            codex_home = Path(
                os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
            ).expanduser()
            releases = codex_home / "packages" / "standalone" / "releases"
            candidates = [
                release / "bin" / "codex.exe"
                for release in releases.iterdir()
                if (release / "bin" / "codex.exe").is_file()
            ]
        except OSError:
            candidates = []
        if not candidates:
            return resolved

        def release_key(candidate: Path) -> tuple[tuple[int, ...], float]:
            match = re.match(r"(\d+(?:\.\d+)*)", candidate.parent.parent.name)
            version = tuple(int(part) for part in match.group(1).split(".")) if match else ()
            try:
                modified = candidate.stat().st_mtime
            except OSError:
                modified = 0.0
            return version, modified

        return str(max(candidates, key=release_key))

    @classmethod
    def _windows_executable_shim(cls, resolved: str) -> str:
        """Swap a non-executable Windows launcher for a runnable sibling.

        Thin wrapper over :func:`skyn3t.exec_paths.executable_shim`, kept as a
        classmethod because tests and callers reference it here. The shared
        helper exists because this bug appeared in three unrelated places —
        the coding CLIs, the vision CLI, and ``npm`` inside the sandbox.
        """
        return _executable_shim(resolved)

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
            available = bool(cls._cli_executable(provider))
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
        path = cls._cli_executable(provider)
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
                encoding="utf-8", errors="replace",
                timeout=3,
                env=_cli_subprocess_env(),
            )
            raw = result.stdout or result.stderr or ""
            version = next((line.strip() for line in raw.splitlines() if line.strip()), "")[:160]
        except (OSError, subprocess.SubprocessError):
            version = ""
        cls._cli_version_cache[provider] = (path, version)
        return version

    @classmethod
    def _cached_cli_version(cls, provider: str) -> str:
        """Return an already-probed version without delaying a build startup.

        Execution evidence benefits from a version when the status endpoint has
        already queried one. Do not spawn a second ``--version`` process just to
        enrich a build record; it is optional evidence, not a preflight.
        """
        cached = cls._cli_version_cache.get(str(provider or "").strip().lower())
        if cached is None:
            return ""
        return str(cached[1] or "").strip().replace("\x00", "")[:160]

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
        path = cls._cli_executable(provider) if available else ""
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

    def _auto_cli_priority(self) -> tuple[str, ...]:
        """``auto``'s CLI preference order, filtered to known providers.

        Delegates to the module-level helper so the routing lock searches the
        exact same chain that resolution executes on.
        """
        return _auto_priority_order(self.settings)

    def _auto_cli_provider(self) -> str:
        """First signed-in CLI in priority order, or "" when none is available."""
        for provider in self._auto_cli_priority():
            if self._cli_available(provider):
                return provider
        return ""

    def _resolve_backend(self, pref: str) -> str:
        """Map an explicit backend preference to a concrete backend.

        Shared by the :attr:`backend` property and ``provider_override`` so a
        slot resolves through exactly the same availability rules as a globally
        selected backend — an unavailable provider degrades to ``stub``, never to
        a DIFFERENT provider (silently substituting one would make a
        multi-provider fan-out's results uninterpretable).
        """
        pref = str(pref or "").strip().lower()
        if pref not in SUPPORTED_LLM_BACKENDS:
            return self._degraded_to_stub(pref, "unsupported_backend")
        if pref == "stub":
            return "stub"
        if pref == "openrouter":
            if openrouter_key(self.settings):
                return "openrouter"
            return self._degraded_to_stub(pref, "missing_key")
        if pref.endswith("_cli"):
            prov = pref[:-4]
            if self._cli_available(prov):
                return f"{prov}_cli"
            return self._degraded_to_stub(pref, "cli_missing")
        # ``auto``: first signed-in CLI in the operator's priority order. This is
        # deliberately NOT Codex-only any more — Codex merely leads the default
        # list so existing hosts see no change. An OpenRouter key is still
        # configuration, not consent to spend: hosted fallback requires the
        # explicit ``auto_allow_openrouter`` opt-in, otherwise auto degrades to
        # the offline stub exactly as before.
        provider = self._auto_cli_provider()
        if provider:
            return f"{provider}_cli"
        if bool(getattr(self.settings, "auto_allow_openrouter", False)) and openrouter_key(
            self.settings
        ):
            return "openrouter"
        return self._degraded_to_stub(pref, "no_cli_available")

    def _degraded_to_stub(self, pref: str, reason: str) -> str:
        """Return ``"stub"``, warning that a REQUESTED backend was degraded.

        A typo'd ``SKYN3T_LLM_BACKEND``, a missing OpenRouter key, or an
        unknown ``provider_override`` used to reach the offline stub with no
        log at resolve time — only ``backend_status()`` carried the reason,
        so builds silently produced stub output. ``_resolve_backend`` runs on
        every :attr:`backend` read (a hot path), so each distinct
        (preference, reason) pair is reported at most once per throttle
        window, not per read. The reason vocabulary mirrors
        ``backend_status()``'s ``state`` field.
        """
        now = time.monotonic()
        last = self._stub_degrade_logged.get((pref, reason))
        if last is None or now - last >= _STUB_DEGRADE_LOG_INTERVAL_S:
            self._stub_degrade_logged[(pref, reason)] = now
            log.warning("llm.backend_degraded_to_stub", requested=pref, reason=reason)
        return "stub"

    @property
    def backend(self) -> str:
        """Resolve the active backend from policy + availability."""
        locked = _BUILD_ROUTING.get()
        if isinstance(locked, dict):
            effective = str(locked.get("effective_backend") or "").strip().lower()
            if effective in {"stub", "openrouter"} or (
                effective.endswith("_cli")
                and effective[:-4] in _KNOWN_CLI_PROVIDERS
            ):
                return effective
        return self._resolve_backend(str(self.settings.llm_backend or "auto"))

    def _effective_backend(self, provider_override: str | None = None) -> str:
        """Backend for ONE call, honouring a per-call provider pin.

        ``provider_override`` deliberately outranks the ``_BUILD_ROUTING`` lock.
        That lock exists to keep the ACTING build pinned to one route; advisor /
        ensemble calls are side-calls whose entire purpose is to span providers,
        and Hermes resolves each MoA slot's provider surface independently for
        the same reason (``agent/moa_loop.py:313-338``).
        """
        pinned = str(provider_override or "").strip().lower()
        if not pinned:
            return self.backend
        return self._resolve_backend(pinned)

    @staticmethod
    def _execution_model_label(backend: str, requested_model: str = "") -> str:
        """Return an honest submission-time model label for one backend.

        Local CLIs choose their own model unless codegen has a dedicated pin,
        while the offline stub makes no provider call. OpenRouter can honor a
        per-build model pin directly; without one the actual model varies by
        tier and is therefore represented as router-selected rather than guessed.
        """
        backend = str(backend or "").strip().lower()
        requested_model = str(requested_model or "").strip()
        if backend == "openrouter":
            return requested_model or "router:auto"
        if backend.endswith("_cli"):
            return f"{backend[:-4]}-cli:default"
        return "offline-stub"

    def _usable_openrouter_pin(self, *models: str) -> str:
        """Return the first configured model permitted by the current cost lock."""
        free_only = bool(getattr(self._routing_settings(), "free_only", False))
        for raw in models:
            model = str(raw or "").strip()
            if model and (not free_only or _is_free_model_id(model)):
                return model
        return ""

    def build_routing_snapshot(self, model_override: str = "") -> dict[str, Any]:
        """Snapshot requested/effective build routing without exposing secrets."""
        requested_backend = str(
            getattr(self.settings, "llm_backend", "auto") or "auto"
        ).strip().lower()
        effective_backend = self.backend
        submission_model_override = str(model_override or "").strip()

        preferred_model = str(
            getattr(self.settings, "preferred_model", "") or ""
        ).strip()
        requested_model = preferred_model if effective_backend == "openrouter" else ""
        global_model = (
            self._usable_openrouter_pin(preferred_model)
            if effective_backend == "openrouter"
            else ""
        )
        effective_model = self._execution_model_label(
            effective_backend,
            global_model,
        )

        codegen_provider = str(
            getattr(self.settings, "codegen_cli_provider", "") or ""
        ).strip().lower()
        codegen_cli_model = str(
            getattr(self.settings, "codegen_cli_model", "") or ""
        ).strip()
        openrouter_codegen_model = str(
            getattr(self.settings, "openrouter_codegen_model", "") or ""
        ).strip()

        if codegen_provider and requested_backend == "stub":
            # Money fence, deeper than any bench/profile pin list: an
            # EXPLICITLY selected stub backend means offline and free — no
            # subscription CLI may be launched on its behalf by a codegen
            # override. A ``--llm-backend stub`` golden bench was observed
            # running real codex agentic builds because the operator's .env
            # codegen pin survived into the attempt settings. (A backend that
            # merely DEGRADED to stub keeps the pin: "stages offline, codegen
            # on my signed-in CLI" is a legitimate configuration.)
            log.warning("llm.codegen_override_ignored_stub_backend",
                        provider=codegen_provider)
            codegen_provider = ""

        if codegen_provider:
            requested_codegen_backend = f"{codegen_provider}_cli"
            effective_codegen_backend = (
                requested_codegen_backend
                if self._cli_available(codegen_provider)
                else "stub"
            )
            requested_codegen_model = codegen_cli_model
            effective_codegen_model = (
                codegen_cli_model or f"{codegen_provider}-cli:default"
                if effective_codegen_backend != "stub"
                else "offline-stub"
            )
            codegen_source = "codegen_cli_pin"
        else:
            requested_codegen_backend = requested_backend
            effective_codegen_backend = effective_backend
            codegen_source = "global_backend"
            if effective_codegen_backend.endswith("_cli"):
                requested_codegen_model = (
                    codegen_cli_model or submission_model_override
                )
                effective_codegen_model = (
                    requested_codegen_model
                    or f"{effective_codegen_backend[:-4]}-cli:default"
                )
            elif effective_codegen_backend == "openrouter":
                requested_codegen_model = (
                    submission_model_override
                    or openrouter_codegen_model
                    or preferred_model
                )
                usable_codegen_model = self._usable_openrouter_pin(
                    submission_model_override,
                    openrouter_codegen_model,
                    preferred_model,
                )
                effective_codegen_model = self._execution_model_label(
                    effective_codegen_backend,
                    usable_codegen_model,
                )
            else:
                requested_codegen_model = (
                    codegen_cli_model or submission_model_override
                )
                effective_codegen_model = "offline-stub"

        codegen = {
            "source": codegen_source,
            "requested_backend": requested_codegen_backend,
            "effective_backend": effective_codegen_backend,
            "requested_model": requested_codegen_model,
            "effective_model": effective_codegen_model,
        }
        model_policy = {
            name: deepcopy(getattr(self.settings, name, None))
            for name in _BUILD_MODEL_POLICY_FIELDS
        }
        return {
            "requested_backend": requested_backend,
            "effective_backend": effective_backend,
            "requested_model": requested_model,
            "effective_model": effective_model,
            "submission": {
                "requested_backend": requested_backend,
                "effective_backend": effective_backend,
                "requested_model": requested_model,
                "model_override": submission_model_override,
                "codegen": deepcopy(codegen),
            },
            "codegen": codegen,
            "model_policy": model_policy,
        }

    def active_build_routing_snapshot(
        self,
        model_override: str = "",
    ) -> dict[str, Any]:
        """Inherit an outer build route, otherwise capture current settings.

        Nested Improve/liveness/visual work belongs to the build that spawned
        it. A live GUI settings change must affect the next submission, not
        silently switch an already-running build's repair provider.
        """
        locked = _BUILD_ROUTING.get()
        if isinstance(locked, dict):
            return deepcopy(locked)
        return self.build_routing_snapshot(model_override)

    @contextmanager
    def build_routing_scope(
        self,
        snapshot: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Lock one background build to its submission-time provider route."""
        route = deepcopy(snapshot or self.build_routing_snapshot())
        effective = str(route.get("effective_backend") or "").strip().lower()
        if effective not in {"stub", "openrouter"} and not (
            effective.endswith("_cli")
            and effective[:-4] in _KNOWN_CLI_PROVIDERS
        ):
            raise RoutingLockError(
                f"Unsupported effective build backend {effective!r}."
            )
        policy = route.get("model_policy")
        settings_view = self.settings
        router_view = self.router
        if isinstance(policy, dict):
            frozen_policy = deepcopy(policy)
            if hasattr(self.settings, "model_copy"):
                settings_view = self.settings.model_copy(
                    update=frozen_policy,
                    deep=True,
                )
            else:  # pragma: no cover - compatibility for settings-like integrations
                settings_view = shallow_copy(self.settings)
                for name, value in frozen_policy.items():
                    setattr(settings_view, name, value)
            router_view = shallow_copy(self.router)
            router_view.settings = settings_view
            for name in ("_overrides", "_paid_fallback_cache"):
                if hasattr(router_view, name):
                    setattr(router_view, name, deepcopy(getattr(router_view, name)))

        route_token = _BUILD_ROUTING.set(route)
        settings_token = _BUILD_SETTINGS.set(settings_view)
        router_token = _BUILD_ROUTER.set(router_view)
        try:
            yield route
        finally:
            _BUILD_ROUTER.reset(router_token)
            _BUILD_SETTINGS.reset(settings_token)
            _BUILD_ROUTING.reset(route_token)

    def backend_status(self) -> dict:
        """Explain backend resolution without exposing secret values.

        ``backend`` remains execution-oriented for compatibility: explicit
        OpenRouter without a key still executes through the stub. This status
        object is the operator-facing truth: it records the requested backend,
        active backend, and why a fallback happened.
        """
        locked = _BUILD_ROUTING.get()
        pref = str(
            locked.get("requested_backend")
            if isinstance(locked, dict) and locked.get("requested_backend")
            else (self.settings.llm_backend or "auto")
        ).strip().lower()
        active = self.backend
        openrouter_configured = bool(openrouter_key(self.settings))
        # For ``auto`` the honest label is the head of the operator's priority
        # order, not a hardcoded codex (auto executes on the first signed-in
        # CLI in auto_cli_priority).
        auto_priority = self._auto_cli_priority()
        preferred_cli = (
            (auto_priority[0] if auto_priority else _AUTO_EXECUTION_CLI_PROVIDER)
            if pref == "auto"
            else (getattr(self.settings, "cli_llm_provider", "") or "codex").lower()
        )
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
            state = "auto_no_cli"
            reason = (
                "Auto found none of the priority CLIs "
                f"({', '.join(auto_priority) or 'codex, claude, kimi'}) signed in "
                "on PATH. Hosted OpenRouter fallback requires the explicit "
                "SKYN3T_AUTO_ALLOW_OPENROUTER opt-in plus a configured key."
            )

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
            "automatic_execution": {
                "backend": f"{_AUTO_EXECUTION_CLI_PROVIDER}_cli",
                "available": cli_available.get(_AUTO_EXECUTION_CLI_PROVIDER, False),
                "openrouter_fallback": False,
            },
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
        settings = self._routing_settings()
        if not getattr(settings, "llm_retry_enabled", True):
            return 0
        return max(0, int(getattr(settings, "llm_max_retries", 3)))

    def _retry_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter (jitter staggers concurrent retries off
        a shared failing endpoint — the same shape as orchestrator._backoff_delay,
        but tuned for a single LLM call)."""
        settings = self._routing_settings()
        base = float(getattr(settings, "llm_retry_base_delay", 0.5))
        cap = float(getattr(settings, "llm_retry_max_delay", 8.0))
        d = min((2 ** attempt) * base, cap)
        return d + random.uniform(0.0, d * 0.5)

    def _fallback_models(self, primary: str, tier: Tier) -> list[str]:
        """Ordered fallback model ids AFTER ``primary``: the operator override
        (``llm_fallback_models`` comma-list) first, then the router's per-tier
        candidates. De-duped, ``primary`` removed. ``[]`` when failover is off."""
        settings = self._routing_settings()
        router = self._routing_router()
        if not getattr(settings, "llm_fallback_enabled", True):
            return []
        out: list[str] = []
        free_only = bool(getattr(settings, "free_only", False))
        for m in str(getattr(settings, "llm_fallback_models", "") or "").split(","):
            m = m.strip()
            if not m:
                continue
            if free_only and not _is_free_model_id(m):
                log.warning("llm.free_only_ignored_paid_fallback", model=m)
                continue
            if m and m != primary and m not in out:
                out.append(m)
        try:
            for m in router.fallback_candidates(
                tier,
                primary=primary,
                profile=str(_ROUTING_PROFILE.get() or "balanced"),
                allow_live_catalog=self.backend == "openrouter",
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
        so the caller sees the real root cause, not a downstream fallback's noise.

        Three bounds keep a provider-wide outage from stacking into hours of
        serialized attempts (retries x ~23 ranked fallbacks x 120-900s timeouts):
        the lazy extend appends at most ``llm_max_fallbacks`` candidates (0 keeps
        the unbounded ladder), non-primary candidates get at most ONE transient
        retry (the transient budget belongs to the primary), and an optional
        ``llm_call_deadline_seconds`` wall-clock deadline (0 = off) — checked only
        BETWEEN attempts, never cancelling an in-flight request, so a 900s
        reasoning-floor attempt is never truncated — re-raises the ORIGINAL error
        once spent. Fallback resolution can hit the live catalog (blocking
        urllib), so it runs off-loop via ``asyncio.to_thread``."""
        settings = self._routing_settings()
        max_retries = self._retry_budget()
        try:
            limit = float(getattr(settings, "llm_call_deadline_seconds", 0) or 0)
        except (TypeError, ValueError):
            limit = 0.0
        deadline = (time.monotonic() + limit) if limit > 0 else 0.0
        first_exc: BaseException | None = None
        candidates = [primary]
        if self._model_unhealthy(primary):
            fallbacks = await asyncio.to_thread(
                self._healthy_fallback_models, primary, tier
            )
            if fallbacks:
                log.info("llm.model_quarantine_skip",
                         model=primary,
                         to=fallbacks[0],
                         tier=getattr(tier, "value", str(tier)))
                candidates = fallbacks
        extended = False  # the lazy fallback resolve happens at most once
        ci = 0
        while ci < len(candidates):
            if deadline and first_exc is not None and time.monotonic() >= deadline:
                log.warning("llm.call_deadline_spent",
                            limit_s=limit, candidates_tried=ci)
                raise first_exc
            model = candidates[ci]
            model_exc: BaseException | None = None
            # The transient-retry budget belongs to the PRIMARY; re-spending it
            # on every rung of the ladder multiplies an outage's wall clock.
            budget = max_retries if ci == 0 else min(max_retries, 1)
            for attempt in range(budget + 1):
                try:
                    return await attempt_fn(model)
                except Exception as exc:  # noqa: BLE001 - classified for recovery
                    # Ledger write (budget.record) — keep it off the loop; a
                    # contended cross-process lock otherwise blocks every task
                    # sharing this loop for the duration of the transaction.
                    await asyncio.to_thread(
                        self._record_failed_openrouter_attempt,
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
                    if cat == "transient" and attempt < budget:
                        if deadline and time.monotonic() >= deadline:
                            log.warning("llm.call_deadline_spent",
                                        limit_s=limit, candidates_tried=ci + 1)
                            raise first_exc
                        log.info("llm.retry", model=model, attempt=attempt + 1,
                                 reason=_err_reason(exc))
                        await asyncio.sleep(self._retry_delay(attempt))
                        continue
                    break  # transient budget spent, or a model error -> try a fallback
            # Resolve fallbacks lazily the first time a candidate misses, and
            # never re-append a model already tried this call: with exactly
            # one fallback the old extend re-added it, spending 2x(retries+1)
            # paid attempts on the same dead model in a single call. The ladder
            # is capped: past the first few healthy candidates the marginal
            # success probability of an outage-wide walk is ~zero.
            if ci + 1 >= len(candidates) and not extended:
                extended = True
                extra = [
                    m for m in await asyncio.to_thread(
                        self._healthy_fallback_models, primary, tier
                    )
                    if m not in candidates
                ]
                cap = max(0, int(getattr(settings, "llm_max_fallbacks", 4) or 0))
                if cap:
                    extra = extra[:cap]
                candidates.extend(extra)
            # Quarantine EVERY exhausted candidate — including the terminal
            # one, which the old guard skipped, so a permanently broken final
            # candidate burned its full retry budget on every subsequent call.
            self._mark_model_unhealthy(model, model_exc or first_exc)
            if ci + 1 < len(candidates):
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
        provider_override: str | None = None,
        effort: str | None = None,
    ) -> LLMResult:
        """Single completion.

        ``provider_override`` pins THIS call to one backend ("claude_cli",
        "openrouter", ...) independently of the globally active one, which is
        what lets an ensemble fan out across several providers at once. Omitted
        (the default) the call is byte-for-byte what it was before.

        ``effort`` (low|medium|high) is an optional reasoning-effort request,
        used by the MoA council's per-slot pins. On OpenRouter it becomes
        ``{"reasoning": {"effort": X}}`` on the request body; the CLI
        invocation templates carry no effort flag, so there it is a no-op
        (logged once per provider, never an error). ``None`` — the default —
        leaves every request exactly as before.
        """
        # Refuse before dispatch when a persisted/shared counter is already over
        # cap. The post-record check still catches the call that crosses a cap.
        # Off-loop: the ledger guard does synchronous cross-process file
        # locking (up to _LEDGER_LOCK_TIMEOUT_S under contention) plus a JSON
        # re-read; run on the loop thread it stalled every concurrent slice
        # and stream watchdog. to_thread copies the contextvars context, so
        # _BUILD_BUDGET_STATE still resolves to this build's shared state.
        await asyncio.to_thread(self.budget.check)
        # Pass task_type so the LearnedModelRouter can serve per-task picks. It
        # was dead-wired (resolve(tier, file_hint) only) -> the learned router
        # always queried the empty task bucket and could never serve.
        # ``model_override`` pins a specific model (best-of-N cross-model sampling)
        # and normally bypasses the router; the (tier, task_type) bucket is
        # unchanged. ``free_only`` is the hard cost guard: a paid manual/preferred
        # pin is ignored unless it is itself an OpenRouter ":free" model.
        backend = self._effective_backend(provider_override)
        requested_override = (model_override or "").strip()
        # An attached image only matters to the openrouter backend (the only one
        # that speaks the multimodal message shape). stub/CLI ignore it and behave
        # exactly as today — degrade, don't crash (design rule #6).
        if backend == "openrouter":
            # Resolution may refresh the live catalog through blocking urllib —
            # run it off-loop so an 8s+ fetch never freezes every concurrent
            # slice/stream sharing this event loop. (to_thread copies the
            # context, so the _ROUTING_* contextvars still apply.)
            model = await asyncio.to_thread(
                self._resolve_pinned_model,
                tier=tier,
                model_override=requested_override,
                setting_names=("preferred_model",),
                file_hint=file_hint,
                task_type=task_type,
                allow_live_catalog=True,
            )
            vision_override = (
                requested_override if requested_override and model == requested_override else None
            )
            if images:
                model, send_images = self._resolve_vision(model, vision_override)
                if send_images:
                    result = await self._openrouter(
                        model, prompt, system, max_tokens, json_mode, images, tier=tier,
                        effort=effort,
                    )
                else:
                    # free_only with no usable free vision model: stay text-only rather
                    # than silently billing a paid model (design rule #5 cheap-by-default).
                    result = await self._openrouter(
                        model, prompt, system, max_tokens, json_mode, tier=tier,
                        effort=effort,
                    )
            else:
                result = await self._openrouter(
                    model, prompt, system, max_tokens, json_mode, tier=tier, effort=effort
                )
        elif backend.endswith("_cli"):
            if effort:
                self._log_cli_effort_noop(backend[:-4], effort)
            # A local CLI owns its model selection through the signed-in CLI
            # session. Consulting the hosted ModelRouter here neither affects
            # execution nor produces useful evidence; it only led Codex runs to
            # be labelled with a cached OpenRouter model such as GLM.
            # A CLI normally owns its model selection through the signed-in
            # session, but an explicitly PINNED slot (claude_cli:sonnet) must
            # actually reach that model — otherwise two advisor slots on one
            # provider silently collapse to the same model.
            result = await self._cli(
                backend[:-4], prompt, system, json_mode, images, model=requested_override
            )
        else:
            model = self._resolve_pinned_model(
                tier=tier,
                model_override=requested_override,
                setting_names=("preferred_model",),
                file_hint=file_hint,
                task_type=task_type,
                allow_live_catalog=False,
            )
            result = self._stub(model, prompt, system, json_mode)
        tier_s = getattr(tier, "value", str(tier))
        self._record_completion(tier_s, task_type, result.model)
        # One fused off-loop ledger transaction instead of record()+check() —
        # each used to pay its own cross-process lock + JSON read/rewrite,
        # synchronously on the event-loop thread, three transactions per
        # completion in total (with the pre-dispatch check above).
        await asyncio.to_thread(self.budget.record_and_check, result)
        return result

    def _log_cli_effort_noop(self, provider: str, effort: str) -> None:
        """Record that a requested reasoning effort cannot reach a CLI backend.

        The headless invocation templates (_CLI_COMMANDS/_CLI_PROMPT_FLAG) have
        no effort flag for ANY provider, so a pinned effort is silently
        droppable only if we say so — log it ONCE per provider per client (an
        MoA council fans the same slot out build after build) and proceed with
        the CLI's own session default. Never an error: degrade, don't crash.
        """
        if provider in self._cli_effort_noop_logged:
            return
        self._cli_effort_noop_logged.add(provider)
        log.warning(
            "llm.cli_effort_noop",
            provider=provider,
            effort=effort,
            note="this CLI's invocation has no reasoning-effort flag; using its session default",
        )

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

        settings = self._routing_settings()
        configured = str(getattr(settings, "vision_model", "") or "").strip()
        no_claude = bool(getattr(settings, "no_claude", False))
        free_only = bool(getattr(settings, "free_only", False))

        if configured:  # operator opted into a specific vision model — honor it
            if no_claude and _is_claude(configured):
                return resolved, False  # forbidden by policy -> drop the image
            if free_only and not _is_free(configured):
                # DELIBERATE exception to free_only, not a cost-guard gap
                # (audit finding M10 was rejected): configuring vision_model
                # at all IS the explicit spend opt-in for image calls, because
                # free_only is the DEFAULT posture and a vision call without a
                # vision model silently drops the image instead. Pinned by
                # test_openrouter_image_keeps_vision_model_under_free_only.
                # The spend stays visible via this warning.
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

    async def _cli(self, provider, prompt, system, json_mode, images=None, *,
                   model: str = "") -> LLMResult:
        """Run a locally-installed coding-agent CLI in headless print mode.

        Degrades to the stub backend (never raises) if the CLI fails or times
        out, so a build keeps moving (design rule #6).

        ``model`` pins the CLI's model (``--model <m>``) the same way
        ``agentic_build`` already does. Without it a pinned slot such as
        ``claude_cli:sonnet`` would silently run the CLI's default model, and two
        advisor slots on one provider would collapse into the same model.
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
        # The Windows standalone substitution is a Codex-specific sandbox
        # recovery. Other CLIs keep their documented command token so their
        # invocation contracts remain stable.
        # Resolve a real executable for EVERY provider, not just Codex. On
        # Windows a bare "claude" is unrunnable (npm ships claude.ps1, and
        # CreateProcess cannot exec a PowerShell script) — see
        # _windows_executable_shim.
        command = self._cli_executable(provider) or provider
        command_template = list(_CLI_COMMANDS.get(provider, [provider, "-p"]))
        if command_template:
            command_template[0] = command
        # Mirrors agentic_build's per-call pin (see its ``model_args``). Ignored
        # when no model is given, so the CLI's own default still applies.
        model_pin = str(model or "").strip()
        model_args = (
            ["--model", model_pin]
            if (model_pin and provider in _KNOWN_CLI_PROVIDERS)
            else []
        )
        argv = [
            *command_template,
            *model_args,
            *_no_mcp_args(self.settings, provider),
        ]
        stdin_prompt = provider in _CLI_STDIN_PROMPT
        if provider == "codex":
            for path in paths:
                argv.extend(("--image", path))
            # stdin avoids Windows' command-line length ceiling for large
            # architecture/review prompts. A literal '-' is Codex's documented
            # noninteractive stdin prompt form.
            argv.append("-")
        elif stdin_prompt:
            pass  # prompt goes over stdin; no positional argument
        else:
            # kimi/copilot cannot read the prompt from stdin, and in print
            # mode file-read tools are not guaranteed, so there is no safe
            # alternate transport here. When argv cannot carry the prompt
            # intact (cmd.exe shim re-parse / CreateProcess ceiling — see
            # _argv_prompt_hazard) fail LOUDLY: a silently truncated prompt
            # produces a plausible reply that counts as success, which is
            # exactly how a mangled MoA advisor once poisoned codegen.
            hazard = _argv_prompt_hazard(command, full)
            if hazard:
                log.warning("llm.cli_prompt_exceeds_argv_limit",
                            provider=provider, reason=hazard,
                            prompt_chars=len(full))
                for tp in temp_images:
                    try:
                        os.unlink(tp)
                    except OSError:
                        pass
                return self._failed_cli_fallback(
                    provider, prompt, system, json_mode,
                    status="failed_cli_prompt_too_large",
                )
            # The prompt flag (when the provider has one) goes LAST, immediately
            # before the prompt, so nothing can be swallowed as its value.
            prompt_flag = _CLI_PROMPT_FLAG.get(provider)
            if prompt_flag:
                argv.append(prompt_flag)
            argv.append(full)
        proc = None
        try:
            # Admission is taken BEFORE the subprocess spawns and before the
            # wait_for clock starts: if queue time counted against the model's
            # timeout, a queued call would get a truncated budget and look like a
            # model failure rather than a busy machine.
            async with provider_slot(
                f"{provider}_cli", resolve_provider_limit(self.settings, f"{provider}_cli")
            ):
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE if stdin_prompt else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,  # own process group -> killable as a tree
                    env=_cli_subprocess_env(),
                )
                communicate = (
                    proc.communicate(full.encode("utf-8"))
                    if stdin_prompt
                    else proc.communicate()
                )
                out, err = await asyncio.wait_for(
                    communicate, timeout=apply_reasoning_floor(
                        self.settings.cli_llm_timeout, model_pin
                    )
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
        # Only a PINNED run reports the model, so the unpinned label stays
        # exactly "<provider>-cli" as every existing consumer expects.
        label = f"{provider}-cli:{model_pin}" if model_pin else f"{provider}-cli"
        return LLMResult(
            text=text, model=label, backend=f"{provider}_cli",
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
        idle_timeout = _resolved_agentic_idle_timeout(
            self.settings,
            int(getattr(self.settings, "agentic_build_timeout", 0))
            or (self.settings.cli_llm_timeout * 3),
        )
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
        # Bounded tail of assistant prose (vent channel) — friction reports ride
        # the message text, not tool calls, so keep just enough to parse them.
        output_text_parts: list[str] = []
        output_text_budget = [_AGENTIC_OUTPUT_TEXT_LIMIT]
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
                        # A slow-thinking reasoning model can exceed the flat 180s
                        # client timeout mid-think -> ReadTimeout -> classified
                        # transient -> every retry burns identically (the exact
                        # failure reasoning_timeouts was written for; the plain
                        # path already floors at llm.py _openrouter). The floor
                        # only ever RAISES the ceiling, and only for known slow
                        # families — everything else keeps 180 exactly. The idle
                        # watchdog is floored the same way, per-model, so it
                        # cannot kill the very turn the request floor allows;
                        # no-floor models keep the operator's window untouched.
                        request_timeout = apply_reasoning_floor(180, m)
                        floor = reasoning_timeout_floor(m)
                        watchdog = (
                            max(idle_timeout, float(floor)) if floor else idle_timeout
                        )

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
                            resp = await client.post(
                                OPENROUTER_URL,
                                json=body,
                                headers=headers,
                                timeout=request_timeout,
                            )
                            resp.raise_for_status()
                            return resp, m

                        if idle_timeout > 0:
                            try:
                                return await asyncio.wait_for(_post(), timeout=watchdog)
                            except TimeoutError as exc:
                                nonlocal turn_timeouts, stall_reason
                                turn_timeouts += 1
                                stall_reason = (
                                    f"OpenRouter agentic turn stalled after {watchdog:g}s"
                                )
                                log.warning(
                                    "llm.or_agentic_stalled",
                                    model=m,
                                    turn=_turn,
                                    idle_s=watchdog,
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
                                # Fallbacks must come from the SAME quality class the
                                # primary was resolved at (Tier.BACKEND codegen — see
                                # agentic_build): Tier.CHEAP here imposed the hard
                                # $1/M price gate + free ladder, pinning whole-app
                                # codegen to a bargain model after one 404/stall.
                                Tier.BACKEND,
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
                        # Off-loop for the same reason as _resilient_call's
                        # failed-attempt accounting: the ledger transaction
                        # must not block the event loop.
                        await asyncio.to_thread(
                            self._record_failed_openrouter_attempt,
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
                    _capture_agentic_output_text(
                        {"type": "assistant", "message": msg},
                        output_text_parts,
                        output_text_budget,
                    )
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
                                # Output-side secret masking: a read_file /
                                # list_files observation can echo a credential
                                # that reached the worktree; scrub it before the
                                # text re-enters the message history sent back
                                # to the provider (and any persisted context).
                                result = _mask_agentic_text(result)
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
                 "output_text": "\n".join(output_text_parts),
                 "error": error}

    @property
    def supports_agentic(self) -> bool:
        """Whole-project codegen: CLI backends are coding agents; OpenRouter gets the
        agentic tool-loop when ``openrouter_agentic`` is on (cheap models, full app)."""
        b = self.backend
        return b.endswith("_cli") or (
            b == "openrouter"
            and bool(getattr(self._routing_settings(), "openrouter_agentic", True))
        )

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
        cortex_safe: bool = False,
    ) -> dict:
        """Run a local coding-agent CLI that writes files directly into workdir.

        This is the RIGHT way to use claude/kimi/copilot for codegen: one
        agentic session that authors a coherent multi-file app, instead of N
        slow per-file completion calls that spin up an agent each and time out.
        ``model`` optionally pins the CLI to a specific model (used by parallel
        code-slicing to route cheap/strong tiers per slice). ``provider`` forces a
        specific CLI (e.g. "claude") regardless of the global backend — this is how
        codegen-only CLI routing works: the global backend stays cheap (OpenRouter)
        while ONLY codegen runs on the claude CLI. ``cortex_safe`` restricts local
        candidate authoring to an isolated, no-network Codex workspace; OpenRouter's
        internal confined tool loop remains available. Returns {ok, backend, error}.
        """
        backend = self.backend
        try:
            self.budget.check()
        except BudgetExceeded as exc:
            return {"ok": False, "backend": backend, "error": str(exc)}

        locked = _BUILD_ROUTING.get()
        locked_codegen = (
            locked.get("codegen")
            if isinstance(locked, dict) and isinstance(locked.get("codegen"), dict)
            else None
        )
        locked_codegen_backend = ""
        if isinstance(locked_codegen, dict):
            locked_codegen_backend = str(
                locked_codegen.get("effective_backend") or ""
            ).strip().lower()
            if locked_codegen_backend.endswith("_cli"):
                provider = locked_codegen_backend[:-4]
            else:
                provider = ""
            effective_label = str(
                locked_codegen.get("effective_model") or ""
            ).strip()
            requested_label = str(
                locked_codegen.get("requested_model") or ""
            ).strip()
            codegen_source = str(locked_codegen.get("source") or "").strip()
            # Preserve internal per-slice model choices when the operator left
            # codegen on automatic routing. A GUI/settings pin, or an explicit
            # codegen CLI provider (whose empty model means that CLI's default),
            # remains authoritative for the whole build.
            if requested_label or codegen_source == "codegen_cli_pin":
                if (
                    effective_label in {"", "router:auto", "offline-stub"}
                    or effective_label.endswith("-cli:default")
                ):
                    model = None
                else:
                    model = effective_label

        provider = str(
            provider or (backend[:-4] if backend.endswith("_cli") else "")
        ).lower()
        if provider and not model and locked_codegen is None:
            model = str(getattr(self.settings, "codegen_cli_model", "") or "").strip() or None
        if not provider:
            if locked_codegen_backend == "stub":
                return {
                    "ok": False,
                    "backend": "stub",
                    "error": "agentic unsupported",
                }
            # No CLI agent: OpenRouter models get the agentic tool-loop (cheap,
            # whole-project codegen) instead of the weak per-file path.
            if backend == "openrouter" and bool(
                getattr(self._routing_settings(), "openrouter_agentic", True)
            ):
                # Off-loop: the resolve can hit the live catalog via blocking
                # urllib (see complete()).
                m = await asyncio.to_thread(
                    self._resolve_pinned_model,
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
        if cortex_safe and provider != "codex":
            return {
                "ok": False,
                "backend": f"{provider}_cli",
                "error": (
                    "Cortex-safe local candidate authoring requires Codex CLI; "
                    f"{provider or 'unknown'} CLI is not permitted."
                ),
            }
        if not self._cli_available(provider):
            return {"ok": False, "backend": backend, "error": "agentic unsupported"}
        # acceptEdits lets the headless agent write files without prompting.
        # _no_mcp_args keeps the agent from loading the host's ambient MCP fleet.
        nm = _no_mcp_args(self._routing_settings(), provider)
        if cortex_safe and provider == "codex" and "--ignore-user-config" not in nm:
            nm.append("--ignore-user-config")
        codex_windows_sandbox = _isolated_codex_windows_sandbox_args(nm)
        cortex_codex_sandbox = (
            ["-c", "sandbox_workspace_write.network_access=false"]
            if cortex_safe and provider == "codex"
            else []
        )
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
        cli_command = self._cli_executable(provider) or provider
        # Providers in _CLI_STDIN_PROMPT take the prompt over stdin and must NOT
        # carry it on argv — see the constant's docstring. This matters most
        # here: the codegen prompt is the largest in the system, so a cmd.exe
        # shim truncating it fails silently, returning a plausible short reply
        # that counts as a successful build.
        stdin_prompt = provider in _CLI_STDIN_PROMPT
        # kimi/copilot cannot take stdin (see _CLI_PROMPT_FILENAME's comment),
        # so when argv cannot carry the prompt intact it is handed over via a
        # transient file in its own temp dir plus a newline-free bootstrap
        # prompt on argv. Both CLIs run agentically here with file-read tools
        # and get the temp dir allow-listed via --add-dir, so the handoff is
        # reliable — and the file never touches the collected app workdir.
        prompt_file = ""
        prompt_file_args: list[str] = []
        argv_prompt = prompt
        if provider in ("kimi", "copilot") and _argv_prompt_hazard(cli_command, prompt):
            try:
                prompt_dir = tempfile.mkdtemp(prefix="skyn3t_agentic_prompt_")
                candidate = os.path.join(prompt_dir, _CLI_PROMPT_FILENAME)
                with open(candidate, "w", encoding="utf-8") as fh:
                    fh.write(prompt)
            except OSError as exc:
                # Do NOT fall back to argv: a hazardous prompt on argv is the
                # exact silent-truncation corruption this transport prevents.
                # Fail the build loudly instead.
                log.warning("llm.agentic_prompt_file_write_failed",
                            provider=provider, error=str(exc)[:120])
                self._record_failed_cli_attempt(
                    provider, prompt, status="failed_cli_prompt_file"
                )
                return {
                    "ok": False,
                    "completed": False,
                    "timed_out": False,
                    "backend": f"{provider}_cli",
                    "model": model or f"{provider}-cli",
                    "account_source": "local_cli_session",
                    "cost_source": "not_reported_by_cli",
                    "cost_usd": None,
                    "cli_execution": _new_cli_execution_evidence(
                        provider,
                        streamed=False,
                        cli_version=self._cached_cli_version(provider),
                    ),
                    "error": (
                        "could not stage the agentic prompt file: "
                        f"{str(exc)[:120]}"
                    ),
                }
            else:
                prompt_file = candidate
                prompt_file_args = ["--add-dir", prompt_dir]
                argv_prompt = _prompt_file_bootstrap(prompt_file)
                log.info("llm.agentic_prompt_via_file", provider=provider,
                         prompt_chars=len(prompt),
                         reason=_argv_prompt_hazard(cli_command, prompt))
        argv = {
            "claude": [cli_command, "-p", *([] if stdin_prompt else [prompt]),
                       "--permission-mode", "acceptEdits",
                       "--setting-sources", "project", *model_args, *stream_args, *nm],
            "codex": [
                cli_command, "exec", "--ephemeral", "--sandbox", "workspace-write",
                "--color", "never", "--skip-git-repo-check", "--json",
                *codex_windows_sandbox, *cortex_codex_sandbox,
                "--cd", workdir, *model_args, *nm, "-",
            ],
            # Kimi's noninteractive form is `-p/--prompt <text>`; it has no
            # `--print` flag (passing one exits nonzero with "unknown option"),
            # and no documented disable-all ambient-MCP flag.
            "kimi": [cli_command, "--prompt", argv_prompt, *model_args,
                     *stream_args, *prompt_file_args],
            "copilot": [
                cli_command, "-p", argv_prompt, "--allow-all-tools", "--no-ask-user",
                "--no-auto-update", "--no-custom-instructions", *model_args, *nm,
                *prompt_file_args,
            ],
        }.get(provider, [cli_command, "-p", prompt])
        # This is deliberately a compact execution receipt, not a raw CLI log.
        # The latter can contain source files, user prompts, or tool output and
        # does not belong in a durable build manifest.
        cached_cli_version = self._cached_cli_version(provider)
        cli_execution = _new_cli_execution_evidence(
            provider,
            streamed=stream,
            cli_version=cached_cli_version,
        )
        stream_evidence_token = _AGENTIC_STREAM_EVIDENCE.set(None)
        proc = None
        # Bounded tail of the agent's prose output (vent channel). Never stored
        # inside cli_execution — that receipt is persisted in durable manifests
        # and must not carry raw agent prose.
        output_text = ""
        agentic_timeout = (
            timeout
            or int(getattr(self.settings, "agentic_build_timeout", 0))
            or (self.settings.cli_llm_timeout * 3)
        )
        build_started = time.monotonic()
        stall_heals_used = 0
        stall_healed_kind = ""
        try:
            # Stall auto-heal loop. Normally one pass; a healable typed stall
            # (prompt_misdelivery / prompt_acceptance_timeout) resends the SAME
            # prompt as a fresh session ONCE within this call — code_agent's own
            # retry layer still sees a single consumed attempt.
            while True:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=workdir,
                    stdin=asyncio.subprocess.PIPE if stdin_prompt else None,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,  # own process group -> killable as a tree
                    env=_cli_subprocess_env(),
                    # A single stream-json event line (a big tool result, or the final
                    # `result` event carrying the whole output) routinely exceeds the
                    # 64KB asyncio StreamReader default, which makes readline() raise
                    # "Separator is found, but chunk is longer than limit" and degrades
                    # the whole build. Give it real headroom (grows on demand, not
                    # preallocated).
                    limit=_AGENTIC_STREAM_LIMIT,
                )
                cli_execution["exit_status"] = "running"
                if stdin_prompt and proc.stdin is not None:
                    proc.stdin.write(prompt.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                # For streaming CLIs this is an INACTIVITY fallback, not a total
                # duration ceiling. Every stream event proves the agent is alive and
                # resets _consume_agentic_stream's idle wait, so productive full-app
                # builds may continue until their terminal result event.
                if stream:
                    idle_timeout = int(
                        _resolved_agentic_idle_timeout(self.settings, agentic_timeout)
                    )
                    if stall_heals_used:
                        # The heal reshare is clamped to the build's REMAINING
                        # budget, so a healed build cannot silently double its
                        # wall-clock (defaults: 600s idle inside an 1800s build
                        # budget leaves a full second window).
                        remaining_s = float(agentic_timeout) - (
                            time.monotonic() - build_started
                        )
                        idle_timeout = int(max(1, min(idle_timeout, remaining_s)))
                    # The attempt counter rides a task-local so this call keeps
                    # its exact 3-argument contract for mocked consumers.
                    _AGENTIC_STREAM_ATTEMPT.set(stall_heals_used)
                    ok = await self._consume_agentic_stream(proc, provider, idle_timeout)
                    observed_execution = _AGENTIC_STREAM_EVIDENCE.get()
                    if isinstance(observed_execution, dict):
                        cli_execution = observed_execution
                        if cached_cli_version and not cli_execution.get("cli_version"):
                            cli_execution["cli_version"] = cached_cli_version
                    # Lift the bounded prose tail OUT of the receipt before the
                    # receipt can be persisted (see the output_text comment above).
                    output_text = str(cli_execution.pop("output_text", "") or "")
                    # Keep older test/custom consumers that return only a bool
                    # compatible. The native consumer always supplies this detail.
                    if cli_execution.get("exit_status") == "running":
                        returncode = getattr(proc, "returncode", None)
                        cli_execution["exit_code"] = (
                            returncode if isinstance(returncode, int) and not isinstance(returncode, bool)
                            else None
                        )
                        cli_execution["exit_status"] = (
                            "exited" if returncode is not None else "not_reaped"
                        )
                    if stall_healed_kind:
                        # Forensic marker on the FINAL receipt: this build healed
                        # a stall once before reaching its outcome.
                        cli_execution["stall_heal_attempted"] = True
                        cli_execution["stall_heal_kind"] = stall_healed_kind
                    stall_kind = str(cli_execution.get("stall_kind") or "")
                    if (
                        not ok
                        and stall_heals_used < _STALL_MAX_HEALS
                        and stall_kind in _STALL_HEALABLE_KINDS
                    ):
                        remaining_s = float(agentic_timeout) - (
                            time.monotonic() - build_started
                        )
                        if remaining_s >= _STALL_HEAL_MIN_REMAINING_S:
                            stall_heals_used += 1
                            stall_healed_kind = stall_kind
                            log.warning(
                                "llm.agentic_stall_heal", provider=provider,
                                stall_kind=stall_kind, remaining_s=int(remaining_s),
                            )
                            # The stalled session is already terminated (idle
                            # guard) or exited (EOF); resend as a fresh session
                            # with a fresh receipt.
                            cli_execution = _new_cli_execution_evidence(
                                provider,
                                streamed=True,
                                cli_version=cached_cli_version,
                            )
                            continue
                else:
                    out, err = await asyncio.wait_for(
                        proc.communicate(), timeout=agentic_timeout,
                    )
                    ok = proc.returncode == 0
                    # Mask on the way out: the raw CLI stdout tail rides the
                    # returned output_text (vent channel / run metadata).
                    output_text = _mask_agentic_text(
                        (out or b"").decode("utf-8", "replace")[
                            -_AGENTIC_OUTPUT_TEXT_LIMIT:
                        ]
                    )
                    returncode = getattr(proc, "returncode", None)
                    cli_execution["exit_code"] = (
                        returncode if isinstance(returncode, int) and not isinstance(returncode, bool)
                        else None
                    )
                    cli_execution["exit_status"] = (
                        "exited" if returncode is not None else "not_reaped"
                    )
                    if not ok:
                        log.warning("llm.agentic_nonzero", provider=provider,
                                    err=(err or b"").decode("utf-8", "replace")[:160])
                break
        except TimeoutError:
            # Copilot uses the blocking path. Its watchdog is a total timeout;
            # streamed CLI watchdogs are recorded by the stream consumer as an
            # idle timeout instead.
            cli_execution["timed_out"] = True
            cli_execution["timeout_kind"] = "total"
            cli_execution["termination_reason"] = "total_timeout"
            await self._terminate(proc)
            returncode = getattr(proc, "returncode", None)
            cli_execution["exit_code"] = (
                returncode if isinstance(returncode, int) and not isinstance(returncode, bool)
                else None
            )
            cli_execution["exit_status"] = (
                "terminated_after_total_timeout"
                if returncode is not None
                else "termination_requested_after_total_timeout"
            )
            self._record_failed_cli_attempt(
                provider, prompt, status="failed_cli_timeout"
            )
            log.warning("llm.agentic_timeout", provider=provider, timeout=agentic_timeout)
            return {
                "ok": False,
                "completed": False,
                "timed_out": True,
                "backend": f"{provider}_cli",
                "model": model or f"{provider}-cli",
                "account_source": "local_cli_session",
                "cost_source": "not_reported_by_cli",
                "cost_usd": None,
                "cli_execution": cli_execution,
                "output_text": output_text,
                "error": "agentic CLI timed out",
            }
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
            observed_execution = _AGENTIC_STREAM_EVIDENCE.get()
            if isinstance(observed_execution, dict):
                cli_execution = observed_execution
                if cached_cli_version and not cli_execution.get("cli_version"):
                    cli_execution["cli_version"] = cached_cli_version
            output_text = str(cli_execution.pop("output_text", "") or output_text)
            self._record_failed_cli_attempt(
                provider, prompt, status="failed_cli_exception"
            )
            log.warning("llm.agentic_failed", provider=provider, error=str(exc)[:160])
            returncode = getattr(proc, "returncode", None)
            cli_execution["termination_reason"] = "spawn_failed" if proc is None else "exception"
            cli_execution["exit_code"] = (
                returncode if isinstance(returncode, int) and not isinstance(returncode, bool)
                else None
            )
            cli_execution["exit_status"] = (
                "spawn_failed"
                if proc is None
                else "terminated_after_exception"
                if returncode is not None
                else "termination_requested_after_exception"
            )
            return {
                "ok": False,
                "completed": False,
                "timed_out": False,
                "backend": f"{provider}_cli",
                "model": model or f"{provider}-cli",
                "account_source": "local_cli_session",
                "cost_source": "not_reported_by_cli",
                "cost_usd": None,
                "cli_execution": cli_execution,
                "output_text": output_text,
                "error": str(exc)[:160],
            }
        finally:
            _AGENTIC_STREAM_EVIDENCE.reset(stream_evidence_token)
            if prompt_file:
                # The transient prompt handoff (file + its private temp dir)
                # must not outlive the invocation on any exit path.
                try:
                    os.unlink(prompt_file)
                    os.rmdir(os.path.dirname(prompt_file))
                except OSError:
                    pass
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
        result = {
            "ok": ok,
            "completed": ok,
            "backend": f"{provider}_cli",
            "model": effective_model,
            "account_source": "local_cli_session",
            "cost_source": "not_reported_by_cli",
            "cost_usd": None,
            "timed_out": bool(cli_execution.get("timed_out")),
            "cli_execution": cli_execution,
            "output_text": output_text,
            "error": "" if ok else "agentic CLI ended without a successful result",
        }
        # Typed stall surfacing (additive): a classified stall rides the error
        # result so code_agent / the dashboard can show "stalled: transport_dead"
        # instead of a bare timeout. Absent on stall-free builds, keeping their
        # result byte-identical to before.
        stall_kind = str(cli_execution.get("stall_kind") or "")
        if stall_kind:
            result["stall_kind"] = stall_kind
            result["stall_evidence"] = str(cli_execution.get("stall_evidence") or "")
        return result

    async def _consume_agentic_stream(self, proc, provider: str, idle_timeout: int) -> bool:
        """Drive a stream-json agentic CLI to completion and report success.

        Reads the NDJSON event stream to EOF (the agent closes stdout when it
        exits), capturing the terminal ``result`` event's error flag for an
        accurate success signal. When ``idle_timeout`` > 0, a gap of that many
        seconds with NO stream activity means the agent has stalled — we kill the
        whole tree and report failure. Each event resets that wait, so there is no
        total-duration ceiling on productive work. stderr is drained concurrently
        so a chatty agent cannot deadlock on a full pipe. Returns ``ok``.

        A fixed, task-local receipt is retained in ``_AGENTIC_STREAM_EVIDENCE``
        for ``agentic_build``. The method intentionally keeps its boolean return
        contract so existing integrations that mock or call it directly remain
        compatible.

        When the stream fails in a way that looks like a stall (the idle guard
        fires, or the transport dies mid-stream without a terminal result), a
        typed stall classification (``stall_kind`` + ``stall_evidence``, see
        :mod:`skyn3t.adapters.stall`) is attached to that receipt — additive
        keys only, so receipt consumers that predate them are unaffected. The
        heal-attempt counter rides the task-local ``_AGENTIC_STREAM_ATTEMPT``.
        """
        saw_result = False
        result_is_error = False
        events = 0
        timed_out = False
        terminated = False
        stream_overrun = False
        evidence = _new_cli_execution_evidence(provider, streamed=True)
        evidence["exit_status"] = "streaming"
        output_parts: list[str] = []
        output_budget = [_AGENTIC_OUTPUT_TEXT_LIMIT]
        # Stall-evidence tracking: bytes/timing/content counters + bounded raw
        # tails (raw stdout lines that are not stream-json events are where a
        # CLI's interactive trust/approval prompt shows up).
        bytes_received = 0
        content_events = 0
        raw_tail: list[str] = []

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

        # Liveness heartbeat. Stream events are the only progress evidence a CLI
        # agent produces, and nothing surfaced them — a measured build ran 24
        # minutes between log lines while working normally, which reads exactly
        # like a hang. Emitting a periodic line costs nothing and makes "is it
        # running?" answerable from the log alone.
        heartbeat_every = float(getattr(self.settings, "agentic_heartbeat_seconds", 60) or 0)
        stream_started = time.monotonic()
        last_beat = stream_started
        last_output_at = stream_started

        err_task = asyncio.create_task(_drain_err())
        try:
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
                        # Stall detected. Classify BEFORE killing the tree: the
                        # process is still alive here (a wait_for timeout means
                        # no EOF arrived), which is what separates the live
                        # kinds (trust/misdelivery/acceptance/heartbeat) from a
                        # dead transport or a crash.
                        quiet_s = max(
                            time.monotonic() - last_output_at, float(idle_timeout)
                        )
                        report = _agentic_stall_report(
                            provider=provider,
                            lifecycle_state="streaming",
                            bytes_received=bytes_received,
                            events_received=events,
                            content_events=content_events,
                            seconds_since_last_output=quiet_s,
                            prompt_sent_at=stream_started,
                            process_alive=True,
                            exit_code=None,
                            idle_timeout=float(idle_timeout or 0),
                            output_tail="\n".join([*err_tail, *raw_tail, *output_parts]),
                            stderr_tail="".join(err_tail),
                            attempt=_AGENTIC_STREAM_ATTEMPT.get(),
                        )
                        evidence["stall_kind"] = report["stall_kind"]
                        evidence["stall_evidence"] = report["stall_evidence"]
                        log.warning(
                            "llm.agentic_stalled", provider=provider,
                            idle_s=idle_timeout, stall_kind=report["stall_kind"],
                            stall_evidence=report["stall_evidence"][:240],
                        )
                        timed_out = True
                        terminated = True
                        evidence["timed_out"] = True
                        evidence["timeout_kind"] = "idle"
                        evidence["termination_reason"] = "idle_timeout"
                        await self._terminate(proc)
                        break
                    except ValueError:
                        # Defence in depth: a single line past even the 64MB buffer
                        # makes readline() raise. Kill the tree (else the agent can
                        # block on a full stdout pipe we've stopped draining) and fall
                        # back to the returncode below.
                        log.warning("llm.agentic_stream_overrun", provider=provider)
                        stream_overrun = True
                        terminated = True
                        evidence["termination_reason"] = "stream_overrun"
                        await self._terminate(proc)
                        break
                    if not line:
                        break  # EOF — the agent exited
                    events += 1
                    evidence["event_count"] = events
                    bytes_received += len(line)
                    _now = time.monotonic()
                    last_output_at = _now
                    if heartbeat_every > 0:
                        if _now - last_beat >= heartbeat_every:
                            last_beat = _now
                            log.info(
                                "llm.agentic_progress", provider=provider,
                                events=events, elapsed_s=int(_now - stream_started),
                            )
                    try:
                        evt = json.loads(line)
                    except (ValueError, TypeError):
                        evidence["invalid_line_count"] = (
                            int(evidence.get("invalid_line_count", 0)) + 1
                        )
                        raw_tail.append(line.decode("utf-8", "replace")[:200])
                        if len(raw_tail) > 10:
                            del raw_tail[:-10]
                        continue  # stray non-JSON log line — ignore
                    if not isinstance(evt, dict):
                        evidence["invalid_line_count"] = (
                            int(evidence.get("invalid_line_count", 0)) + 1
                        )
                        raw_tail.append(line.decode("utf-8", "replace")[:200])
                        if len(raw_tail) > 10:
                            del raw_tail[:-10]
                        continue
                    _record_cli_execution_event(evidence, evt)
                    _capture_agentic_output_text(evt, output_parts, output_budget)
                    if str(evt.get("type") or "") not in _AGENTIC_LIFECYCLE_EVENT_TYPES:
                        content_events += 1
                    if evt.get("type") == "result":
                        saw_result = True
                        result_is_error = bool(evt.get("is_error"))
            except asyncio.CancelledError:
                evidence["termination_reason"] = "cancelled"
                evidence["exit_status"] = "cancelled"
                raise
            finally:
                err_task.cancel()
                try:
                    await err_task
                except asyncio.CancelledError:
                    pass

            # stdout EOF means the agent is exiting; reap it (bounded) so
            # returncode is set for the fallback success check. _terminate()
            # already reaps the process on an idle/overrun abort.
            if not terminated:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except TimeoutError:
                    evidence["termination_reason"] = "reap_timeout"
                except Exception:  # noqa: BLE001 - never hang on reap
                    pass

            returncode = getattr(proc, "returncode", None)
            evidence["exit_code"] = (
                returncode if isinstance(returncode, int) and not isinstance(returncode, bool)
                else None
            )
            if timed_out:
                evidence["exit_status"] = (
                    "terminated_after_idle_timeout"
                    if returncode is not None
                    else "termination_requested_after_idle_timeout"
                )
            elif stream_overrun:
                evidence["exit_status"] = (
                    "terminated_after_stream_overrun"
                    if returncode is not None
                    else "termination_requested_after_stream_overrun"
                )
            elif returncode is None:
                evidence["exit_status"] = "not_reaped"
            else:
                evidence["exit_status"] = "exited"

            # Preserve the pre-existing success semantics: a terminal `result`
            # error wins; Codex's `turn.completed` is useful evidence but process
            # exit status remains its source of truth.
            ok = False if timed_out else (
                (not result_is_error) if saw_result else (returncode == 0)
            )
            evidence["ok"] = ok
            if not ok and not timed_out and not stream_overrun and not saw_result:
                # The transport ended WITHOUT a terminal result event: the
                # process exited on its own (crash / mid-stream death), or the
                # channel closed before the prompt was ever accepted. Classify
                # it — the idle-guard branch above already covered live stalls.
                report = _agentic_stall_report(
                    provider=provider,
                    lifecycle_state=str(evidence.get("exit_status") or ""),
                    bytes_received=bytes_received,
                    events_received=events,
                    content_events=content_events,
                    seconds_since_last_output=time.monotonic() - last_output_at,
                    prompt_sent_at=stream_started,
                    process_alive=False,  # stdout hit EOF: channel is closed
                    exit_code=returncode if isinstance(returncode, int) else None,
                    idle_timeout=float(idle_timeout or 0),
                    output_tail="\n".join([*err_tail, *raw_tail, *output_parts]),
                    stderr_tail="".join(err_tail),
                    attempt=_AGENTIC_STREAM_ATTEMPT.get(),
                )
                evidence["stall_kind"] = report["stall_kind"]
                evidence["stall_evidence"] = report["stall_evidence"]
            if not ok:
                log.warning("llm.agentic_nonzero", provider=provider,
                            events=events, err="".join(err_tail)[-160:],
                            stall_kind=evidence.get("stall_kind") or "")
            return ok
        finally:
            # Always leave the caller a bounded receipt, including cancellation
            # and unexpected reader errors. No raw event line is retained. The
            # bounded assistant-prose tail (vent channel) rides the receipt only
            # as far as ``agentic_build``, which pops it before persisting.
            if output_parts:
                evidence["output_text"] = "\n".join(output_parts)
            _AGENTIC_STREAM_EVIDENCE.set(evidence)

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
                          tier: Tier = Tier.CHEAP,
                          effort: str | None = None) -> LLMResult:
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
            # Reasoning-effort passthrough (the MoA council pins it per advisor
            # slot). Safe by construction: the value is clamped to the three
            # public levels, and OpenRouter IGNORES `reasoning` for models
            # without a reasoning mode — so this can never break a call over an
            # unsupported knob.
            eff = (effort or "").strip().lower()
            if eff in _REASONING_EFFORT_LEVELS:
                body["reasoning"] = {"effort": eff}
            # A reasoning model can think silently for longer than the flat 120s
            # this used to hard-code, dying mid-think -> transport timeout ->
            # classified transient -> every retry burns the same way. The floor
            # only ever RAISES the ceiling, and only for known slow families.
            request_timeout = apply_reasoning_floor(120, m)
            async with provider_slot(
                "openrouter", resolve_provider_limit(self.settings, "openrouter")
            ):
                # Shared per-loop keep-alive client — no fresh TCP+TLS
                # handshake per attempt. The reasoning floor rides the
                # per-request ``timeout=`` override, keeping per-model
                # semantics identical on the shared client.
                client = _shared_openrouter_client()
                resp = await client.post(
                    OPENROUTER_URL, json=body, headers=headers, timeout=request_timeout
                )
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
                f"Install Codex CLI or select a backend explicitly for real generation.\n"
                f"Prompt summary: {prompt[:160]}"
            )
        approx = max(1, len(prompt) // 4)
        return LLMResult(text=text, model=model, backend="stub",
                         prompt_tokens=approx, completion_tokens=len(text) // 4, cost_usd=0.0)
