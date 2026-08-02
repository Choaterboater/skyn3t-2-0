"""CodeAgent — writes actual source files into the worktree.

This is the most important generative agent. It:

* detects the stack from the plan/brief (default ``react_vite``);
* with a real LLM backend, generates each planned file from a per-file prompt,
  routing the tier by file extension via ``file_hint`` so frontend files use the
  UI tier and backend files use the BACKEND tier;
* with the OFFLINE stub backend, emits a real, runnable minimal scaffold for the
  detected stack (e.g. a working Vite+React counter app) so an offline
  ``skyn3t studio build`` produces a genuinely runnable project (design rule #1);
* repairs common entrypoint import/export mismatches before returning.

All file writes go under ``task.payload["worktree_dir"]`` (or a temp dir if
absent) and are confined to that directory.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import structlog

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import (
    canonical_project_relpath,
    detect_stack,
    extract_code,
    knowledge_block,
    slugify,
)
from skyn3t.agents._scaffold import (
    _implies_cli_agent,
    _implies_finance,
    _implies_llm_gateway,
    _implies_market_data,
    _implies_memory_chat,
    _implies_supabase,
    default_pyproject,
    scaffold_for,
    synthesize_python_entrypoint,
)
from skyn3t.core.agent import AgentCapability, AgentStatus, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.model_router import Tier
from skyn3t.studio.acceptance_contract import (
    restore_acceptance_contracts,
    snapshot_acceptance_contracts,
)
from skyn3t.studio.design_tokens import design_md_block
from skyn3t.studio.layout_profiles import LayoutProfile, layout_contract_block, profile_from_payload
from skyn3t.worktree import list_files

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _WorktreeLockEntry:
    lock: asyncio.Lock
    users: int = 0

_SYSTEM = (
    "You are an expert software engineer. Generate the COMPLETE, production-quality "
    "contents of a single file: fully implemented with real functionality and "
    "error handling, NO placeholders, NO TODOs, NO '...' elisions, no stub bodies. "
    "Write it as if shipping to production. Output ONLY the file contents — no "
    "commentary, no markdown fences."
)

# Files whose quality is doc/manifest-shaped (not source logic). Routed to the
# DOCS tier with extra, content-specific instructions so a README is thorough and
# a dependency manifest is accurate — never a one-line stub.
_README_NAMES = frozenset({"readme.md", "readme.rst", "readme.txt", "readme"})
_MANIFEST_NAMES = frozenset(
    {"requirements.txt", "pyproject.toml", "package.json", "setup.py", "setup.cfg", "pipfile"}
)

_README_INSTR = (
    "This is the project's README — make it THOROUGH (no placeholder READMEs). "
    "Include, as Markdown sections: a title; a paragraph saying what the app does "
    "(specific to the brief); ## Features (the actual features); ## Installation "
    "(exact dependency-install commands); ## Usage (exact run commands with example "
    "invocations or endpoints); and ## Project structure (a bullet per significant "
    "file/dir). A one-line README is a failure."
)
_MANIFEST_INSTR = (
    "This is the dependency manifest — list the REAL dependencies the app actually "
    "imports/uses for this stack, one per line (or in the proper block). Include a "
    "description/name where the format has one. Do not leave it empty or omit deps."
)

# Design bar for user-facing UI — keeps codegen from shipping the generic
# unstyled-React / emoji-as-icon look that reads as a template.
_DESIGN_DIRECTIVE = (
    "DESIGN BAR (this is a user-facing UI — make it look intentional and distinctive, "
    "not a default template): establish a real visual hierarchy with a deliberate type "
    "scale, spacing, alignment and a cohesive color palette; add depth and interaction "
    "states (hover / focus / active / empty states); style EVERYTHING with CSS. Do NOT "
    "use emoji as icons or primary UI elements — use inline SVG or CSS shapes for icons. "
    "Avoid the unstyled create-react-app look. "
    "NUMERIC DESIGN BUDGET (keep it disciplined, not a rainbow): use a restrained type "
    "scale of 3-5 distinct font sizes and at most 2 font weights; ONE accent color plus "
    "a small neutral ramp (background / surface / border / text) — not a spread of "
    "unrelated hues; and a consistent 4/8px spacing scale (every margin/padding a "
    "multiple of 4). "
    "BANNED AI-DEFAULT LOOK (do NOT ship these unless the brief explicitly asks for "
    "them — they read as a generic AI template): a generic purple/violet gradient hero "
    "band on a white page, and emoji-bullet feature grids (an emoji next to each "
    "feature card). Pick a palette that fits THIS product instead. "
    "GRADIENTS: off by default — if one genuinely fits the product, use subtle "
    "ANALOGOUS hues only (e.g. blue into teal), 2-3 stops max, never on primary "
    "surfaces. "
    "LAYOUT: flexbox first; CSS grid only for true 2-D arrangements; prefer gap "
    "over margins; no floats, no absolute-positioned scaffolding. "
    "TYPE POLISH: headings wrap with text-wrap: balance; body line-height "
    "1.4-1.6; set the page background on <html> so it never flashes white. "
    "NO FILLER VISUALS: no abstract blobs, gradient circles or decorative shape "
    "divs as filler; no hand-drawn SVG illustrations — real content and real "
    "imagery only. "
    "COMPOSE FROM PRIMITIVES: the scaffold ships UI primitives at "
    "src/components/ui.jsx (Button, Panel, StatCard, TextInput, Badge, Modal, "
    "Table, FormField) — COMPOSE pages from them; never restyle them with "
    "one-off inline classes, never hand-roll a duplicate of a primitive — "
    "extend the primitives file instead."
)
# Config hygiene — keeps codegen from hardcoding API keys/endpoints. Reading
# config through env/an accessor lets the post-build config surfacer detect it and
# wire it to a generated Settings screen.
_CONFIG_DIRECTIVE = (
    "CONFIGURATION: never hardcode API keys, tokens, secrets or external endpoint "
    "URLs. Read them from configuration — Vite/browser client config uses "
    "`import.meta.env.VITE_*`; Next.js client config uses `process.env.NEXT_PUBLIC_*`; "
    "server-side Node config uses unprefixed `process.env.*`; Python uses "
    "`os.getenv(...)` — with a sensible fallback. For a web UI that needs "
    "user-supplied values (e.g. an API key), expose them via a config module and "
    "a Settings screen the user can fill in, not literals in code."
)
# Friction-report channel (ported from Lovable's vent loop — measured there to
# improve limit-explanation and cut futile retry loops; vent spikes double as
# incident detection). When the PIPELINE blocks codegen — not the app — the
# agent reports it on a bounded VENT: line instead of silently working around
# it or faking compliance. CodeAgent parses these out of the session output
# into run_metadata + the build's vents.jsonl; the learning loop turns a
# recurring vent into a lesson. Vents never ship in code, commits, or READMEs.
_VENT_DIRECTIVE = (
    "VENT CHANNEL: if something in this pipeline blocks you — a tool you do not "
    "have, a contradictory directive, an error you cannot diagnose, or a "
    "requirement you cannot satisfy honestly — do NOT work around it silently "
    "or fake it. Emit one line per issue starting with `VENT: ` (max 3) "
    "explaining it plainly. Vents go to the operator and the self-improvement "
    "loop, never into code, commits, or the README."
)
_VENT_PREFIX = "VENT:"
_VENT_MAX = 3
_VENT_MAX_CHARS = 300


def parse_vents(text: str, *, max_vents: int = _VENT_MAX,
                max_chars: int = _VENT_MAX_CHARS) -> list[str]:
    """Extract bounded ``VENT:`` friction reports from agentic session output.

    First ``max_vents`` lines win; each is whitespace-flattened and trimmed to
    ``max_chars``. Deterministic and side-effect free."""
    vents: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(_VENT_PREFIX):
            continue
        vent = " ".join(stripped[len(_VENT_PREFIX):].split())[:max_chars].strip()
        if vent:
            vents.append(vent)
        if len(vents) >= max_vents:
            break
    return vents


def strip_vent_lines(content: str) -> str:
    """Remove any ``VENT:`` lines from text about to be written to disk.

    A vent is a pipeline signal, not source — it must never land in a written
    file even when the agent ignores the convention and inlines one. Content
    without the prefix is returned untouched (zero behavior change)."""
    if _VENT_PREFIX not in content:
        return content
    return "\n".join(
        line for line in content.split("\n")
        if not line.strip().startswith(_VENT_PREFIX)
    )


def _vents_log_path(logs_dir: Any, slug: str) -> Path:
    safe = "".join(
        ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(slug or "app")
    ) or "app"
    return Path(logs_dir or "logs") / safe / "vents.jsonl"


def _append_vents_log(slug: str, records: list[dict[str, Any]]) -> Path | None:
    """Append vent records to ``logs/<slug>/vents.jsonl``; never raises.

    One JSON line per vent (``{slug, vent, attempt}``) — the operator trail a
    vent-spike incident check reads. Mirrors council_trace's per-build JSONL
    (logs_dir, disposable) rather than the durable manifest."""
    try:
        from skyn3t.config.settings import get_settings

        path = _vents_log_path(getattr(get_settings(), "logs_dir", "logs"), slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, default=str) + "\n")
        return path
    except Exception as exc:  # noqa: BLE001 - vent logging may never fail a build
        log.warning("code_agent.vents_log_failed", error=str(exc)[:160])
        return None


def read_vents_log(logs_dir: Any, slug: str, *, max_vents: int = _VENT_MAX) -> list[str]:
    """Read back the bounded, de-duplicated vent texts for a build.

    Best-effort: a missing/corrupt log yields ``[]`` — the learning capture
    degrades to zero vent lessons, never a failed build."""
    try:
        path = _vents_log_path(logs_dir, slug)
        if not path.exists():
            return []
        vents: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            vent = (
                str(record.get("vent") or "").strip()
                if isinstance(record, dict) else ""
            )
            if vent and vent not in vents:
                vents.append(vent)
            if len(vents) >= max_vents:
                break
        return vents
    except Exception as exc:  # noqa: BLE001
        log.warning("code_agent.vents_log_read_failed", error=str(exc)[:160])
        return []
# Cheap, broadly-available default for generated apps. Overridable per-app via the
# OPENROUTER_MODEL env var; intentionally NOT a Claude model (cost) or a :free id
# (those get retired and 404 — see the OpenRouter cascade debugging).
_OPENROUTER_DEFAULT_MODEL = "openai/gpt-4o-mini"
# LLM routing — a generated app that calls a language model must use the user's
# OpenRouter key (OpenAI-API-compatible) with a CHEAP model, never a direct
# provider SDK/key: the host keeps no standalone Anthropic/OpenAI key and Claude is
# expensive. OPENROUTER_API_KEY is then auto-passed into the preview by the serve
# layer, so the app runs with no extra setup.
_LLM_DIRECTIVE = (
    "LLM CALLS (server-side only — the key must NEVER reach the browser): if the "
    "app calls a language model, it MUST route every call through its OWN backend "
    "endpoint, which calls OpenRouter (OpenAI-API-compatible) server-side. The "
    "browser may ONLY `fetch('/api/llm', ...)` on the app's own origin; it must "
    "NEVER call `https://openrouter.ai` directly and must NEVER see, embed, or read "
    "`OPENROUTER_API_KEY`. Because the preview runs a SINGLE command, an app that "
    "calls an LLM MUST be built on a stack whose dev/start server can host that "
    "endpoint in the SAME process — Next.js (`app/api/...`), Node/Express, FastAPI, "
    "or Flask — NOT a pure Vite SPA or a static HTML site. On the server side: use "
    "the OpenAI SDK (Python/JS `openai`) pointed at base URL "
    "`https://openrouter.ai/api/v1`, read the key from `OPENROUTER_API_KEY` "
    "(server-only — `process.env.OPENROUTER_API_KEY` / "
    "`os.getenv('OPENROUTER_API_KEY')`), and pick the model from `OPENROUTER_MODEL` "
    f"with a cheap default of `{_OPENROUTER_DEFAULT_MODEL}`. Any model (incl. Claude) "
    "is reachable via its OpenRouter id (e.g. `anthropic/claude-3.5-haiku`). NEVER "
    "give the key a client-exposed prefix (`VITE_`, `NEXT_PUBLIC_`, `REACT_APP_`, "
    "`PUBLIC_`) — those make it browser-visible. Do NOT use the Anthropic SDK, the "
    "native OpenAI API, or ANTHROPIC_API_KEY/OPENAI_API_KEY — only OpenRouter (the "
    "user has no direct provider key and Claude is costly). Per-stack proxy idiom: "
    "Next.js — `app/api/llm/route.js` exporting an async `POST(req)` that calls the "
    "OpenRouter client and returns `Response.json(...)`, with the UI POSTing to "
    "`/api/llm`; Node/Express — `app.post('/api/llm', ...)` on the UI's server; "
    "FastAPI/Flask — an `@app.post('/api/llm')` handler. The route is the ONLY place "
    "the key and OpenRouter client live; keep key and model in env, never hardcoded."
)
# Bolt full-file / no-placeholder contract (research item 44). Every generated OR
# rewritten file must be COMPLETE — the "// rest of the code remains the same"
# elision is the dangling-import bug class. Shared verbatim by the code-improver's
# rewrite prompt (imported there) so a REPAIR can't reintroduce an elided stub.
_FULL_FILE_CONTRACT = (
    "FULL-FILE CONTRACT: output the COMPLETE file every time — the entire contents as "
    "they should appear on disk. NEVER abbreviate with placeholder comments such as "
    "`// rest of code...`, `/* unchanged */`, `// ... existing code ...`, "
    "`# rest of the file is unchanged`, or `TODO: implement`. There is no pre-existing "
    "version to be 'unchanged' from — an elided body ships a non-functional stub."
)

# Real-data / no-fakes contract (research item 45) for product/web app stacks: the
# generated app must model data honestly instead of faking persistence, auth, or the
# network. Injected for data-bearing product stacks (web apps + JS/py backends), NOT
# for games (no product-data/auth surface).
_DATA_DIRECTIVE = (
    "DATA & AUTH (real data flow, never fakes): persist real product / multi-user data "
    "in a real backend store — NEVER use localStorage / sessionStorage as a DATABASE "
    "for multi-user or product data (it is per-browser and trivially forgeable). NEVER "
    "hardcode fake authentication: no `password123`, no client-side-only credential "
    "checks, no hardcoded user list standing in for real auth. Do NOT fake API calls "
    "with setTimeout + hardcoded responses — call the real backend. If the chosen stack "
    "has NO backend, model the data honestly (in-memory or a local file) AND SAY SO "
    "explicitly in the README (state the limitation) — never pretend ephemeral state is "
    "persisted."
)

# Agent / automation app safety contract (research item 50): an app that acts on the
# user's behalf must default destructive/outward actions to a dry run. Injected only
# when the brief implies an agent/automation app (see _implies_agent_app).
_AGENT_APP_DIRECTIVE = (
    "AGENT / AUTOMATION SAFETY (this app performs actions on the user's behalf): every "
    "DESTRUCTIVE or OUTWARD action (sending email / messages, posting, deleting, "
    "executing trades, calling a side-effecting external API) MUST default to a dry run "
    "— `dry_run=true` — and return a STRUCTURED report `{action, target, wouldDo}` "
    "describing what it WOULD do, WITHOUT doing it. A real send/execute happens ONLY "
    "when an explicit flag is passed (`dry_run=false` / `--execute` / `live=true`). "
    "Missing delivery or credential config must yield a structured skipped status "
    "(e.g. `{status: 'skipped_no_delivery'}`), never a crash."
)

# Word-bound tokens/phrases that mark an agent/automation app brief. Single tokens are
# matched on word boundaries (the phaser keyword-stealing lesson — never substring-match
# a short ambiguous word); multi-word phrases are matched as substrings.
_AGENT_APP_TOKENS = frozenset({
    "agent", "agents", "automation", "automate", "autonomous", "workflow",
    "webhook", "webhooks", "scheduled", "cronjob", "crawler", "scraper",
    "bot", "bots",  # discord/telegram/slack bots are agent apps (research §3.2)
})
_AGENT_APP_PHRASES = (
    "team of agents", "send email", "sends email", "send emails", "post to",
    "posts to", "monitor and", "monitors and", "notify me", "auto-reply",
    "briefing", "always-on", "on a schedule",
)
_AGENT_APP_TOKEN_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_AGENT_APP_TOKENS, key=len, reverse=True)) + r")\b"
)


def _implies_agent_app(brief: str) -> bool:
    """True when the brief implies an app that autonomously performs actions —
    an agent / automation / workflow / scheduled bot. Word-bound so a stray
    substring (e.g. 'management') never steals a non-agent brief."""
    low = (brief or "").lower()
    if _AGENT_APP_TOKEN_RE.search(low):
        return True
    return any(phrase in low for phrase in _AGENT_APP_PHRASES)


_GAME_STACK_DIRECTIVE = (
    "STACK — NON-NEGOTIABLE: build a GAME, not a website. This is a Phaser 3 + Vite "
    "browser game in VANILLA JavaScript. The entry is src/main.js (a Phaser Scene "
    "that ONLY renders state and reads input) plus a PURE src/sim.js holding ALL game "
    "logic — createState(seed), step(state, input, dt), isWin(state), isLose(state) "
    "with NO Phaser import; ONE authoritative state advanced only by step(); a SEEDED "
    "rng carried in state. "
    "DETERMINISM (non-negotiable): createState(seed) MUST derive ALL randomness from "
    "its seed ARGUMENT only — never from Math.random() or Date.now(), NOT EVEN to pick "
    "the initial seed value. The runtime gate replays the sim twice from one fixed "
    "seed and flags any divergence as non-determinism, so a clock-seeded rng (e.g. "
    "`rng = Date.now()`) fails the gate. The same seed must always reproduce the exact "
    "same game. "
    "TIME (dt is in SECONDS): step()'s `dt` is the frame delta in SECONDS (~0.016 at "
    "60fps), so all speeds/timers in step() are per-SECOND (e.g. `x += vx * dt`). In "
    "the Phaser scene's `update(time, delta)`, Phaser's `delta` is in MILLISECONDS — "
    "you MUST call `step(state, input, delta / 1000)`, never pass the raw millisecond "
    "delta. The runtime gate steps the sim at dt = 1/60 second; a sim that treats dt "
    "as milliseconds advances ~1000x too slow and is barely exercised. "
    "INPUT CONTRACT (exact): step()'s `input` is ALWAYS the object "
    "`{left, right, up, down, action, pause}` — all BOOLEANS. Read ONLY these fields; "
    "NEVER invent custom input fields (do NOT read input.paddleDir, input.launch, "
    "input.aim, etc.). Map every control onto these: left/right/up/down = movement, "
    "`action` = the primary action (fire / launch / jump / hit). Any field you read "
    "off `input` other than those six is a BUG. "
    "TOUCH/TABLET CONTROLS (required): the game MUST be playable on a tablet with no "
    "keyboard. Wire Phaser pointer/touch input in the Scene: tap/click on the canvas "
    "sets a visible target or direction and maps to left/right/up/down booleans; hold "
    "or tap maps to `action` when appropriate. Do not ship keyboard-only controls. "
    "Show concise in-game guidance such as 'Tap anywhere to move'. "
    "PAUSE/GAME-OVER: `state.paused` and `state.over` are LEVEL flags OWNED BY THE "
    "HOST. step()'s FIRST statement MUST be `if (state.paused || state.over) return "
    "state` (freeze when paused, ignore input when over). step() must NEVER write "
    "state.paused or state.over itself — the Phaser scene toggles pause on a key, NOT "
    "the sim. "
    "NUMERIC SAFETY: initialize EVERY numeric state field to a concrete FINITE number "
    "in createState, and never let an undefined value reach arithmetic (an "
    "`undefined * number` is NaN and FAILS the runtime gate). "
    "FINITE STATE (non-negotiable): the headless gate serializes state to JSON to check "
    "determinism, so EVERY number anywhere in state must stay FINITE — BOTH NaN AND "
    "Infinity are violations. Encode 'inactive / never fires / no cooldown' as `null`, a "
    "boolean flag, or a LARGE FINITE constant (e.g. a big number of seconds) — NEVER "
    "`Infinity` or `-Infinity`. A cooldown/timer/duration set to Infinity (e.g. "
    "`cooldownRemaining = Infinity`) fails the gate; use null or a finite sentinel "
    "instead. "
    "PRESENTATION — FILL THE SCREEN: render entities at a GENEROUS, clearly-visible "
    "size. The player and each enemy/obstacle should be a substantial fraction of the "
    "play area — roughly 48-80px on the standard ~800x600 canvas, NEVER tiny specks "
    "lost in empty space. Scale generated sprites UP to that size (e.g. "
    "`sprite.setDisplaySize(64, 64)`) — do NOT shrink a 256px sprite down to a 32px "
    "icon. If the play area looks mostly empty, make entities bigger AND/OR spawn more "
    "on screen so it reads as a full, lively game, not a few dots in a void. "
    "Use ONLY plain .js. Do NOT use React, Next.js, Vue, JSX/TSX, or TypeScript, and "
    "do NOT create app/, pages/, components/, next.config, tsconfig, or any "
    "web-framework routes — those build a website, not this game. Everything renders "
    "to a single Phaser canvas."
)

# Game FEEL / juice — the difference between a game that FUNCTIONS and one that feels
# good. Backed by data/skills/game-feel-juice.md (retrieved into the prompt); concrete
# per-event mandates so a cheap model wires real feedback instead of a flat tech demo.
# Effects live in the Scene/renderer ONLY so the pure src/sim.js stays deterministic.
_GAME_FEEL_DIRECTIVE = (
    "GAME FEEL (required — a game with no impact feedback is a FAIL, not a real game): "
    "implement juice in the Phaser Scene/renderer ONLY — NEVER in src/sim.js (the pure "
    "sim stays deterministic; it emits typed events, e.g. push to a state.events list, "
    "that the scene drains and reacts to each frame). Mandatory feedback, BY EVENT: "
    "(1) on-shoot — an additive muzzle-flash sprite (~50ms) + a small gun-kickback tween "
    "(~5px, 40ms yoyo Back.easeOut) + a light camera.shake(70, 0.004); bullets rendered "
    "bigger (~1.8x) and stretched along travel. "
    "(2) on-hit (enemy survives) — flash the enemy solid white with setTintFill(0xffffff) "
    "cleared after ~80ms, knock it back away from the impact, and explode an ~8-particle "
    "ADD burst at the hit point. "
    "(3) on-kill — HITSTOP ~50ms (briefly freeze gameplay via a time/physics/tween "
    "timeScale, restored on a REAL-TIME timer so determinism holds) + a ~24-particle "
    "death burst + camera.shake(150, 0.012) + a squash/scale-pop tween + a rising, fading "
    "'+score' text. "
    "(4) on-player-hit — camera.flash(200, 255, 0, 0) (red), red setTintFill, "
    "camera.shake(300, 0.02), and an invincibility alpha-blink the damage check honors. "
    "(5) on-powerup / level-up — a celebration: a gold camera.flash, an upward particle "
    "fountain, a scale-pop (Back.easeOut), a brief slow-mo, and floating text. "
    "RULES: screen shake is strongest-request-wins (take the MAX in a frame, NEVER sum) "
    "and capped (~0.05 intensity); .destroy() every emitter/sprite and clearTint() after "
    "use (a leaked pool fails the runtime gate). At minimum, hitstop + hit-flash + screen "
    "shake + particle bursts MUST be wired and firing — see the game-feel-juice skill for "
    "exact Phaser-3.60 snippets per event."
)
# Scene WIRING — the single most damaging game bug we see: codegen splits the game
# into real Scene modules (BootScene.js, GameScene.js, …) but src/main.js never
# imports them and runs a throwaway INLINE scene instead, so the polished game is
# orphaned dead code and the player sees "just ships" / colored placeholders. This
# directive forbids that at the source; CodeAgent._repair_scene_registry is the
# deterministic safety net that auto-rewires main.js when a model ignores it.
_GAME_WIRING_DIRECTIVE = (
    "SCENE WIRING — NON-NEGOTIABLE (the #1 cause of a dead-looking game): src/main.js "
    "is the ONE entry and the ONE place that calls `new Phaser.Game(config)`. If you "
    "split the game across multiple Scene files (e.g. BootScene.js, MenuScene.js, "
    "GameScene.js, GameOverScene.js), EACH file MUST `export default` its Scene class, "
    "and src/main.js MUST `import` every one of those Scene modules and list them in the "
    "Phaser `config.scene: [...]` array, with the boot/preload scene FIRST. main.js MUST "
    "NOT define its own inline Scene classes that shadow those modules — a main.js that "
    "runs a throwaway inline scene while the real GameScene.js sits unimported renders "
    "placeholders / a static frame, NEVER the actual game. Every Scene you write MUST be "
    "reachable from the registry, and every `this.scene.start('key')` target MUST match a "
    "registered scene key. Do NOT leave any authored Scene module unimported."
)
# The game-art directive is now GENRE-AWARE and built per-brief from the art
# director's deterministic plan — see CodeAgent._game_art_directive. (A geometric
# game is told to render crisp primitives; a sprite genre gets the load+fallback
# idiom per game-aware role; both over one shared palette.)

# Stacks for which the design bar applies.
_WEB_STACKS = frozenset({
    "react", "react_vite", "react_ts", "vite", "nextjs", "next", "astro",
    "remix", "static", "html", "node", "node_express", "express", "vue",
    "svelte", "sveltekit",
    "phaser",  # a Phaser game has real HUD/menu visual-design concerns
})

# Generated app composition contracts apply to conventional web frontends, not
# canvas-game prompts. Phaser retains its dedicated game/art directives.
_DESIGN_WEB_STACKS = _WEB_STACKS - {"phaser"}


def _seed_tokens_md(extra: Any) -> str:
    """The per-build design-seed override the runner threads for divergence-
    seeded best-of-N: ``extra["design_seed"]["tokens_md"]`` replaces the
    brief-derived ``design_md_block(brief)`` so each candidate themes from a
    DISTINCT token set. '' when absent/malformed -> derive (the byte-identical
    default path)."""
    seed = extra.get("design_seed") if isinstance(extra, dict) else None
    if isinstance(seed, dict):
        tokens_md = seed.get("tokens_md")
        if isinstance(tokens_md, str):
            return tokens_md
    return ""
_FRONTEND_EXTENSIONS = frozenset({
    ".jsx", ".tsx", ".css", ".html", ".htm", ".vue", ".svelte",
    ".astro", ".scss", ".sass", ".less",
})

# Stacks that carry product/multi-user DATA (so the real-data/no-fake-auth
# contract applies): every web product stack EXCEPT games, plus the common
# Python backends. A Phaser game has no auth/persistence surface.
_DATA_STACKS = (_WEB_STACKS - {"phaser"}) | frozenset({"fastapi", "flask", "django"})

# Swift / SwiftUI native macOS (Swift Package Manager). NOT a web app — the model
# must never emit package.json / npm / index.html / JS. Mirrors the sim-core split:
# pure logic in its own SwiftUI-free file so it is unit-testable.
_SWIFT_DIRECTIVE = (
    "STACK — NON-NEGOTIABLE: build a NATIVE macOS app in Swift with SwiftUI, built "
    "by Swift Package Manager (SPM). This is NOT a web app: NEVER emit package.json, "
    "npm, node_modules, index.html, JavaScript/TypeScript, Vite, or any web files. "
    "LAYOUT (SPM): a root `Package.swift` (swift-tools-version:5.9, "
    "`platforms: [.macOS(.v13)]`) declaring FOUR targets — a `.target` library "
    "`AppCore` for the pure logic, an `.executableTarget` `App` (dependencies: "
    '["AppCore"]) for the UI, an `.executableTarget` `AppCLI` (dependencies: '
    '["AppCore"]) for deterministic terminal proof, and a `.testTarget` '
    '`AppCoreTests` (dependencies: ["AppCore"]). Sources live under '
    "`Sources/AppCore/*.swift`, `Sources/App/*.swift`, `Sources/AppCLI/main.swift`, "
    "and tests under `Tests/AppCoreTests/*.swift`. "
    "ENTRY: `Sources/App/MainApp.swift` holds the `@main struct ...: App` with a "
    "`WindowGroup { ContentView() }` — the `@main` file must NOT be named "
    "`main.swift`. "
    "ARCHITECTURE (pure core + render-only view): put ALL state and logic in "
    "`Sources/AppCore/` as plain value types / structs with NO `import SwiftUI` "
    "(so XCTest can exercise them headlessly) — ONE authoritative model with its "
    "operations. The SwiftUI views in `Sources/App/` own NO logic: they hold the "
    "model (`@State`/`@StateObject`), render it, and forward user actions to its "
    "methods. "
    "TESTS: `Tests/AppCoreTests/` uses XCTest with `@testable import AppCore` and "
    "asserts the core's real behavior. INTERACTIVE PROOF: `AppCLI` provides `--help`, "
    "an offline prompt loop over AppCore, and a nonzero stderr error for invalid args. "
    "Ship root `.skyn3t-cli-playtest.json` version 1 with root-relative command "
    "`.build/debug/AppCLI` and literal help / interactive / invalid-input scenarios. "
    "TARGET: macOS 13+; keep it compiling cleanly under the installed toolchain "
    "(avoid unavailable APIs). Implement the brief's real features, not a stub."
)


# Native iOS is deliberately separate from the macOS SwiftPM directive: it needs
# an Xcode target, iOS privacy declarations, device-only capture APIs, and a
# first-class accessibility contract.
_SWIFT_IOS_DIRECTIVE = (
    "STACK — NON-NEGOTIABLE: build a NATIVE iPhone/iPad app in Swift + SwiftUI for Xcode, "
    "not a website and not a macOS SwiftPM executable. NEVER emit package.json, npm, "
    "node_modules, index.html, JavaScript/TypeScript, Vite, React, Expo, or web files. "
    "PROJECT: preserve/create App.xcodeproj with a real application target named App and "
    "an XCTest target; use its synced App/ source folder so new Swift files compile without "
    "fragile manual PBX lists. Keep App/Info.plist and include NSCameraUsageDescription. "
    "Use Swift 6 strict concurrency, @MainActor UI mutation, structured async/await, and "
    "Observation (@Observable) for nonpersistent screen state. PERSISTENCE: use SwiftData "
    "@Model, ModelContainer, @Query, and explicit relationships; ship offline-first data with "
    "no account or key required. Keep parsing/normalization/business rules testable via XCTest. "
    "SCANNING: use VisionKit DataScannerViewController only after support/availability checks; "
    "always offer prominent manual entry and let people correct every OCR field. REVIEWS: never "
    "scrape sites or invent external scores. Model attributed provider links/adapters, fetch only "
    "documented user-configured APIs, and keep community tasting notes as first-party local data. "
    "ACCESSIBILITY IS LOAD-BEARING: Dynamic Type, VoiceOver labels/hints, semantic colors, 44pt "
    "targets, visible text actions, high contrast, and simple large-button navigation for older "
    "adults. DESIGN: standard SwiftUI navigation, tabs, sheets, SF Symbols, and semantic materials "
    "that adapt to Liquid Glass; newer visual APIs need availability fallbacks. Implement the full "
    "brief, never a starter counter. MAC PROOF: xcodebuild -project App.xcodeproj -scheme App -sdk "
    "iphonesimulator -destination generic/platform=iOS Simulator build."
)


_MCP_DIRECTIVE = (
    "STACK — NON-NEGOTIABLE: build a Model Context Protocol (MCP) SERVER in Python "
    "that exposes TOOLS to AI assistants over the stdio transport. This is NOT a web "
    "app and NOT an HTTP/REST API: NEVER emit package.json, npm, index.html, Flask/"
    "FastAPI, or a web server. "
    "LAYOUT: `server.py` (the runnable root — `python server.py` starts the stdio "
    "server), `tools.py` (the tool IMPLEMENTATIONS as pure functions), "
    "`tests/test_tools.py` (pytest over the pure functions), and `requirements.txt` "
    "listing `mcp`. "
    "SDK: use the official Python SDK — `from mcp.server.fastmcp import FastMCP`; "
    "create `mcp = FastMCP(\"<server-name>\")`, register each tool with an `@mcp.tool()` "
    "decorator, and end with `if __name__ == \"__main__\": mcp.run()` (stdio). "
    "ARCHITECTURE (pure core + thin registration — the sim-core split): put ALL tool "
    "LOGIC in `tools.py` as ordinary, PURE, TOTAL functions (no `mcp` import, never "
    "raise on well-typed input) so they are unit-testable without the SDK; the "
    "`@mcp.tool()` functions in `server.py` are THIN wrappers that call `tools.py`. "
    "TOOLS ARE LOAD-BEARING: give every tool a descriptive snake_case name, a "
    "one-line docstring (clients pick tools by name + description), and TYPED "
    "parameters (real type hints → FastMCP derives the JSON Schema). Implement the "
    "brief's real tools — 2+ genuinely useful ones, not a stub. Handle bad input by "
    "returning a structured result, never by crashing the process. Add at least one "
    "`@mcp.resource(...)`. Keep upstream API keys OPTIONAL (read from the env with a "
    "safe fallback) so the server boots and lists its tools with no secrets."
)


_RAG_DIRECTIVE = (
    "STACK — NON-NEGOTIABLE: build a RAG (retrieval-augmented) 'chat with your "
    "documents' app as a Python FastAPI server. This is NOT a React/npm app: NEVER "
    "emit package.json, index.html, or JSX. "
    "LAYOUT: `main.py` (the FastAPI app — the runnable root: `python main.py` serves "
    "on the PORT env var, default 8000, via `uvicorn.run` under `if __name__ == "
    "\"__main__\":`), `rag_core.py` (chunking + retrieval as PURE stdlib Python), "
    "`corpus/` (seed documents indexed at boot), `test_rag_core.py` (pytest over the "
    "pure core), `requirements.txt` (fastapi, uvicorn, httpx). "
    "ROUTES ARE LOAD-BEARING (a deterministic gate drives them): GET / → a minimal "
    "SELF-CONTAINED HTML chat page (vanilla JS fetch to /chat and /ingest, inline "
    "CSS, NO external assets or CDNs); GET /health → 200; "
    "GET /v1/stats → JSON with integer \"documents\" and \"chunks\" counts; "
    "POST /ingest {\"text\", \"source\"} → chunk + index the text (validate: missing/"
    "empty text is a 4xx, never a 500) and grow the stats; GET /query?q=...&k=3 → "
    "{\"results\": [{\"text\", \"source\", \"score\"}]} ranked by relevance — a "
    "just-ingested fact MUST be retrievable; POST /chat {\"question\"} → 200 with a "
    "non-empty \"answer\" grounded in the retrieved chunks plus a \"sources\" list; "
    "GET /chat/stream?question=...&k=3 → the same grounded answer as an SSE stream "
    "(Content-Type text/event-stream, `data:` frames, terminated by `data: [DONE]` — "
    "the stream must CLOSE, never stay open forever). "
    "ARCHITECTURE (pure core + thin HTTP — the sim-core split): ALL retrieval logic "
    "lives in `rag_core.py` with NO fastapi/uvicorn/httpx imports so it is "
    "unit-testable without a server; routes in `main.py` are THIN wrappers. "
    "LLM SEAM: `/chat` may call an OpenAI-compatible endpoint ONLY via the "
    "OPENAI_BASE_URL env var (model from OPENAI_MODEL, key from OPENAI_API_KEY) — "
    "NEVER hardcode a provider URL, model, or key, NEVER import a provider SDK. "
    "When the seam is unset or the call fails, `/chat` MUST still return 200 with an "
    "extractive answer (the top retrieved chunk) — every route works with ZERO keys."
)


_WORKFLOW_DIRECTIVE = (
    "STACK — NON-NEGOTIABLE: build an agent-WORKFLOW app (a multi-step runner) as a "
    "Python FastAPI server. This is NOT a React/npm app: NEVER emit package.json, "
    "index.html files, or JSX. "
    "LAYOUT: `main.py` (the FastAPI app — the runnable root: `python main.py` serves "
    "on the PORT env var, default 8000, via `uvicorn.run` under `if __name__ == "
    "\"__main__\":`), `workflow_core.py` (the engine as PURE stdlib Python), "
    "`test_workflow_core.py` (pytest over the pure engine), `requirements.txt` "
    "(fastapi, uvicorn). "
    "ENGINE CONTRACT (pure core + thin HTTP — the sim-core split): steps run IN "
    "ORDER and may ONLY call tools registered in a ToolRegistry (never fabricate "
    "an API); a failing step retries its declared count then lands in a TYPED error "
    "envelope ({type, step, message, retryable}) and fails the run — the engine "
    "NEVER raises; every run is appended to a run ledger. "
    "ROUTES ARE LOAD-BEARING: GET / → a minimal SELF-CONTAINED HTML runs dashboard "
    "(vanilla JS, inline CSS, NO external assets); GET /health → 200; GET /v1/stats "
    "→ JSON with integer \"runs\" and the workflow/tool names; POST /trigger "
    "{\"workflow\", \"params\", \"dry_run\"} with dry_run DEFAULTING TRUE → run the "
    "workflow and return {\"run_id\", \"dry_run\", \"status\", \"brief\", "
    "\"delivery\": {\"status\"}}; GET /runs and GET /runs/{id} read the ledger. "
    "DELIVERY IS OPTIONAL: adapters (webhook/email) are configured via env vars "
    "(e.g. WEBHOOK_URL) — a dry run yields delivery.status=\"dry_run\", a live run "
    "with NO config yields \"skipped_no_delivery\", NEVER a crash and NEVER a "
    "required key. Implement the brief's real steps as registered tools."
)


_AGENT_PACK_DIRECTIVE = (
    "STACK — NON-NEGOTIABLE: build an agent TEAM PACK — a persona ROSTER product, "
    "NOT a running app: NEVER emit package.json, index.html, FastAPI, or servers. "
    "LAYOUT: `agents/<division>/<name>.md` persona files, `catalog.json` (the pack "
    "manifest: {\"pack\", \"divisions\": {division: [names]}, \"convert\": "
    "{\"claude-code\": {\"dest\": \".claude/agents\"}}}), `pack_tools.py` (PURE "
    "stdlib lint/originality/convert tooling), `main.py` (the CLI: lint | convert "
    "--tool X | summary; `python main.py lint` exits non-zero on issues), and the "
    "pack's own `test_agent_pack.py`. ZERO runtime dependencies. "
    "EVERY PERSONA (a deterministic lint gate enforces this): YAML frontmatter with "
    "name, description, color; sections `## Identity`, `## Core Mission`, "
    "`## Critical Rules` (5+ concrete, DOMAIN-SPECIFIC rules drawn from the brief); "
    "at least 120 words of real substance. PERSONAS MUST BE GENUINELY DISTINCT — a "
    "pairwise shingle-originality check fails re-skinned duplicates, so give each "
    "persona its own voice, vocabulary, and responsibilities. Build the divisions "
    "and personas the BRIEF asks for (a law-firm pack gets legal roles, not "
    "generic ones), and keep every agent listed in catalog.json."
)


# Variant directives (wave-2 §3.6/3.7/3.9/3.10): compact contracts for briefs
# that ride an existing stack but carry a variant shape. One good clause here
# is worth many repairs — without these, a model-generated 'llm gateway' never
# hears about fallback chains or the keyless internal seam.
_VARIANT_DIRECTIVES: tuple[tuple[str, Any, str], ...] = (
    ("fastapi", _implies_finance,
     "VARIANT — paper-trading finance app: keep the strategy engine a PURE "
     "module (`strategy.py`, stdlib-only, Decimal money math — float never "
     "touches money) over DATED candles from `market.py` (DATA_VENDOR env, "
     "default canned fixtures — zero keys); every analysis honors the as_of "
     "cutoff (NEVER read a candle dated after as_of); same candles → an "
     "IDENTICAL trade list, every value finite; unknown symbols → typed 404 "
     "{\"error\": {\"type\": \"NO_DATA\"}}; orders are PAPER-ONLY into the "
     "sqlite ledger (LEDGER_FILE env) and an overdraft/oversell → typed 400 "
     "INSUFFICIENT_FUNDS — never a bare 500 and never a real broker call."),
    ("fastapi", _implies_market_data,
     "VARIANT — market-data API: keep the pure vendor registry in `vendors.py` "
     "(canned deterministic fixtures selected by the DATA_VENDOR env var, default "
     "canned — zero keys); routes GET /stock/{symbol}, GET /indicators?symbol=&"
     "window=, GET /news?symbol=; an unknown symbol returns a TYPED 404 "
     "{\"error\": {\"type\": \"NO_DATA\", ...}}; invalid params are 422 — never a "
     "bare 500."),
    ("fastapi", _implies_llm_gateway,
     "VARIANT — LLM gateway: an OpenAI-compatible POST /v1/chat/completions proxy "
     "with a model catalog (per-1K costs + tags), PRIORITY routing, FALLBACK to "
     "the next provider on upstream error (exhausted chains → typed 502 "
     "ALL_UPSTREAMS_FAILED), cheap-tagged requests → small models, and an EXACT "
     "usage ledger (tokens × catalog prices) at GET /v1/usage. Ship two bundled "
     "stub upstreams behind 'internal:' URLs so it boots KEYLESS; real providers "
     "are env-configured URLs, never hardcoded."),
    ("rag", _implies_memory_chat,
     "VARIANT — memory-augmented chat: journal every chat turn to an append-only "
     "JSONL file (MEMORY_FILE env, default memory.jsonl; corrupt lines skipped, "
     "never fatal) and REPLAY it into the retrieval index at boot so facts stated "
     "in conversation survive restarts. Record each turn's memory AFTER its own "
     "retrieval — a turn must never retrieve itself. Report a \"memories\" count "
     "in /v1/stats."),
    ("python_cli", _implies_cli_agent,
     "VARIANT — terminal copilot: `run '<prompt>' --format json` must emit ONE "
     "typed JSON event per line (session_start / tool_call / tool_result / answer "
     "/ denied) and exit 2 when a tool is denied; writes require --mode "
     "workspace-write (read-only is the DEFAULT) and every file path is confined "
     "to the workspace (symlink-safe, size-capped); ship offline mock-provider "
     "scenarios in scenarios.json and `doctor --output-format json`. Also ship an "
     "offline `chat` prompt loop and root `.skyn3t-cli-playtest.json` version 1 with "
     "literal help / interactive / invalid-input scenarios; its command begins "
     "`[\"{python}\", \"-B\", \"main.py\"]`."),
    ("nextjs", _implies_supabase,
     "VARIANT — Supabase Next.js app: create `lib/supabaseClient.js` with "
     "`createClient` from `@supabase/supabase-js`, reading ONLY browser-safe "
     "`NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`; show a "
     "clear missing-config state instead of crashing; any `SUPABASE_SERVICE_ROLE_KEY` "
     "or admin secret is server-only and must NEVER be imported, referenced, or "
     "rendered from client components such as `app/page.jsx`."),
)


def variant_directive(stack: str, brief: str) -> str:
    """The variant contract for this brief, or '' when the brief is the base
    stack shape. Pure and deterministic (same triggers as scaffold_for)."""
    low = (stack or "").strip().lower()
    for wanted_stack, trigger, directive in _VARIANT_DIRECTIVES:
        if low == wanted_stack and trigger(brief or ""):
            return directive
    return ""


class CodeAgent(BaseAgent):
    # Max concurrent per-file generations (bounds nested claude -p instances).
    _gen_concurrency = 4
    _run_metadata_keys = (
        "degraded", "degraded_reason", "codegen_override_unavailable",
        "prompts", "agentic", "prose_rejected", "index_html_repaired",
        "scene_registry_repaired", "agentic_untrusted_paths", "vents",
    )

    def __init__(self, name: str = "code", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="codegen", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="codegen", description="Write runnable source files into the worktree",
            tags=("generative", "code")))
        self.llm = llm or LLMClient()
        # CodeAgent is a singleton, but independent worktrees run concurrently.
        # All mutable execution metadata is isolated in the current task context;
        # only writes to the SAME worktree are serialized.
        self._run_metadata_var: contextvars.ContextVar[dict[str, Any] | None] = (
            contextvars.ContextVar(f"{name}_codegen_metadata", default=None)
        )
        self._worktree_locks: dict[str, _WorktreeLockEntry] = {}
        self._worktree_locks_guard = asyncio.Lock()
        self._active_runs = 0
        self._active_runs_guard = asyncio.Lock()

    async def initialize(self) -> None:
        self.metadata["backend"] = self.llm.backend

    @property
    def _task_metadata(self) -> dict[str, Any]:
        """Metadata isolated to the current build execution."""
        current = self._run_metadata_var.get()
        return current if current is not None else self.metadata

    @staticmethod
    def _worktree_key(task: TaskRequest) -> str:
        payload = task.payload if isinstance(task.payload, dict) else {}
        raw = payload.get("worktree_dir") or payload.get("project_dir")
        if not raw:
            return f"task:{task.task_id}"
        try:
            return str(Path(str(raw)).resolve())
        except OSError:
            return str(raw)

    async def run(self, task: TaskRequest) -> TaskResult:
        """Run different worktrees concurrently; serialize only shared targets."""
        # Run metadata used to live on the singleton. Remove any legacy/stale
        # values before entering a task-local context so old state cannot leak
        # through diagnostics or tests during a rolling upgrade.
        for metadata_key in self._run_metadata_keys:
            self.metadata.pop(metadata_key, None)
        key = self._worktree_key(task)
        async with self._worktree_locks_guard:
            entry = self._worktree_locks.get(key)
            if entry is None:
                entry = _WorktreeLockEntry(asyncio.Lock())
                self._worktree_locks[key] = entry
            entry.users += 1
        acquired = False
        token = None
        try:
            await entry.lock.acquire()
            acquired = True
            token = self._run_metadata_var.set({})
            async with self._active_runs_guard:
                self._active_runs += 1
                self.status = AgentStatus.BUSY
            # Deliberately bypass BaseAgent.run's singleton-wide _run_lock.
            # Timing, task events, persistence-facing TaskResult fields, and
            # LLM routes still flow through _run_locked; route capture is already
            # task-local in LLMClient.
            return await self._run_locked(task)
        finally:
            if token is not None:
                self._run_metadata_var.reset(token)
                async with self._active_runs_guard:
                    self._active_runs = max(0, self._active_runs - 1)
                    self.status = AgentStatus.BUSY if self._active_runs else AgentStatus.READY
            if acquired:
                entry.lock.release()
            async with self._worktree_locks_guard:
                entry.users = max(0, entry.users - 1)
                if entry.users == 0 and self._worktree_locks.get(key) is entry:
                    self._worktree_locks.pop(key, None)

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload if isinstance(task.payload, dict) else {}
        worktree = self._resolve_worktree(payload)
        contracts = snapshot_acceptance_contracts(worktree)
        restored: set[str] = set()
        try:
            result = await self._execute_codegen(
                task, worktree, contracts, restored
            )
        finally:
            restored.update(restore_acceptance_contracts(worktree, contracts))

        if contracts and isinstance(result.output, dict):
            result.output["acceptance_contracts_protected"] = sorted(contracts)
            result.output["acceptance_contracts_restored"] = sorted(restored)
        if restored:
            log.warning(
                "code_agent.acceptance_contract_restored",
                task_id=task.task_id,
                files=sorted(restored),
            )
        return result

    async def _execute_codegen(
        self,
        task: TaskRequest,
        worktree: Path,
        acceptance_contracts: dict[str, bytes],
        restored_contracts: set[str],
    ) -> TaskResult:
        p = task.payload
        # Per-run reset: this agent is a long-lived singleton, so a `degraded`
        # flag set by a PRIOR build must not leak into this one (it would emit a
        # false no_go + halved score on a clean build). Clear before any work.
        run_metadata = self._task_metadata
        for key in self._run_metadata_keys:
            run_metadata.pop(key, None)
        brief = p.get("brief", "") or p.get("slug", "app")
        # An art plan the runner computed + threaded (LLM-tailored or the floor); the
        # game-art directive uses it so codegen lists the SAME roles the sprite
        # generator produced. Absent for non-game builds / older payloads.
        _extra = p.get("extra")
        _art_plan = _extra.get("art_plan") if isinstance(_extra, dict) else None
        # The runner-threaded GDD (LLM-tailored or the deterministic floor); the
        # depth directive uses it so a retry keeps the SAME design the run committed.
        _game_design = _extra.get("game_design") if isinstance(_extra, dict) else None
        _asset_foundry = _extra.get("asset_foundry") if isinstance(_extra, dict) else None
        # Advisory-council guidance the runner computed ONCE for this build and
        # threaded, exactly like art_plan/game_design above. Threading rather
        # than recomputing keeps every best-of-N trajectory on the SAME advice,
        # so the trajectories still differ only by model.
        _moa_guidance = str(
            (_extra.get("moa_guidance") or "") if isinstance(_extra, dict) else ""
        )
        # Per-trajectory design seed (divergence-seeded best-of-N): threaded
        # exactly like moa_guidance above; overrides the derived token block.
        _design_tokens_md = _seed_tokens_md(_extra)
        raw_plan = p.get("plan")
        plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
        raw_prior = p.get("prior")
        prior = raw_prior if isinstance(raw_prior, dict) else {}
        raw_design = self._unwrap_design_payload(prior.get("design"))
        profile = profile_from_payload(_extra.get("layout_profile") if isinstance(_extra, dict) else None)
        design = self._design_with_layout_profile(
            raw_design if isinstance(raw_design, dict) else None, profile,
        )
        stack = detect_stack(
            brief=brief, plan=plan,
            explicit=p.get("stack", "") or (plan.get("stack", "") if plan else ""),
        )
        app_name = slugify(p.get("slug") or brief, "app")

        # Hermes orchestrator-worker: a parallel SLICE writes ONLY its own files
        # (no whole-app scaffold floor / under-delivery revert — a slice is small
        # by design, and the merged tree's wiring is repaired by the post-merge
        # proof/fix-loop). Dispatched before the monolithic path below.
        slice_scope = p.get("slice_scope") if isinstance(p.get("slice_scope"), dict) else None
        if slice_scope:
            return await self._execute_slice(
                task,
                p,
                brief,
                stack,
                plan,
                app_name,
                worktree,
                slice_scope,
                acceptance_contracts,
                restored_contracts,
            )

        # Decide what files to write. Prefer the architect's plan; otherwise the
        # canonical scaffold. The scaffold guarantees a runnable baseline. For game
        # stacks, make the scaffold art-aware (preload role sprites + primitive
        # fallback) when game_art is enabled — the sprites themselves are written by
        # the runner's role-sprite step (#6).
        from skyn3t.config.settings import get_settings as _gs_art

        art = stack == "phaser" and bool(getattr(_gs_art(), "game_art_enabled", True))
        scaffold = scaffold_for(stack, app_name, brief, art=art)
        files: dict[str, str] = dict(scaffold)
        reported_expected: set[str] = set()
        preserved_asset_paths: set[str] = set()

        knowledge = knowledge_block(p)
        # Codegen-only CLI routing: a configured `codegen_cli_provider` (e.g.
        # "claude") runs the agentic whole-app build on that CLI even when the
        # global backend is cheap (OpenRouter) — high-quality codegen without
        # paying for the CLI on every other stage.
        model_override = p.get("model_override")
        _codegen_cli_ok, _agentic_kwargs, _codegen_unavailable = self._codegen_agentic_routing(
            stack, model_override, p)
        if _codegen_unavailable:
            run_metadata["codegen_override_unavailable"] = _codegen_unavailable
            run_metadata["codegen_override_reason"] = (
                f"{_codegen_unavailable} CLI was explicitly selected for codegen but "
                "is unavailable; kept the offline scaffold without an OpenRouter fallback."
            )
        if _codegen_unavailable:
            # An explicit provider choice is a routing lock. Keep the runnable
            # offline scaffold rather than silently spending through OpenRouter.
            pass
        elif self.llm.backend == "stub" and not _codegen_cli_ok:
            # Offline: deliver the runnable scaffold as-is.
            pass
        elif getattr(self.llm, "supports_agentic", False) or _codegen_cli_ok:
            # CLI backend is a coding AGENT: ONE agentic session authors the
            # whole coherent multi-file app — including its OWN entrypoint —
            # into a CLEAN worktree. (Pre-laying the scaffold left a stub main.py
            # masquerading as the entrypoint while the real code sat in a package.)
            # The scaffold is a fallback only if the agent under-delivers.
            from skyn3t.config.settings import get_settings

            preexisting_assets = self._snapshot_preexisting_assets(worktree)
            preserved_asset_paths = set(preexisting_assets)
            before_agentic = self._snapshot_regular_files(worktree)
            untrusted_agentic_paths: set[str] = set()

            # "Did the agent add a real app beyond the scaffold?" A delivery barely
            # above the scaffold's own code size is the placeholder leaking through,
            # so require a real margin over it (not a flat 800-byte floor).
            threshold = max(800, self._threshold_code_bytes(scaffold) * 2)
            max_retries = max(0, int(getattr(get_settings(), "agentic_retries", 1)))

            disk: dict[str, str] = {}
            code_bytes = 0
            agentic_ok = True
            agentic_confirmed = True
            agentic_error = ""
            prose_files: list[str] = []
            missing_planned: list[str] = []
            contract_gap = ""
            expected_planned = self._agentic_planned_paths(plan)
            reported_expected = expected_planned
            attempt = 0
            while True:
                prompt = (
                    self._agentic_prompt(
                        brief, stack, plan, knowledge,
                        art_plan=_art_plan, game_design=_game_design,
                        asset_foundry=_asset_foundry, design=design,
                        moa_guidance=_moa_guidance,
                        design_tokens_md=_design_tokens_md)
                    if attempt == 0
                    else self._agentic_resume_prompt(
                        brief, stack, plan, disk, missing_planned,
                        agentic_error, contract_gap, design=design,
                        design_tokens_md=_design_tokens_md)
                )
                # Capture the exact prompt this build sends the model so it's
                # inspectable per-build in the dashboard. Built, sent, and — until
                # now — discarded. One entry per attempt (initial + any retry).
                run_metadata.setdefault("prompts", []).append({
                    "stage": "codegen" if attempt == 0 else f"codegen_retry_{attempt}",
                    "chars": len(prompt),
                    "text": prompt,
                })
                res = await self.llm.agentic_build(
                    prompt,
                    str(worktree),
                    planned_paths=sorted(expected_planned),
                    **_agentic_kwargs,
                )
                self._restore_preexisting_assets(worktree, preexisting_assets)
                restored_contracts.update(
                    restore_acceptance_contracts(worktree, acceptance_contracts)
                )
                untrusted_agentic_paths.update(
                    self._prune_untrusted_agentic_new_paths(
                        worktree,
                        before_agentic,
                        expected_planned,
                    )
                )
                agentic_ok = bool(res.get("ok", True))
                agentic_confirmed = agentic_ok and bool(
                    res.get("completed", agentic_ok)
                )
                agentic_error = res.get("error", "")
                # Vent channel: harvest friction reports from the session output
                # BEFORE any degradation handling — best-effort, never raises.
                await self._capture_vents(res, app_name, attempt)
                disk = self._read_files(worktree)
                # The CLI writes files directly (bypassing extract/validate). Guard
                # against it writing chat prose instead of code: reject prose source
                # files so they don't count as "delivered" and never ship.
                disk, prose_files = self._clean_agentic_files(disk, scaffold)
                self._unlink_rejected_agentic_files(worktree, prose_files, scaffold)
                code_bytes = self._code_bytes(disk)
                present_planned = self._present_planned_files(worktree, expected_planned)
                missing_planned = sorted(expected_planned - present_planned)
                # Decoration the model planned but never wrote must not buy an
                # agentic round-trip — see _synthesize_trivial_assets.
                if missing_planned:
                    synthesized = self._synthesize_trivial_assets(worktree, missing_planned)
                    if synthesized:
                        log.info("code_agent.trivial_assets_synthesized",
                                 files=synthesized[:12])
                        present_planned = self._present_planned_files(
                            worktree, expected_planned)
                        missing_planned = sorted(expected_planned - present_planned)
                contract_gap = self._agentic_contract_gap(stack, disk)
                under_delivered = (
                    not (disk and code_bytes >= threshold)
                    or bool(contract_gap)
                    or bool(missing_planned)
                )
                # A large subset is still incomplete when the architecture named
                # files that never arrived, or the provider never confirmed finish.
                # Resume in the SAME worktree with a compact missing-file prompt.
                if (agentic_confirmed and not under_delivered) or attempt >= max_retries:
                    break
                log.warning("code_agent.agentic_retry", attempt=attempt + 1,
                            code_bytes=code_bytes, threshold=threshold, ok=agentic_ok,
                            contract_gap=contract_gap,
                            missing_planned=missing_planned[:12])
                attempt += 1

            res["completed"] = agentic_confirmed
            res["complete"] = agentic_confirmed and not under_delivered
            res["planned_files"] = len(expected_planned)
            res["missing_files"] = missing_planned[:50]
            run_metadata["agentic"] = res
            if prose_files:
                # Some files were prose (not code) and were reverted to the
                # scaffold. This DEGRADES the build only if it left the app
                # under-delivered (checked next) — a substantial, building app with
                # one auto-reverted util still works and must NOT be no_go'd over it
                # (proof/build/liveness already verify the app actually runs).
                log.warning("code_agent.agentic_prose_rejected", files=prose_files)
                run_metadata["prose_rejected"] = list(prose_files)
            if untrusted_agentic_paths:
                cleaned = sorted(untrusted_agentic_paths)
                log.warning("code_agent.agentic_untrusted_paths_pruned", files=cleaned)
                run_metadata["agentic_untrusted_paths"] = cleaned
            contract_gap = self._agentic_contract_gap(stack, disk)
            present_planned = self._present_planned_files(worktree, expected_planned)
            missing_planned = sorted(expected_planned - present_planned)
            under_delivered = (
                not (disk and code_bytes >= threshold)
                or bool(contract_gap)
                or bool(missing_planned)
            )
            incomplete = under_delivered or not agentic_confirmed
            if incomplete:
                # Genuine under-delivery: no-op'd / left a stub, or a prose-revert
                # dropped the real code below threshold.
                reasons: list[str] = []
                if not agentic_confirmed:
                    reasons.append(
                        f"agentic build failed or did not confirm completion: "
                        f"{agentic_error or 'provider returned without a successful finish'}"
                    )
                if missing_planned:
                    reasons.append(
                        f"missing {len(missing_planned)} planned file(s): "
                        + ", ".join(missing_planned[:12])
                    )
                elif contract_gap:
                    reasons.append(f"missed required {stack} contract: {contract_gap}")
                elif prose_files:
                    reasons.append(
                        f"prose (not code) in {prose_files} left only "
                        f"{code_bytes} code bytes (threshold {threshold})"
                    )
                elif not (disk and code_bytes >= threshold):
                    reasons.append(
                        f"under-delivered {code_bytes} code bytes in {len(disk)} files "
                        f"(threshold {threshold})"
                    )
                detail = "; ".join(reasons) or "completion contract not satisfied"
                degraded_reason = (
                    f"agentic build incomplete after {attempt} retr"
                    f"{'y' if attempt == 1 else 'ies'}: {detail}"
                )
                log.warning(
                    "code_agent.agentic_degraded", agentic_ok=agentic_ok,
                    code_bytes=code_bytes, files_on_disk=len(disk),
                    retries=attempt, reason=degraded_reason,
                )
                run_metadata["degraded"] = True
                run_metadata["degraded_reason"] = degraded_reason
            if disk and code_bytes >= threshold:
                # Preserve substantial partial work, but merge the deterministic
                # scaffold floor under it so common entrypoints/manifests are never
                # absent. ``degraded`` prevents an incomplete subset from shipping.
                files = ({**scaffold, **disk} if incomplete else disk)
                # Insurance against the entrypoint-clobber bug: a weak model can
                # ship an index.html that never imports the app (a standalone inline
                # page), so the real game never loads and entities stay primitive.
                files = self._repair_html_entrypoint(worktree, files, stack, scaffold)
                # Insurance against the orphaned-scene-graph bug: a model can split
                # the game into real Scene modules yet leave src/main.js running a
                # throwaway inline scene that never imports them, so the polished
                # game is dead code and only placeholders render. Auto-rewire main.js
                # to register the real scene modules.
                files = self._repair_scene_registry(worktree, files, stack)
            else:
                # Keep productive partial work even when it contains no source
                # extension counted by the byte threshold (for example generated
                # images, content JSON, or documentation written before the UI).
                # The degraded marker above still prevents this tree from being
                # mistaken for a complete delivery; the scaffold is only a
                # runnable floor beneath the partial output, not a replacement.
                files = {**scaffold, **disk}
                files = self._repair_html_entrypoint(worktree, files, stack, scaffold)
                files = self._repair_scene_registry(worktree, files, stack)
        else:
            # Completion backend (OpenRouter): per-file, generated CONCURRENTLY
            # (bounded) so a multi-file app's wall-clock is the slowest file.
            planned = self._planned_paths(plan, scaffold)
            expected_planned = self._agentic_planned_paths(plan)
            reported_expected = expected_planned
            generated: set[str] = set()
            sem = asyncio.Semaphore(self._gen_concurrency)

            async def _one(rel_path: str) -> tuple[str, str | None]:
                async with sem:
                    try:
                        return rel_path, await self._generate_file(
                            rel_path, brief, stack, plan, knowledge,
                            model_override=model_override, design=design,
                            design_tokens_md=_design_tokens_md)
                    except Exception:  # noqa: BLE001 - keep scaffold fallback for this file
                        return rel_path, None

            for rel_path, content in await asyncio.gather(*(_one(p) for p in planned)):
                if content and content.strip():
                    files[rel_path] = content
                    generated.add(rel_path)
            missing = sorted(expected_planned - generated)
            if missing:
                run_metadata["degraded"] = True
                run_metadata["degraded_reason"] = (
                    f"completion codegen missed {len(missing)} planned file(s): "
                    + ", ".join(missing[:12])
                )

        files = self._repair_entrypoints(stack, files, app_name)

        just_written = set(self._write_files(worktree, files))
        written = sorted(
            just_written
            | set(list_files(worktree))
            | self._present_planned_files(worktree, reported_expected)
            | preserved_asset_paths
        )

        out: dict[str, Any] = {
            "files_written": len(written),
            "worktree_dir": str(worktree),
            "stack": stack,
            "files": written,
            "backend": "stub" if _codegen_unavailable else self.llm.backend,
            # The exact prompt(s) this stage sent the model (empty on the offline
            # scaffold path). The runner lifts these into manifest.extra["prompts"].
            "prompts": run_metadata.get("prompts", []),
        }
        # Agent-reported pipeline friction (bounded); absent on vent-free builds,
        # so a build that never vents is byte-identical downstream to before.
        if run_metadata.get("vents"):
            out["vents"] = list(run_metadata["vents"])
        agentic = run_metadata.get("agentic")
        if isinstance(agentic, dict):
            out["agentic"] = {
                k: agentic.get(k)
                for k in (
                    "ok",
                    "backend",
                    "model",
                    "attempted_model",
                    "fallback_model",
                    "stalled",
                    "stall_reason",
                    "turn_timeouts",
                    "completed",
                    "complete",
                    "auto_converged",
                    "timed_out",
                    "files_written",
                    "planned_files",
                    "missing_files",
                    "turns",
                    "provider_requests",
                    "context_bytes_sent",
                    "max_context_bytes_sent",
                    "tool_calls",
                    "write_tool_calls",
                    "single_write_calls",
                    "batch_write_calls",
                    "write_argument_bytes_compacted",
                    "cached_tokens",
                    "cache_write_tokens",
                    "session_id",
                    "cli_execution",
                    "error",
                )
                if agentic.get(k) not in (None, "")
            }
        # Propagate degradation signal from the agentic path so downstream
        # scoring/verdict can see it. Never set on the stub or completion paths.
        if run_metadata.get("degraded"):
            out["degraded"] = True
            out["degraded_reason"] = run_metadata.get("degraded_reason", "unknown")
        codegen_unavailable = run_metadata.get("codegen_override_unavailable")
        if codegen_unavailable:
            out["codegen_override_unavailable"] = codegen_unavailable
        codegen_reason = run_metadata.get("codegen_override_reason")
        if codegen_reason:
            out["codegen_override_reason"] = codegen_reason

        return TaskResult(
            task_id=task.task_id, success=True,
            output=out,
        )

    # ---- helpers ---------------------------------------------------------
    def _resolve_worktree(self, payload: dict[str, Any]) -> Path:
        wd = payload.get("worktree_dir") or payload.get("project_dir")
        if wd:
            path = Path(wd)
        else:
            path = Path(tempfile.mkdtemp(prefix="skyn3t_code_"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _planned_paths(self, plan: dict[str, Any], scaffold: dict[str, str]) -> list[str]:
        paths: list[str] = []
        for f in (plan.get("files") or []):
            if isinstance(f, dict) and f.get("path"):
                paths.append(str(f["path"]))
            elif isinstance(f, str):
                paths.append(f)
        # Always ensure the scaffold's files exist as a runnable floor.
        for path in scaffold:
            if path not in paths:
                paths.append(path)
        return paths

    @staticmethod
    def _agentic_planned_paths(plan: dict[str, Any]) -> set[str]:
        """Safe file paths explicitly promised by the architect.

        The agentic builder may choose its own structure when no architecture was
        supplied. Once an architect names files, however, a large subset is not a
        complete delivery and must be resumed or marked degraded.
        """
        expected: set[str] = set()
        for item in plan.get("files") or []:
            raw = item.get("path") if isinstance(item, dict) else item
            rel = str(raw or "").strip().replace("\\", "/")
            if not rel or rel.endswith("/") or "\x00" in rel or ":" in rel:
                continue
            path = PurePosixPath(rel)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                continue
            expected.add(path.as_posix())
        return expected

    def _agentic_prompt(self, brief: str, stack: str, plan: dict[str, Any], knowledge: str,
                        *, art_plan: dict[str, Any] | None = None,
                        game_design: dict[str, Any] | None = None,
                        asset_foundry: dict[str, Any] | None = None,
                        design: dict[str, Any] | None = None,
                        moa_guidance: str = "",
                        design_tokens_md: str | None = None) -> str:
        files = plan.get("files") or []
        manifest = "\n".join(
            f"  {f['path']} — {f.get('purpose', '')}"
            for f in files if isinstance(f, dict) and f.get("path")
        )
        prompt = (
            f"{knowledge}"
            f"Build a COMPLETE, production-quality {stack} application for this brief:\n"
            f"{brief}\n\n"
            + (f"{_GAME_STACK_DIRECTIVE}\n\n" if stack == "phaser" else "")
            + (f"{_GAME_WIRING_DIRECTIVE}\n\n" if stack == "phaser" else "")
            + (f"{_GAME_FEEL_DIRECTIVE}\n\n" if stack == "phaser" else "")
            + (f"{self._game_depth_directive(brief, game_design)}\n\n" if stack == "phaser" else "")
            + (f"{_SWIFT_DIRECTIVE}\n\n" if stack == "swift" else "")
            + (f"{_SWIFT_IOS_DIRECTIVE}\n\n" if stack == "swift_ios" else "")
            + (f"{_MCP_DIRECTIVE}\n\n" if stack == "mcp" else "")
            + (f"{_RAG_DIRECTIVE}\n\n" if stack == "rag" else "")
            + (f"{_WORKFLOW_DIRECTIVE}\n\n" if stack == "workflow" else "")
            + (f"{_AGENT_PACK_DIRECTIVE}\n\n" if stack == "agent_pack" else "")
            + (f"{variant_directive(stack, brief)}\n\n"
               if variant_directive(stack, brief) else "")
            + f"Architecture summary: {plan.get('summary', '')}\n"
            + (f"Planned files:\n{manifest}\n\n" if manifest else "\n")
            + "Write ALL files into the CURRENT directory (create subfolders as needed). "
            "Make it a real, fully-featured, MULTI-FILE app — implement every feature in the "
            "brief with real logic and error handling. No placeholders, no TODOs, no stub "
            "functions. CRITICAL: provide a WORKING entrypoint at the project ROOT that wires "
            "the whole app together (for Python a top-level main.py whose `python main.py` "
            "actually runs the app; for web a real index/app entry) — do NOT leave a hello/"
            "greeting placeholder.\n"
            "DOCS (required, not optional): write a THOROUGH README.md — no placeholder "
            "READMEs. It MUST include, as Markdown sections: a title; a paragraph describing "
            "what the app does (specific to THIS brief); ## Features (the actual features you "
            "implemented); ## Installation (exact dependency-install commands); ## Usage (exact "
            "commands to run it, with example invocations / endpoints); and ## Project structure "
            "(a bullet per significant file/dir explaining its role). A one-line README is a "
            "failure.\n"
            "DEPENDENCY MANIFEST (required): write an ACCURATE manifest for the stack listing "
            "the REAL dependencies you actually import — requirements.txt (with a line per dep) "
            "or pyproject.toml for Python, package.json (with a populated dependencies block and "
            "a description field) for Node/JS. Do not leave it empty or omit deps you use.\n"
            + (f"{_DESIGN_DIRECTIVE}\n" if (stack or "").lower() in _DESIGN_WEB_STACKS else "")
            # Brief-derived tokens travel WITH the design bar, not in the
            # head-capped skill-advice bucket (a mature skill library truncated
            # them to 0 bytes). Same stack gate as the bar: never phaser/mobile.
            # A threaded design seed (best-of-N divergence) overrides the
            # derived block; default (falsy) derives exactly as before.
            + (f"{design_tokens_md or design_md_block(brief)}\n" if (stack or "").lower() in _DESIGN_WEB_STACKS else "")
            + (
                f"Follow this design direction: {self._design_summary(design)}\n"
                if self._design_summary(design) and (stack or "").lower() in _DESIGN_WEB_STACKS
                else ""
            )
            + (
                f"{self._game_art_directive(brief, art_plan, asset_foundry)}\n"
                if self._game_art_on(stack) else ""
            )
            + (
                f"{self._web_asset_directive(asset_foundry)}\n"
                if (stack or "").lower() in (_WEB_STACKS - {"phaser"}) else ""
            )
            + (f"{_DATA_DIRECTIVE}\n" if (stack or "").lower() in _DATA_STACKS else "")
            + (f"{_AGENT_APP_DIRECTIVE}\n" if _implies_agent_app(brief) else "")
            + f"{_FULL_FILE_CONTRACT}\n"
            + f"{_CONFIG_DIRECTIVE}\n"
            + f"{_VENT_DIRECTIVE}\n"
            + f"{_LLM_DIRECTIVE}\n"
            # Advisory-council guidance goes at the TAIL, after every directive.
            # The directive block above is byte-identical across every build of a
            # given stack; splicing brief-varying text into the middle would
            # fragment the longest span shared across builds and across
            # best-of-N trajectories. Appending keeps that span contiguous and
            # gives the advice recency. (Hermes appends for the same reason at a
            # different scale — agent/moa_loop.py:1303-1337.) Empty by default,
            # so a council-off build's prompt is byte-identical to before.
            + (f"\n{moa_guidance}\n" if moa_guidance else "")
            + "Do not ask questions — just build it."
        )
        return self.system_prompt(prompt)

    @staticmethod
    def _game_art_on(stack: str) -> bool:
        """Game stacks get the sprite directive when game art is enabled — the
        runner writes the sprite files, so codegen must reference them with a
        primitive fallback (else it rewrites main.js and drops the art)."""
        if stack != "phaser":
            return False
        from skyn3t.config.settings import get_settings

        return bool(getattr(get_settings(), "game_art_enabled", True))

    @staticmethod
    def _game_art_directive(
        brief: str,
        art_plan: dict[str, Any] | None = None,
        asset_foundry: dict[str, Any] | None = None,
    ) -> str:
        """Genre-aware game-art directive. Uses the runner-threaded ``art_plan`` when
        present (so codegen lists the SAME roles the sprite generator produced — the
        alignment guarantee for a non-deterministic LLM plan), else derives the plan
        from the brief deterministically. A geometric genre is told to render crisp
        styled primitives and load NO sprites ($0); a sprite genre gets the per-role
        load+primitive-fallback idiom. Always one shared palette."""
        from skyn3t.agents.art_director import ArtPlan, direct_art

        plan = ArtPlan.from_dict(art_plan) if isinstance(art_plan, dict) else direct_art(brief)
        palette = " ".join(plan.palette)
        sprites = plan.sprite_roles()
        prims = plan.primitive_roles()
        foundry_paths: list[str] = []
        if isinstance(asset_foundry, dict):
            selected = asset_foundry.get("selected")
            if isinstance(selected, dict):
                for entry in selected.values():
                    if isinstance(entry, dict) and entry.get("path"):
                        foundry_paths.append(str(entry["path"]))
        foundry_clause = ""
        if foundry_paths:
            foundry_clause = (
                "ASSET FOUNDRY — exact served paths already selected for this build: "
                + ", ".join(sorted(dict.fromkeys(foundry_paths)))
                + ". Load and reference ONLY these exact paths; do not invent extra "
                "sprite/audio files outside the manifest.\n"
            )

        if plan.open_ended:
            # A game the genre table doesn't recognize: don't pin a role list — give
            # codegen the sprite-vs-primitive RULE + palette + a baseline sprite set,
            # and let it render the entities THIS brief implies.
            sprite_list = ", ".join(sprites)
            fallback_hex = plan.palette[1].lstrip("#")
            return (
                "GAME ART — this is an open-ended game: render the entities THIS "
                "brief implies, using roles APPROPRIATE to the actual game (not a "
                "fixed list). RULE per entity: a character / creature / vehicle / "
                "collectible / themed object = a SPRITE; a geometric shape, "
                "projectile, platform, wall, HUD bar, or abstract element = a clean "
                "styled PRIMITIVE from the palette. Use this EXACT shared palette "
                f"(hex): {palette}. BASELINE sprites ARE generated at "
                f"/assets/sprites/<role>.png for: {sprite_list} — load EACH in "
                "preload() via `this.load.image('<role>', '/assets/sprites/"
                "<role>.png')` (these PNGs ALREADY EXIST at build time and ARE the "
                "real art; fabricating textures in code with `make.graphics()` + "
                "`.generateTexture(...)` is FORBIDDEN — it discards the art). Render "
                "EVERY on-screen instance AS A PHASER SPRITE "
                "that uses the texture: `this.add.sprite(x, y, '<role>')` for one "
                "entity, or a `this.physics.add.group()` keyed to that texture for "
                "many bullets / enemies / pickups (spawn and recycle sprites from the "
                "group). Load ONLY textures that exist — the baseline list above is "
                "EXHAUSTIVE on disk; do NOT load any other /assets/ path (an invented "
                "'gold.png'/'coin.png' 404s and CRASHES the scene at runtime): render "
                "currency, score and HUD icons as palette-styled primitives or text "
                "instead. Set a clear depth order for foreground/background layers with "
                "`setDepth(...)` when the scene has more than one visual plane. Do NOT "
                "draw a role that HAS a sprite with this.add.graphics(), "
                "fillRect, fillCircle or this.add.rectangle — that is a BUG; the ONLY "
                "allowed fallback is a MISSING texture, guarded by "
                f"`if (!this.textures.exists('<role>')) {{ /* a 0x{fallback_hex} "
                "rectangle */ }}`. Scale each sprite to its gameplay size with "
                "`setDisplaySize(w, h)` and, when helpful, center with "
                "`setOrigin(0.5, 0.5)`. For any other role this game needs, reuse a "
                "fitting baseline sprite or draw a styled primitive from the palette; "
                "render a rich LAYERED background (parallax layers + a subtle gradient, "
                f"NOT a single flat fill) over ~{plan.palette[0]}. Art is a RENDER "
                "concern in src/main.js ONLY; keep ALL game logic in the pure "
                "src/sim.js unchanged."
            )

        if not sprites:
            ents = ", ".join(f"{r.role} ({r.color})" for r in prims.values())
            return (
                f"GAME ART — genre '{plan.genre}', a GEOMETRIC game: render EVERY "
                "entity as a clean styled PRIMITIVE (crisp Phaser rectangles/circles "
                "with a subtle glow), NOT sprites and NOT muddy gradients, and load "
                "NO sprite image. Use this EXACT shared palette (hex): "
                f"{palette}. Entities and their colors: {ents}; background "
                f"~{plan.palette[0]}. Art is a RENDER concern in src/main.js ONLY; "
                "keep ALL game logic in the pure src/sim.js unchanged."
            )

        example = next(iter(sprites))
        sprite_list = ", ".join(sprites)
        prim_list = (
            ", ".join(f"{r.role} ({r.color})" for r in prims.values()) or "none"
        )
        hex_no_hash = plan.roles[example].color.lstrip("#")
        bg = plan.roles.get("background")
        bg_subject = bg.subject if bg is not None else "the game's setting"
        pow_sprites = [k for k in sprites if k.startswith("powerup")]
        powerup_clause = (
            f"POWER-UP VARIETY — {', '.join(pow_sprites)}: these are DISTINCT power-up "
            "TYPES, each with its OWN sprite. Render EACH power-up kind with its OWN "
            "matching texture (e.g. a shield power-up uses `powerup_shield`, rapid-fire "
            "uses `powerup_rapid`) so different power-ups LOOK different — NEVER draw "
            "every power-up with the same texture.\n"
            if len(pow_sprites) > 1 else ""
        )
        return (
            f"GAME ART — genre '{plan.genre}'. Use this EXACT shared palette (hex): "
            f"{palette}.\n"
            f"{foundry_clause}"
            f"SPRITE ROLES — {sprite_list}: in preload() load EACH via "
            "`this.load.image('<role>', '/assets/sprites/<role>.png')` — these PNG "
            "files ALREADY EXIST at build time and ARE the game's real art. Do NOT "
            "fabricate textures in code: calling `make.graphics()`/`add.graphics()` "
            "then `.generateTexture('<role>', …)` (or otherwise hand-drawing these "
            "roles) is FORBIDDEN — it throws away the generated art and is a BUG. "
            "Then render EVERY on-screen instance of that role AS A PHASER SPRITE "
            "that uses the loaded texture — never as a hand-drawn shape. One entity: "
            f"`this.add.sprite(x, y, '{example}')` (or `this.physics.add.sprite`). "
            "MANY entities (bullets, enemies, pickups): make a Phaser GROUP keyed to "
            "the texture and spawn/recycle sprites from it — e.g. `this.enemies = "
            "this.physics.add.group(); this.enemies.create(x, y, 'enemy')` — do NOT "
            "draw them with this.add.graphics(), fillRect, fillCircle or "
            "this.add.rectangle. Scale each sprite to its gameplay size with "
            "`setDisplaySize(w, h)` and, when helpful, center with "
            "`setOrigin(0.5, 0.5)`. Rendering an entity that HAS a generated texture "
            "as a primitive is a BUG; the ONLY allowed fallback is a MISSING texture, "
            f"guarded by `if (!this.textures.exists('{example}')) {{ /* then a "
            f"0x{hex_no_hash} rectangle */ }}`.\n"
            f"{powerup_clause}"
            f"PRIMITIVE ROLES — {prim_list}: these have NO sprite file — draw them as "
            "clean styled shapes from the palette color shown. Give all background and "
            "foreground elements explicit depth ordering so overlaps stay readable.\n"
            "The SPRITE ROLES list is EXHAUSTIVE: load ONLY textures that exist — do "
            "NOT load any other /assets/ path (an invented 'gold.png'/'coin.png' 404s "
            "and CRASHES the scene at runtime). Currency, score and HUD icons are NOT "
            "sprites: render them as palette-styled primitives or text.\n"
            f"BACKGROUND ({bg_subject}): render a rich, LAYERED backdrop with depth — "
            "several parallax layers, a subtle gradient/atmosphere and a few brighter "
            f"accent details over ~{plan.palette[0]} — NOT a single flat fill. "
            "The sprite files are generated into public/assets/sprites/ by the build — "
            "do NOT create them yourself, just reference them. Art is a RENDER concern "
            "in src/main.js ONLY; keep ALL game logic in the pure src/sim.js unchanged."
        )

    @staticmethod
    def _web_asset_directive(asset_foundry: dict[str, Any] | None = None) -> str:
        if not isinstance(asset_foundry, dict) or asset_foundry.get("type") != "web":
            return ""
        selected = asset_foundry.get("selected")
        if not isinstance(selected, dict):
            return ""
        paths = {
            key: str(value.get("path") or "")
            for key, value in selected.items()
            if isinstance(value, dict) and value.get("path")
        }
        hero = paths.get("web/hero")
        og = paths.get("web/og")
        favicon = paths.get("web/favicon")
        if not any((hero, og, favicon)):
            return ""
        parts = [
            "WEB ASSET FOUNDRY — exact served image paths already exist for this build.",
            "Use these real assets; do not invent stock-image URLs, remote CDNs, or missing /assets paths.",
        ]
        if hero:
            # "Render it visibly" was the previous wording and was not enough.
            # Measured on a delivered site: the model wired the favicon and the
            # og:image — both mechanical one-liners in <head> — and skipped the
            # hero, the only asset needing a layout decision in the page body.
            # So name the element and the alt text and state the consequence,
            # rather than describing the intent. Placement stays with the design:
            # an earlier "above-the-fold hero/banner" mandate made every page a
            # banner-band template.
            parts.append(
                f"Hero image: `{hero}`. The primary page MUST render an `<img>` tag "
                f"(or this stack's image component) whose src is `{hero}`, with "
                "descriptive alt text. Place it where it strengthens THIS design — "
                "a hero, feature, about or media section, sized and cropped by the "
                "layout — never as a boilerplate full-width banner band pasted at "
                "the top. Wiring it into metadata alone does NOT satisfy this: a "
                "page that never displays the hero fails acceptance."
            )
        if og:
            parts.append(f"Open Graph image: `{og}`. Wire it into metadata when the stack supports it.")
        if favicon:
            parts.append(f"Favicon: `{favicon}`. Reference it from the app metadata/head.")
        return " ".join(parts)

    @staticmethod
    def _game_depth_directive(brief: str, game_design: dict[str, Any] | None = None) -> str:
        """Demand DEPTH so the cheap model can't ship a thin one-mechanic toy
        (roadmap #7). Uses the runner-threaded GDD when present (LLM-tailored), else
        derives it deterministically from the brief. Every element below is a hard
        requirement; all of it lives in the pure src/sim.js so the headless gate and
        art tier keep working unchanged."""
        from skyn3t.agents.game_designer import GameDesign, design_game

        gd = (
            GameDesign.from_dict(game_design)
            if isinstance(game_design, dict)
            else design_game(brief)
        )
        n_pow = max(3, min(5, len(gd.powerups)))
        return (
            "GAME DEPTH (required — a thin one-mechanic toy is a FAIL): build a "
            f"COMPLETE {gd.genre} game with real depth.\n"
            f"- CORE LOOP: {gd.core_loop}.\n"
            f"- WIN: {gd.win}. LOSE: {gd.lose}. Both must be REACHABLE and shown to "
            "the player (a win/lose screen or banner).\n"
            f"- PROGRESSION: {gd.progression} — NOT a single static screen.\n"
            "- EXPLICIT BRIEF COUNTS ARE HARD CONTRACTS: if the brief says 120 levels, "
            "12 islands, 4 phases, waves, stages, characters, or similar counts, create "
            "real data/logic for those counts in src/sim.js and visible UI progress. "
            "Do NOT shrink them to a sample, demo, landing page, or a few labeled buttons.\n"
            f"- MECHANICS (implement each, interacting): {', '.join(gd.mechanics)}.\n"
            f"- POWER-UPS / UPGRADES (at least {n_pow}, with REAL gameplay effects, "
            f"not just labels): {', '.join(gd.powerups)}.\n"
            f"- VARIETY (distinct types with different behavior): {', '.join(gd.variety)}.\n"
            f"- ECONOMY / SCORING: {gd.economy}.\n"
            "Put ALL of this in the pure src/sim.js state + step() (the Phaser scene "
            "only renders it), so it stays deterministic and testable."
        )

    def _codegen_agentic_routing(
        self, stack: str, model_override: Any, payload: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any], str]:
        payload = payload if isinstance(payload, dict) else {}
        extra_value = payload.get("extra")
        extra: dict[str, Any] = extra_value if isinstance(extra_value, dict) else {}
        timeout_value = payload.get("agentic_timeout") or extra.get("agentic_timeout")
        try:
            timeout = int(timeout_value) if timeout_value else None
        except (TypeError, ValueError):
            timeout = None
        runtime = {"timeout": timeout} if timeout else {}
        live_settings = getattr(self.llm, "settings", None)
        codegen_provider = str(
            getattr(live_settings, "codegen_cli_provider", "") or "").strip().lower()
        codegen_model = (
            str(getattr(live_settings, "codegen_cli_model", "") or "").strip() or None
        )
        codegen_cli_ok = bool(codegen_provider) and self.llm._cli_available(codegen_provider)
        if codegen_cli_ok:
            return True, {
                "provider": codegen_provider,
                "model": codegen_model,
                "stack": stack,
                **runtime,
            }, ""
        if codegen_provider:
            # Keep the explicit provider in the call contract as defence in
            # depth: even if a future caller ignores ``unavailable``,
            # LLMClient.agentic_build will reject this provider instead of
            # switching to the global OpenRouter backend.
            return False, {
                "provider": codegen_provider,
                "model": codegen_model,
                "stack": stack,
                **runtime,
            }, codegen_provider
        return False, {"model": model_override, "stack": stack, **runtime}, ""

    # ---- parallel code slicing (Hermes orchestrator-worker) --------------
    async def _execute_slice(
        self, task: TaskRequest, p: dict[str, Any], brief: str, stack: str,
        plan: dict[str, Any], app_name: str, worktree: Path,
        slice_scope: dict[str, Any],
        acceptance_contracts: dict[str, bytes],
        restored_contracts: set[str],
    ) -> TaskResult:
        """Generate ONLY this slice's files. The full file manifest is supplied as
        read-only context so cross-slice imports line up. On under-delivery the
        slice falls back to its own scaffold subset (a runnable floor) and flags
        degraded — mirroring the monolithic path so a vanished slice can't ship a
        silent hole."""
        name = str(slice_scope.get("name") or "slice")
        slice_files = [str(x) for x in (slice_scope.get("files") or [])]
        owned_paths = self._agentic_planned_paths({"files": slice_files})
        slice_baseline = self._snapshot_regular_files(worktree)
        manifest = str(slice_scope.get("manifest") or "")
        model_override = p.get("model_override")
        codegen_cli_ok, agentic_kwargs, codegen_unavailable = self._codegen_agentic_routing(
            stack, model_override, p)
        if codegen_unavailable:
            self._task_metadata["codegen_override_unavailable"] = codegen_unavailable
            self._task_metadata["codegen_override_reason"] = (
                f"{codegen_unavailable} CLI was explicitly selected for codegen but "
                "is unavailable; kept the offline slice without an OpenRouter fallback."
            )
        knowledge = knowledge_block(p)
        raw_prior = p.get("prior")
        prior: dict[str, Any] = raw_prior if isinstance(raw_prior, dict) else {}
        raw_extra = p.get("extra")
        extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
        design_tokens_md = _seed_tokens_md(extra)
        profile = profile_from_payload(extra.get("layout_profile"))
        raw_design = self._unwrap_design_payload(prior.get("design"))
        design = self._design_with_layout_profile(
            raw_design if isinstance(raw_design, dict) else None, profile,
        )
        files: dict[str, str] = {}
        degraded_reason = ""

        if codegen_unavailable:
            files = self._slice_stub(stack, app_name, brief, slice_files)
        elif self.llm.backend == "stub" and not codegen_cli_ok:
            # Offline: emit a minimal non-empty stub per slice file so the
            # orchestration is testable and the merge produces real files.
            files = self._slice_stub(stack, app_name, brief, slice_files)
        elif getattr(self.llm, "supports_agentic", False) or codegen_cli_ok:
            from skyn3t.config.settings import get_settings

            expected = owned_paths
            max_retries = max(0, int(getattr(get_settings(), "agentic_retries", 1)))
            disk: dict[str, str] = {}
            missing = sorted(expected)
            confirmed = False
            agentic_error = ""
            prose_seen: set[str] = set()
            out_of_scope_seen: set[str] = set()
            attempt = 0
            res: dict[str, Any] = {}
            while True:
                prompt = (
                    self._agentic_slice_prompt(
                        brief, stack, name, slice_files, manifest, knowledge, design=design,
                        design_tokens_md=design_tokens_md)
                    if attempt == 0
                    else self._agentic_slice_resume_prompt(
                        brief, stack, name, slice_files, disk, missing, agentic_error,
                        design=design, design_tokens_md=design_tokens_md)
                )
                self._task_metadata.setdefault("prompts", []).append({
                    "stage": f"codegen_slice_{name}"
                    if attempt == 0 else f"codegen_slice_{name}_retry_{attempt}",
                    "chars": len(prompt),
                    "text": prompt,
                })
                res = await self.llm.agentic_build(
                    prompt,
                    str(worktree),
                    allowed_paths=sorted(expected),
                    enforce_antistub=False,
                    verify_on_stop=False,
                    **agentic_kwargs,
                )
                restored_contracts.update(
                    restore_acceptance_contracts(worktree, acceptance_contracts)
                )
                agentic_ok = bool(res.get("ok", True))
                confirmed = agentic_ok and bool(res.get("completed", agentic_ok))
                agentic_error = str(res.get("error", "") or "")
                await self._capture_vents(res, app_name, attempt)
                out_of_scope_seen.update(
                    self._prune_slice_out_of_scope(worktree, expected, slice_baseline)
                )
                disk = {
                    path: content
                    for path, content in self._read_files(worktree).items()
                    if path in expected
                }
                # Reject chat-prose source files (no scaffold to revert to -> dropped).
                disk, prose = self._clean_agentic_files(disk, {})
                self._unlink_rejected_agentic_files(worktree, prose, {})
                prose_seen.update(prose)
                present_owned = self._present_planned_files(worktree, expected)
                missing = sorted(expected - present_owned)
                if (confirmed and not missing) or attempt >= max_retries:
                    break
                attempt += 1

            complete = confirmed and not missing
            res["completed"] = confirmed
            res["complete"] = complete
            res["planned_files"] = len(expected)
            res["missing_files"] = missing[:50]
            res["out_of_scope_files"] = sorted(out_of_scope_seen)[:50]
            self._task_metadata["agentic"] = res
            if prose_seen:
                self._task_metadata["prose_rejected"] = sorted(prose_seen)
            if complete:
                files = disk
            else:
                reasons: list[str] = []
                if not confirmed:
                    reasons.append(
                        "provider did not confirm completion"
                        + (f": {agentic_error}" if agentic_error else "")
                    )
                if missing:
                    reasons.append(
                        f"missing {len(missing)} owned file(s): " + ", ".join(missing[:12])
                    )
                if prose_seen:
                    reasons.append("rejected prose source: " + ", ".join(sorted(prose_seen)[:8]))
                if out_of_scope_seen:
                    reasons.append(
                        "removed out-of-scope writes: "
                        + ", ".join(sorted(out_of_scope_seen)[:8])
                    )
                degraded_reason = (
                    f"agentic slice '{name}' incomplete after {attempt} retr"
                    f"{'y' if attempt == 1 else 'ies'}: "
                    + "; ".join(reasons or ["completion contract not satisfied"])
                )
                # Keep every useful partial file and add only the deterministic
                # owned-file floor beneath it. The degraded marker prevents this
                # recovery tree from being mistaken for a complete specialist merge.
                files = {**self._slice_stub(stack, app_name, brief, missing), **disk}
        else:
            # Completion backend: generate just this slice's files concurrently,
            # each pinned to the slice's tier model when provided. Pass the full
            # cross-slice manifest so each file imports the right sibling paths.
            sem = asyncio.Semaphore(self._gen_concurrency)

            async def _one(rel: str) -> tuple[str, str | None]:
                async with sem:
                    try:
                        return rel, await self._generate_file(
                            rel, brief, stack, plan, knowledge,
                            model_override=model_override, manifest=manifest, design=design,
                            design_tokens_md=design_tokens_md)
                    except Exception:  # noqa: BLE001 - isolate per file
                        return rel, None

            generated: set[str] = set()
            for rel, content in await asyncio.gather(*(_one(r) for r in slice_files)):
                if content and content.strip():
                    files[rel] = content
                    generated.add(rel)
            missing = sorted(owned_paths - generated)
            if missing:
                files = {**self._slice_stub(stack, app_name, brief, slice_files), **files}
                degraded_reason = (
                    f"completion slice '{name}' missed {len(missing)} owned file(s): "
                    + ", ".join(missing[:12])
                )

        written = sorted(
            set(self._write_files(worktree, files))
            | self._present_planned_files(worktree, owned_paths)
        )
        out: dict[str, Any] = {
            "files_written": len(written), "worktree_dir": str(worktree),
            "stack": stack, "files": written,
            "backend": "stub" if codegen_unavailable else self.llm.backend,
            "slice": name,
            "prompts": self._task_metadata.get("prompts", []),
        }
        if self._task_metadata.get("vents"):
            out["vents"] = list(self._task_metadata["vents"])
        agentic = self._task_metadata.get("agentic")
        if isinstance(agentic, dict):
            out["agentic"] = {
                key: agentic.get(key)
                for key in (
                    "ok", "backend", "model", "completed", "complete",
                    "auto_converged", "timed_out",
                    "files_written", "planned_files", "missing_files",
                    "turns", "provider_requests", "context_bytes_sent",
                    "max_context_bytes_sent", "tool_calls", "write_tool_calls",
                    "single_write_calls", "batch_write_calls",
                    "write_argument_bytes_compacted", "cached_tokens",
                    "cache_write_tokens", "session_id",
                    "cli_execution",
                    "out_of_scope_files", "error",
                )
                if agentic.get(key) not in (None, "")
            }
        if degraded_reason:
            out["degraded"] = True
            out["degraded_reason"] = degraded_reason
        codegen_unavailable_out = self._task_metadata.get("codegen_override_unavailable")
        if codegen_unavailable_out:
            out["codegen_override_unavailable"] = codegen_unavailable_out
        codegen_reason_out = self._task_metadata.get("codegen_override_reason")
        if codegen_reason_out:
            out["codegen_override_reason"] = codegen_reason_out
        return TaskResult(task_id=task.task_id, success=True, output=out)

    def _agentic_slice_prompt(
        self, brief: str, stack: str, slice_name: str, slice_files: list[str],
        manifest: str, knowledge: str, design: dict[str, Any] | None = None,
        design_tokens_md: str | None = None,
    ) -> str:
        want = "\n".join(f"  {f}" for f in slice_files) or "  (none listed)"
        # The frontend slice owns the look — give it the design bar + the chosen
        # design tokens so it doesn't ship the generic emoji-template UI. Other
        # slices (config/tests/backend) stay lean.
        design_block = ""
        if (
            (slice_name == "frontend" or slice_name.startswith("frontend_"))
            and (stack or "").lower() in _DESIGN_WEB_STACKS
        ):
            design_block = f"\n\n{_DESIGN_DIRECTIVE}\n\n{design_tokens_md or design_md_block(brief)}"
            summary = self._design_summary(design)
            if summary:
                design_block += f"\nFollow this design direction: {summary}"
        prompt = (
            f"{knowledge}"
            f"You are building the **{slice_name}** part of a larger {stack} application, "
            f"in parallel with other agents building the rest. Brief:\n{brief}\n\n"
            f"Write ONLY these files (create subfolders as needed) — fully implemented, "
            f"production-quality, no placeholders or TODOs:\n{want}\n\n"
            f"The REST of the app is being written by other agents at these exact paths — "
            f"do NOT create them, but import from them by these exact paths so the code "
            f"coheres:\n{manifest}\n\n"
            "Implement real logic and error handling for your files only. Match the import "
            "paths above exactly. Do not ask questions — just write your files."
            f"{design_block}"
            f"\n\n{_VENT_DIRECTIVE}"
        )
        return self.system_prompt(prompt)

    def _agentic_slice_resume_prompt(
        self,
        brief: str,
        stack: str,
        slice_name: str,
        slice_files: list[str],
        files: dict[str, str],
        missing: list[str],
        previous_error: str,
        design: dict[str, Any] | None = None,
        design_tokens_md: str | None = None,
    ) -> str:
        """Compact continuation for an incomplete specialist slice."""
        remaining = "\n".join(f"- {path}" for path in missing) or (
            "- No owned path is absent; validate the existing slice and finish successfully."
        )
        existing = "\n".join(f"- {path}" for path in sorted(files)[:120]) or "- (none)"
        owned = "\n".join(f"- {path}" for path in slice_files) or "- (none)"
        error = previous_error or "the prior session ended without confirmed completion"
        design_block = ""
        if (
            (slice_name == "frontend" or slice_name.startswith("frontend_"))
            and (stack or "").lower() in _DESIGN_WEB_STACKS
        ):
            summary = self._design_summary(design)
            if summary:
                design_block = (
                    f"\n\n{_DESIGN_DIRECTIVE}\n\n{design_tokens_md or design_md_block(brief)}"
                    f"\nFollow this design direction: {summary}"
                )
        return self.system_prompt(
            "RESUME THIS SLICE IN PLACE. Preserve every working file and do not restart "
            "or reduce the implementation.\n\n"
            f"Brief: {brief}\nStack: {stack}\nSlice: {slice_name}\n"
            f"Prior issue: {error}\n\n"
            f"Owned paths (do not write outside these):\n{owned}\n\n"
            f"Still missing:\n{remaining}\n\n"
            f"Already present (inspect before editing):\n{existing}\n\n"
            "Implement every missing owned file in full, repair wiring among the owned "
            "files, and finish successfully only when the entire slice is complete."
            + design_block
            + f"\n\n{_VENT_DIRECTIVE}"
        )

    @staticmethod
    def _design_with_layout_profile(
        design: dict[str, Any] | None, profile: LayoutProfile,
    ) -> dict[str, Any]:
        """Preserve the runner-frozen profile when design is absent or degraded."""
        merged = dict(design or {})
        contract = CodeAgent._profile_layout_contract(profile)
        merged["layout_profile"] = profile.to_dict()
        merged["layout_contract"] = [contract]
        return merged

    @staticmethod
    def _profile_layout_contract(profile: LayoutProfile) -> str:
        contract = layout_contract_block(profile)
        if profile.name == "workspace":
            return contract + " Use an asymmetric grid or split pane; do not use uniform cards."
        if profile.name == "editorial":
            return contract + " A constrained reading column is permitted where it suits the content."
        return contract

    @staticmethod
    def _unwrap_design_payload(raw_design: Any) -> Any:
        """The designer stage returns {"design": {...}, "model"/"backend": ...}
        and the runner stores that verbatim in prior["design"] — unwrap the
        payload when nested, else the whole design direction silently drops
        out of every codegen prompt."""
        if isinstance(raw_design, dict) and isinstance(raw_design.get("design"), dict):
            return raw_design["design"]
        return raw_design

    @staticmethod
    def _design_summary(design: dict[str, Any] | None) -> str:
        """Condense the design stage's tokens (theme/palette/typography/layout) into
        a one-line direction for the codegen prompt. '' when none."""
        if not isinstance(design, dict):
            return ""
        bits: list[str] = []
        if design.get("theme"):
            bits.append(f"theme={design['theme']}")
        raw_profile = design.get("layout_profile")
        if isinstance(raw_profile, dict):
            profile = profile_from_payload(raw_profile)
            if profile.to_dict() == raw_profile:
                bits.append(f"LAYOUT PROFILE: {profile.name}")
                contract = design.get("layout_contract") or [
                    CodeAgent._profile_layout_contract(profile)
                ]
                bits.append(f"layout_contract={contract}")
        pal = design.get("palette")
        if isinstance(pal, dict) and pal:
            bits.append("palette(" + ", ".join(f"{k}:{v}" for k, v in pal.items()) + ")")
        for key in ("typography", "layout", "components", "states"):
            val = design.get(key)
            if val:
                bits.append(f"{key}={val}")
        # The frozen desktop contract is intentionally complete provenance, not
        # a profile name that can be reformatted from current defaults. Keep the
        # bounded summary large enough to carry that contract plus its
        # profile-specific implementation direction and design tokens.
        return "; ".join(bits)[:2_000]

    # Stub content per extension for the offline slice path (non-empty + valid).
    @staticmethod
    def _minimal_stub(rel: str) -> str:
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if ext == "py":
            return f"# {rel} (slice stub)\n"
        if ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
            return f"// {rel} (slice stub)\nexport {{}};\n"
        if ext in ("css", "scss", "sass", "less"):
            return f"/* {rel} (slice stub) */\n"
        if ext == "html":
            return f"<!-- {rel} -->\n<!doctype html><div id=\"root\"></div>\n"
        if ext == "json":
            return "{}\n"
        return f"{rel} (slice stub)\n"

    def _slice_stub(self, stack: str, app_name: str, brief: str,
                    slice_files: list[str]) -> dict[str, str]:
        """Offline slice content: reuse the stack scaffold for known paths, a
        minimal stub for the rest."""
        full = scaffold_for(stack, app_name, brief)
        return {
            rel: full.get(rel) or self._minimal_stub(rel)
            for rel in slice_files
            if Path(rel).suffix.lower() not in self._BINARY_EXTS
        }

    _CODE_EXTS = (
        "py", "js", "jsx", "mjs", "cjs", "ts", "tsx", "go", "rs", "rb",
        "php", "java", "kt", "kts", "swift", "dart", "cs", "c", "cc", "cpp",
        "cxx", "h", "hpp", "astro", "html", "htm", "css", "scss", "sass",
        "less", "vue", "svelte",
    )
    _THRESHOLD_CODE_EXTS = ("py", "js", "jsx", "ts", "tsx", "go", "rs", "rb")

    @classmethod
    def _code_bytes(cls, files: dict[str, str]) -> int:
        """Total bytes across substantive source and user-facing markup/style files."""
        return sum(
            len(c) for f, c in files.items()
            if f.rsplit(".", 1)[-1] in cls._CODE_EXTS
        )

    @classmethod
    def _threshold_code_bytes(cls, files: dict[str, str]) -> int:
        """Legacy scaffold baseline, kept stable as substantive formats expand."""
        return sum(
            len(content)
            for path, content in files.items()
            if path.rsplit(".", 1)[-1] in cls._THRESHOLD_CODE_EXTS
        )

    @staticmethod
    def _agentic_contract_gap(stack: str, files: dict[str, str]) -> str:
        """Stack-specific must-have checks for substantial agentic output.

        These are retry triggers, not proof substitutes. In particular, Phaser's
        headless invariant gate can only verify a generated game when the model
        preserves the pure simulation split; a large scene-only game is still
        unverifiable and should get another codegen attempt before fallback.
        """
        if (stack or "").lower() == "phaser":
            if "src/sim.js" not in files:
                return "missing pure src/sim.js simulation core"

            source = "\n".join(
                content for rel, content in files.items()
                if rel.rsplit(".", 1)[-1].lower() in {"js", "jsx", "ts", "tsx"}
            )
            has_phaser_boot = "new Phaser.Game" in source
            has_scene_shape = bool(
                re.search(r"\bPhaser\.Scene\b|extends\s+Phaser\.Scene\b", source)
            )
            looks_like_react_shell = bool(
                re.search(
                    r"react-router-dom|ReactDOM\.createRoot|createRoot\(|<Routes\b|"
                    r"src/App\.(jsx|tsx)|src/pages/",
                    "\n".join(f"{rel}\n{content}" for rel, content in files.items()),
                )
            )
            if looks_like_react_shell and not has_phaser_boot:
                return (
                    "Phaser game contract violated: output looks like a React "
                    "website shell and lacks new Phaser.Game"
                )
            if not has_phaser_boot:
                return "missing Phaser game boot: new Phaser.Game"
            if not has_scene_shape:
                return "missing Phaser game scene: Phaser.Scene"
        return ""

    def _agentic_retry_prompt(
        self, brief: str, stack: str, plan: dict[str, Any], knowledge: str,
        code_bytes: int, *, art_plan: dict[str, Any] | None = None,
        game_design: dict[str, Any] | None = None,
        asset_foundry: dict[str, Any] | None = None,
        design: dict[str, Any] | None = None,
    ) -> str:
        """Corrective prompt for a retry after the agent under-delivered. Threads the
        SAME art_plan + game_design so the retry's art/depth directives still match
        the sprites the generator produced and the GDD the run committed to."""
        return (
            f"Your previous attempt under-delivered — it wrote only {code_bytes} "
            "bytes of code, essentially just the starter template / a placeholder "
            "(e.g. a `count is N` demo counter). That is NOT acceptable.\n\n"
            "Now build the COMPLETE, real, multi-file application for the brief — "
            "every feature implemented with real logic, multiple pages/components "
            "wired together into the entrypoint, real state and data. NO placeholder "
            "counter, NO 'starter' text, NO TODOs or stubs. Get as close to a "
            "fully-working app as possible.\n\n"
            + self._agentic_prompt(
                brief, stack, plan, knowledge, art_plan=art_plan,
                game_design=game_design, asset_foundry=asset_foundry,
                design=design)
        )

    def _agentic_resume_prompt(
        self,
        brief: str,
        stack: str,
        plan: dict[str, Any],
        files: dict[str, str],
        missing: list[str],
        previous_error: str,
        contract_gap: str,
        design: dict[str, Any] | None = None,
        design_tokens_md: str | None = None,
    ) -> str:
        """Compact in-place continuation after an incomplete agentic session.

        Repeating the full ~14K initial prompt wastes the small recovery window
        and encourages wholesale rewrites. The new session can inspect the shared
        worktree, so give it the brief, exact remaining contract, and prior error.
        """
        purposes = {
            str(item.get("path", "")).replace("\\", "/"): str(item.get("purpose", ""))
            for item in plan.get("files") or []
            if isinstance(item, dict) and item.get("path")
        }
        remaining = "\n".join(
            f"- {path}" + (f" — {purposes[path]}" if purposes.get(path) else "")
            for path in missing
        ) or "- No path is absent; inspect the existing files and finish validation/wiring."
        existing = "\n".join(f"- {path}" for path in sorted(files)[:120]) or "- (none)"
        issues = "\n".join(
            f"- {issue}" for issue in (previous_error, contract_gap) if issue
        ) or "- The prior session ended without confirming completion."
        variant = variant_directive(stack, brief)
        return (
            "RESUME IN PLACE. A previous codegen session ended before the complete "
            "architecture was confirmed. Preserve working files and do not restart or "
            "replace the product with a smaller demo.\n\n"
            f"Brief: {brief}\nStack: {stack}\n"
            f"Architecture: {plan.get('summary', '')}\n\n"
            f"Prior completion issues:\n{issues}\n\n"
            f"Files still required by the approved architecture:\n{remaining}\n\n"
            f"Files already present (inspect before editing):\n{existing}\n\n"
            + (f"Variant contract: {variant}\n\n" if variant else "")
            + (
                f"{_DESIGN_DIRECTIVE}\n{design_tokens_md or design_md_block(brief)}\n"
                f"Follow this design direction: {self._design_summary(design)}\n\n"
                if self._design_summary(design) and (stack or "").lower() in _DESIGN_WEB_STACKS
                else ""
            )
            + f"{_VENT_DIRECTIVE}\n\n"
            + "Implement every missing file in full, repair imports and entrypoint wiring, "
            "and validate the complete existing app. Do not omit features, TODO, or elide "
            "code. Finish successfully only after the whole planned application is present."
        )

    # `assets` holds pre-generated binary images (Replicate). Skipping it keeps
    # those bytes off the text round-trip below — reading a PNG with errors=
    # "ignore" then re-writing it via _write_files would corrupt it.
    _SKIP_PARTS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".pytest_cache", "dist", ".next", "assets"})
    _BINARY_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                              ".bmp", ".svg", ".pdf", ".woff", ".woff2", ".ttf"})
    _AGENTIC_NEW_ROOT_FILES = frozenset({
        ".env.example", "CREDITS.md", "Dockerfile", "LICENSE", "README.md",
        "eslint.config.js", "index.css", "index.html", "main.js", "main.jsx",
        "main.ts", "main.tsx", "next.config.js", "next.config.mjs",
        "package-lock.json", "package.json", "pnpm-lock.yaml", "pyproject.toml",
        "requirements.txt", "script.js", "style.css", "styles.css", "tsconfig.json",
        "vite.config.js", "vitest.config.js", "yarn.lock",
    })
    _AGENTIC_SOURCE_ROOTS = frozenset({
        "api", "app", "assets", "backend", "components", "data", "frontend", "lib",
        "pages", "public", "server", "src", "state", "styles", "test", "tests",
        "utils",
    })
    _AGENTIC_ALWAYS_ALLOWED_ROOTS = frozenset({"assets", "data", "public", "test", "tests"})

    # Static decoration the model routinely PLANS and then does not write.
    # Content here is deliberately neutral and unbranded — the point is that the
    # reference resolves, not that the placeholder is good art.
    _TRIVIAL_ASSET_BODIES: dict[str, str] = {
        ".svg": (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
            'role="img" aria-label="App icon">\n'
            '  <rect width="64" height="64" rx="12" fill="#1f2933"/>\n'
            '  <circle cx="32" cy="32" r="14" fill="#e6e8eb"/>\n'
            "</svg>\n"
        ),
        "robots.txt": "User-agent: *\nAllow: /\n",
        ".nojekyll": "",
        ".gitkeep": "",
    }

    @classmethod
    def _synthesize_trivial_assets(cls, worktree: Path, missing: list[str]) -> list[str]:
        """Create missing planned files that are static decoration, not logic.

        A planned-but-unwritten file makes the delivery "under-delivered" and
        buys another agentic round-trip. That is the right trade for a missing
        module and a bad one for a favicon. Measured on a real build: both
        best-of-N trajectories planned ``public/favicon.svg``, neither wrote it,
        and each spent ~60s on a resume that did not produce it either — so the
        round-trip was pure cost with no chance of success.

        Only extensions/names whose content carries no app behaviour are
        synthesized; anything else stays missing so the resume still happens.
        Confined to the worktree, and never through a symlinked parent.
        """
        root = Path(worktree).resolve()
        made: list[str] = []
        for rel in missing:
            parts = PurePosixPath(rel).parts
            if not parts or ".." in parts:
                continue
            name = parts[-1]
            body = cls._TRIVIAL_ASSET_BODIES.get(name)
            if body is None:
                body = cls._TRIVIAL_ASSET_BODIES.get(PurePosixPath(rel).suffix)
            if body is None:
                continue
            target = root.joinpath(*parts)
            try:
                if not target.resolve().is_relative_to(root):
                    continue
            except (OSError, ValueError):
                continue
            current = root
            unsafe = False
            for part in parts:
                current = current / part
                if current.is_symlink():
                    unsafe = True
                    break
            if unsafe or target.exists():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
            except OSError:
                continue
            made.append(rel)
        return made

    @staticmethod
    def _present_planned_files(worktree: Path, expected: set[str]) -> set[str]:
        """Return confined, regular planned files present on disk.

        This inventory is separate from ``_read_files``: planned images, fonts,
        SVGs, assets, and large artifacts count as delivered without ever being
        decoded and rewritten as text. Symlinks (including symlinked parents) do
        not satisfy completeness.
        """
        root = Path(worktree).resolve()
        present: set[str] = set()
        for rel in expected:
            parts = PurePosixPath(rel).parts
            candidate = root.joinpath(*parts)
            current = root
            unsafe = False
            for part in parts:
                current = current / part
                if current.is_symlink():
                    unsafe = True
                    break
            if unsafe:
                continue
            try:
                resolved = candidate.resolve()
                if os.path.commonpath([str(root), str(resolved)]) != str(root):
                    continue
                if candidate.is_file():
                    present.add(rel)
            except (OSError, ValueError):
                continue
        return present

    def _snapshot_preexisting_assets(self, worktree: Path) -> dict[str, bytes]:
        """Snapshot upstream assets so codegen cannot corrupt or delete them."""
        root = Path(worktree).resolve()
        assets: dict[str, bytes] = {}
        for path in root.rglob("*"):
            try:
                rel_path = path.relative_to(root)
            except ValueError:
                continue
            if ".git" in rel_path.parts or path.is_symlink() or not path.is_file():
                continue
            rel = rel_path.as_posix()
            is_asset = "assets" in rel_path.parts or path.suffix.lower() in self._BINARY_EXTS
            if not is_asset:
                continue
            try:
                resolved = path.resolve()
                if os.path.commonpath([str(root), str(resolved)]) != str(root):
                    continue
                assets[rel] = path.read_bytes()
            except (OSError, ValueError):
                continue
        return assets

    @staticmethod
    def _restore_preexisting_assets(worktree: Path, assets: dict[str, bytes]) -> None:
        """Restore the exact bytes authored by upstream asset-foundry stages."""
        root = Path(worktree).resolve()
        for rel, content in assets.items():
            target = root.joinpath(*PurePosixPath(rel).parts)
            try:
                if target.is_symlink():
                    target.unlink()
                resolved = target.resolve()
                if os.path.commonpath([str(root), str(resolved)]) != str(root):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.is_file() or target.read_bytes() != content:
                    target.write_bytes(content)
            except (OSError, ValueError):
                continue

    @staticmethod
    def _snapshot_regular_files(worktree: Path) -> dict[str, bytes]:
        """Snapshot preexisting slice files so ownership cleanup can restore them."""
        root = Path(worktree)
        snapshot: dict[str, bytes] = {}
        for path in root.rglob("*"):
            try:
                rel_path = path.relative_to(root)
                if ".git" in rel_path.parts or path.is_symlink() or not path.is_file():
                    continue
                snapshot[rel_path.as_posix()] = path.read_bytes()
            except (OSError, ValueError):
                continue
        return snapshot

    @classmethod
    def _agentic_new_path_allowed(
        cls,
        rel: str,
        expected: set[str],
    ) -> bool:
        """Allow normal direct-agent additions without admitting terminal junk.

        A whole-app CLI session is not constrained by the architect's file list,
        so it may legitimately add a component or test.  When the plan already
        establishes a source root (for example ``src/``), though, a new parallel
        root such as ``components/`` or ``state/`` is almost always a malformed
        terminal fragment.  Keep those writes out while preserving ordinary
        extensions beneath the established source layout.
        """
        canonical = canonical_project_relpath(rel)
        if canonical != rel:
            return False
        if canonical in expected or canonical in cls._AGENTIC_NEW_ROOT_FILES:
            return True
        path = PurePosixPath(canonical)
        # A plan-free agentic build has no architecture from which we can infer
        # whether a root-level entrypoint (``App.jsx``, ``main.py``) is junk.
        # Preserve ordinary extension-bearing files in that mode; structured
        # plans still receive the stricter source-root policy below.
        if not expected:
            return bool(path.suffix)
        if len(path.parts) < 2 or not path.suffix:
            return False
        root = path.parts[0]
        if root not in cls._AGENTIC_SOURCE_ROOTS:
            return False
        planned_roots = {
            PurePosixPath(item).parts[0]
            for item in expected
            if len(PurePosixPath(item).parts) > 1
        }
        planned_source_roots = planned_roots & cls._AGENTIC_SOURCE_ROOTS
        return (
            root in cls._AGENTIC_ALWAYS_ALLOWED_ROOTS
            or not planned_source_roots
            or root in planned_source_roots
        )

    @classmethod
    def _prune_untrusted_agentic_new_paths(
        cls,
        worktree: Path,
        baseline: dict[str, bytes],
        expected: set[str],
    ) -> list[str]:
        """Drop malformed direct-CLI additions while preserving known source."""
        root = worktree.resolve()
        normalized_expected = {
            canonical
            for item in expected
            if (canonical := canonical_project_relpath(item)) is not None
        }
        removed: list[str] = []
        for path in sorted(root.rglob("*"), key=lambda value: len(value.parts), reverse=True):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if cls._SKIP_PARTS & set(relative.parts):
                continue
            rel = relative.as_posix()
            try:
                if path.is_symlink():
                    if rel not in baseline:
                        path.unlink()
                        removed.append(rel)
                elif path.is_file() and rel not in baseline:
                    if not cls._agentic_new_path_allowed(rel, normalized_expected):
                        path.unlink()
                        removed.append(rel)
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                continue
        return sorted(removed)

    @staticmethod
    def _prune_slice_out_of_scope(
        worktree: Path,
        owned: set[str],
        baseline: dict[str, bytes],
    ) -> list[str]:
        """Remove or restore writes outside a specialist's normalized ownership set."""
        root = Path(worktree)
        removed: list[str] = []
        paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
        for path in paths:
            try:
                rel_path = path.relative_to(root)
            except ValueError:
                continue
            if ".git" in rel_path.parts:
                continue
            rel = rel_path.as_posix()
            try:
                if path.is_symlink():
                    # An owned-name symlink can point outside the worktree and
                    # must never satisfy specialist completeness.
                    path.unlink()
                    removed.append(rel)
                elif path.is_file():
                    if rel not in owned:
                        if rel in baseline:
                            if path.read_bytes() != baseline[rel]:
                                path.write_bytes(baseline[rel])
                                removed.append(rel)
                        else:
                            path.unlink()
                            removed.append(rel)
                elif path.is_dir():
                    path.rmdir()  # clean empty directories left by removed files
            except OSError:
                continue
        for rel, content in baseline.items():
            if rel in owned:
                continue
            target = root.joinpath(*PurePosixPath(rel).parts)
            if target.exists() or target.is_symlink():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                removed.append(rel)
            except OSError:
                continue
        return sorted(removed)

    def _read_files(self, worktree: Path) -> dict[str, str]:
        """Read every text file the agent wrote into the worktree."""
        root = Path(worktree)
        out: dict[str, str] = {}
        for p in root.rglob("*"):
            if not p.is_file() or p.name == ".DS_Store":
                continue
            if any(part in self._SKIP_PARTS for part in p.relative_to(root).parts):
                continue
            if p.suffix.lower() in self._BINARY_EXTS:
                continue  # never round-trip binary assets through text
            try:
                if p.stat().st_size > 500_000:
                    continue  # skip oversized/binary artifacts
                out[p.relative_to(root).as_posix()] = p.read_text(
                    encoding="utf-8", errors="ignore"
                )
            except OSError:
                continue
        return out

    @staticmethod
    def _html_entrypoint_intact(html: str, files: dict[str, str]) -> bool:
        """True if ``index.html`` loads a JS module entrypoint that actually exists
        among the delivered ``files``.

        The clobber bug ships an index.html whose only ``<script>`` is an inline toy
        page (or a module ``src`` pointing at a file that was never written), so the
        real app never loads. A valid ``<script type="module" src="...">`` whose
        target is on disk means the entrypoint is wired."""
        for m in re.finditer(
            r'<script\b[^>]*\btype=["\']module["\'][^>]*\bsrc=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        ):
            src = m.group(1).split("?")[0].lstrip("/")
            if src.startswith("./"):
                src = src[2:]
            if any(k == src or k.endswith("/" + src) for k in files):
                return True
        return False

    @staticmethod
    def _phaser_entrypoint_intact(html: str, files: dict[str, str]) -> bool:
        """True when ``index.html`` loads the Phaser boot module, not merely any
        existing JS/JSX file. A stale React shell can have a valid
        ``/src/main.jsx`` module while the real game lives dead in
        ``/src/main.js``."""
        for m in re.finditer(
            r'<script\b[^>]*\btype=["\']module["\'][^>]*\bsrc=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        ):
            src = m.group(1).split("?")[0].lstrip("/")
            if src.startswith("./"):
                src = src[2:]
            entry = files.get(src)
            if entry is not None and "new Phaser.Game" in entry:
                return True
        return False

    def _repair_html_entrypoint(
        self, worktree: Path, files: dict[str, str], stack: str, scaffold: dict[str, str]
    ) -> dict[str, str]:
        """Guarantee an HTML build's ``index.html`` loads its real JS entrypoint.

        A weak codegen model sometimes replaces the scaffold's correct index.html
        with a standalone inline page that never imports the app — the "real game
        never loads, enemies stay primitive rectangles" failure. When the delivered
        index.html has no module script pointing at a delivered file, restore the
        scaffold's known-good index.html (which loads the canonical entrypoint). The
        repair is deliberately conservative: it only fires when the delivered file is
        broken AND the scaffold's index.html points at a file that WAS delivered, so
        a legitimately renamed entrypoint is never clobbered."""
        html = files.get("index.html")
        scaffold_html = scaffold.get("index.html")
        if not html or not scaffold_html:
            return files  # not an HTML stack / nothing to restore
        intact = (
            self._phaser_entrypoint_intact
            if (stack or "").lower() == "phaser"
            else self._html_entrypoint_intact
        )
        if intact(html, files):
            return files  # delivered index.html already loads a real entrypoint
        if not intact(scaffold_html, files):
            return files  # scaffold entrypoint absent (renamed) -> can't safely repair
        (Path(worktree) / "index.html").write_text(scaffold_html, encoding="utf-8")
        files = dict(files)
        files["index.html"] = scaffold_html
        self._task_metadata["index_html_repaired"] = True
        log.warning("code_agent.index_html_entrypoint_repaired", stack=stack)
        return files

    # ------------------------------------------------------------------
    # Scene-graph (entry-point) repair net — the orphaned-scene clobber.
    #
    # Symptom: codegen splits the game into real Phaser Scene modules
    # (BootScene.js, GameScene.js, …) that each `export default` a Scene
    # subclass and wire to each other, but src/main.js never imports them and
    # instead defines its OWN throwaway inline scene that it runs via
    # `new Phaser.Game({ scene: [<inline>] })`. The polished game is orphaned
    # dead code; the player sees "just ships" / placeholders. Even a top model
    # makes this error, so we repair it deterministically (mirrors the
    # index.html entrypoint repair above).
    # ------------------------------------------------------------------

    _SCENE_EXPORT_RE = re.compile(
        r"export\s+default\s+class\s+\w+\s+extends\s+(?:Phaser\.)?Scene\b"
    )
    _INLINE_SCENE_RE = re.compile(
        r"\bclass\s+\w+\s+extends\s+(?:Phaser\.)?Scene\b"
    )

    @staticmethod
    def _scene_modules(files: dict[str, str]) -> list[tuple[str, str]]:
        """Return ``(path, import_ident)`` for every ``src/**/*.js`` module other than
        main.js that default-exports a Phaser ``Scene`` subclass.

        ``import_ident`` is a UNIQUE, safe JS identifier derived from the filename,
        used for a default import (the module's real class name is irrelevant).
        Idents are de-duplicated so two filenames that sanitize to the same token
        (e.g. ``game-over.js`` and ``gameover.js``) cannot emit duplicate imports."""
        out: list[tuple[str, str]] = []
        seen: dict[str, int] = {}
        for path, text in files.items():
            if not path.endswith(".js") or not text:
                continue
            base = path.rsplit("/", 1)[-1]
            if base == "main.js":
                continue
            if not CodeAgent._SCENE_EXPORT_RE.search(text):
                continue
            stem = base[:-3]
            ident = re.sub(r"\W", "", stem) or "Scene"
            if ident[0].isdigit():
                ident = "Scene" + ident
            if ident in seen:
                seen[ident] += 1
                ident = f"{ident}_{seen[ident]}"
            else:
                seen[ident] = 0
            out.append((path, ident))
        return out

    @staticmethod
    def _order_scenes(modules: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Order scene modules so Phaser auto-starts the right first scene:
        boot/preload -> title/menu/start -> game/play -> gameover/win/lose/end."""
        def rank(item: tuple[str, str]) -> int:
            name = item[1].lower()
            if any(k in name for k in ("boot", "preload", "load")):
                return 0
            if any(k in name for k in ("title", "menu", "start", "home", "splash")):
                return 1
            # check end-states BEFORE "game" so GameOverScene sorts last, not as a play scene
            if any(k in name for k in ("gameover", "over", "win", "lose", "end", "result", "credit")):
                return 4
            if any(k in name for k in ("game", "play", "main", "level", "world", "battle")):
                return 2
            return 3
        return sorted(modules, key=lambda m: (rank(m), m[0]))

    @staticmethod
    def _scene_registry_intact(files: dict[str, str]) -> bool:
        """True unless the orphaned-scene clobber is present.

        The clobber: real ``export default`` Scene modules exist, but src/main.js
        imports NONE of them and instead defines its own inline Scene class(es). In
        that case the authored game never runs. Conservative by design — if main.js
        imports even one real scene module, or defines no inline scenes of its own,
        we leave it alone (no false repair)."""
        main = files.get("src/main.js")
        if not main:
            return True  # no Vite entry to wire (single-file / other layout)
        modules = CodeAgent._scene_modules(files)
        if not modules:
            return True  # single-scene game (or scenes inline in main) — nothing to wire
        for path, _ident in modules:
            stem = path.rsplit("/", 1)[-1][:-3]
            if re.search(
                r"""from\s+["'][^"']*\b""" + re.escape(stem) + r"""(?:\.js)?["']""",
                main,
            ):
                return True  # main imports at least one real scene module -> wired
        # main imports none of the real scenes. Only treat as the clobber when main
        # is actually running its OWN inline scene(s) (the shadowing signature).
        if CodeAgent._INLINE_SCENE_RE.search(main):
            return False
        return True

    @staticmethod
    def _extract_brace_object(text: str, head_pattern: str) -> str | None:
        """Return the ``{...}`` object literal (inclusive) that IMMEDIATELY follows the
        first match of ``head_pattern`` (e.g. ``const config =``), brace-matched so
        nested objects are preserved. ``None`` if the head is not directly followed by
        an object literal (so ``new Phaser.Game(someVar)`` won't grab a stray brace)."""
        m = re.search(head_pattern, text)
        if not m:
            return None
        i = m.end()
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] != "{":
            return None  # head isn't directly followed by an object literal
        depth = 0
        for j in range(i, len(text)):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[i : j + 1]
        return None

    @classmethod
    def _compose_scene_main(cls, main: str, ordered: list[tuple[str, str]]) -> str | None:
        """Rebuild src/main.js so it imports + registers the real scene modules.

        Preserves the original non-scene imports and the ``const config = {...}``
        object literal (swapping only its ``scene: [...]`` array), injects ordered
        scene-module imports, and drops the inline scene classes (rebuilding the file
        naturally omits them). Returns ``None`` if it cannot compose safely."""
        if not ordered:
            return None
        stems = {path.rsplit("/", 1)[-1][:-3] for path, _ in ordered}
        # Capture FULL import statements (incl. multi-line destructured imports and
        # ASI/no-semicolon forms) so we never truncate one into invalid JS.
        raw_imports = re.findall(
            r"import\b[^;'\"]*?from[ \t\r\n]*['\"][^'\"]+['\"][ \t]*;?"
            r"|import[ \t]*['\"][^'\"]+['\"][ \t]*;?",
            main,
        )
        has_phaser = False
        kept: list[str] = []
        for stmt in raw_imports:
            if re.search(r"""['\"]phaser['\"]""", stmt):
                has_phaser = True
                continue  # re-added canonically below
            # drop any import that already references a scene module we re-add
            if any(
                re.search(r"""['\"][^'\"]*\b""" + re.escape(s) + r"""(?:\.js)?['\"]""", stmt)
                for s in stems
            ):
                continue
            kept.append(stmt.strip())

        header: list[str] = ["import Phaser from 'phaser';"]
        header.extend(kept)
        for path, ident in ordered:
            # Import specifier relative to src/main.js, so scenes in src/scenes/ etc.
            # resolve correctly (NOT flattened to the basename).
            if path.startswith("src/"):
                spec = "./" + path[len("src/"):]
            else:
                spec = "../" + path
            header.append(f"import {ident} from '{spec}';")
        scene_arr = "[" + ", ".join(ident for _, ident in ordered) + "]"

        inner = cls._extract_brace_object(main, r"\bconst\s+config\s*=\s*")
        if inner is None:
            # Some models inline the config object directly in the Game() call:
            # `new Phaser.Game({ ... })`. Preserve those settings too.
            inner = cls._extract_brace_object(main, r"new\s+Phaser\.Game\s*\(\s*")
        if inner:
            new_inner, n = re.subn(
                r"scene\s*:\s*\[[^\]]*\]", f"scene: {scene_arr}", inner
            )
            if n == 0:  # config had no scene key — inject one before the closing brace
                new_inner = inner.rstrip()[:-1].rstrip()
                if not new_inner.endswith(",") and not new_inner.endswith("{"):
                    new_inner += ","
                new_inner += f"\n  scene: {scene_arr},\n}}"
            config_block = "const config = " + new_inner + ";"
        else:
            config_block = (
                "const config = {\n"
                "  type: Phaser.AUTO,\n"
                "  parent: 'game-container',\n"
                "  width: 800,\n"
                "  height: 600,\n"
                "  backgroundColor: '#000000',\n"
                "  scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH },\n"
                "  physics: { default: 'arcade', arcade: { debug: false } },\n"
                f"  scene: {scene_arr},\n"
                "};"
            )
        _ = has_phaser  # phaser import is always emitted canonically
        result = "\n".join(header) + "\n\n" + config_block + "\n\nnew Phaser.Game(config);\n"
        # Degrade-safe backstop: never SHIP a non-running entry. If our rebuild is
        # unbalanced (e.g. a dropped/odd import or config), bail and let the build's
        # own fix loop handle it rather than overwriting with broken JS.
        if (
            result.count("{") != result.count("}")
            or result.count("(") != result.count(")")
            or result.count("[") != result.count("]")
        ):
            return None
        return result

    def _repair_scene_registry(
        self, worktree: Path, files: dict[str, str], stack: str
    ) -> dict[str, str]:
        """Guarantee src/main.js imports + registers the real Scene modules.

        When codegen splits the game into ``export default`` Scene modules but main.js
        ignores them and runs its own inline scene, the authored game is orphaned dead
        code. Rebuild main.js to import and register the real scenes (boot first).
        Gated to the phaser stack and to the exact clobber signature, so a correctly
        wired single- or multi-scene game is never touched."""
        if stack != "phaser":
            return files
        if self._scene_registry_intact(files):
            return files
        main = files.get("src/main.js")
        if not main:
            return files
        ordered = self._order_scenes(self._scene_modules(files))
        new_main = self._compose_scene_main(main, ordered)
        if not new_main:
            return files  # could not compose safely — leave as-is for the fix loop
        (Path(worktree) / "src" / "main.js").write_text(new_main, encoding="utf-8")
        files = dict(files)
        files["src/main.js"] = new_main
        self._task_metadata["scene_registry_repaired"] = True
        log.warning(
            "code_agent.scene_registry_repaired",
            stack=stack,
            scenes=[ident for _, ident in ordered],
        )
        return files


    async def _capture_vents(self, res: dict[str, Any], slug: str, attempt: int) -> None:
        """Harvest ``VENT:`` lines from one agentic session's output text.

        Best-effort by design — the vent channel must never fail a build.
        Bounded (first 3 across the whole build, ≤300 chars each) and recorded
        three ways: ``run_metadata["vents"]`` (surfaces with the build record),
        one CODEGEN_VENT event per vent (dashboard/incident signal), and an
        append to ``logs/<slug>/vents.jsonl`` (the operator trail the learning
        capture reads back at build end)."""
        try:
            text = res.get("output_text") if isinstance(res, dict) else ""
            vents = parse_vents(str(text or ""))
            if not vents:
                return
            run_metadata = self._task_metadata
            captured = run_metadata.setdefault("vents", [])
            new_records: list[dict[str, Any]] = []
            for vent in vents:
                if len(captured) >= _VENT_MAX:
                    break
                captured.append(vent)
                new_records.append({"slug": slug, "vent": vent, "attempt": attempt})
                try:
                    await self.event_bus.emit(
                        EventType.CODEGEN_VENT, self.name,
                        {"slug": slug, "vent": vent, "attempt": attempt},
                    )
                except Exception:  # noqa: BLE001 - a bus failure must not stop capture
                    pass
            if new_records:
                _append_vents_log(slug, new_records)
                log.info("code_agent.vents_captured", slug=slug,
                         attempt=attempt, count=len(new_records))
        except Exception as exc:  # noqa: BLE001 - the vent channel never fails a build
            log.warning("code_agent.vent_capture_failed", error=str(exc)[:160])

    @staticmethod
    def _clean_agentic_files(
        disk: dict[str, str], scaffold: dict[str, str]
    ) -> tuple[dict[str, str], list[str]]:
        """Drop source files the agent wrote that are PROSE or ELIDED, not code.

        Returns ``(clean_files, rejected_paths)``. A rejected file is reverted to
        its scaffold version when one exists (a runnable baseline), else dropped
        entirely — so chat prose / stub placeholders never ship as source.
        Non-code files are kept.

        DELIBERATELY LENIENT on syntax: a coding agent authors real files that the
        authoritative gate (proof_run's real `npm build` / pytest, then the
        fix-loop) validates downstream. The only hard rejects here are (a) a CLEAR
        prose file — the agent chatted instead of coding — (b) a Python file
        that won't even compile, and (c) an ELIDED file — the agent wrote an
        edit-style "/* ... unchanged ... */" / "rest of the file is unchanged"
        placeholder as if patching a pre-existing original, leaving a
        non-functional stub (high-precision idioms only). We do NOT run the cheap
        JS/TS brace-balance heuristic: it false-positives on valid code (regex
        literals, nested template literals) and silently reverting the agent's app
        to the offline scaffold stub guarantees a no_go on a build that would
        otherwise compile.
        """
        from skyn3t.agents.validate import _CODE_EXTS, _looks_like_prose, looks_elided

        clean: dict[str, str] = {}
        rejected: list[str] = []
        for path, content in disk.items():
            p = path.lower()
            is_code = p.endswith(_CODE_EXTS)
            # A vent is a pipeline signal, never source: strip VENT: lines from
            # everything about to ship (code AND prose-check below see the
            # scrubbed text) so the channel can't leak into a written file.
            content = strip_vent_lines(content)
            # (a) the agent chatted instead of coding, or (b) it elided the file
            # body with an edit-style "/* ... unchanged ... */" placeholder (a
            # non-functional stub — there is no original to be unchanged from).
            drop = is_code and (_looks_like_prose(content) or looks_elided(content))
            if not drop and p.endswith(".py"):
                try:
                    compile(content, path, "exec")
                except SyntaxError:
                    drop = True
            if drop:
                rejected.append(path)
                if path in scaffold:
                    clean[path] = scaffold[path]
            else:
                clean[path] = content
        return clean, rejected

    @staticmethod
    def _unlink_rejected_agentic_files(
        worktree: Path, rejected: list[str], scaffold: dict[str, str]
    ) -> None:
        """Remove rejected agent-authored files that are absent from the scaffold."""
        root = Path(worktree).resolve()
        for rel in rejected:
            if rel in scaffold:
                continue
            try:
                target = (root / rel).resolve()
            except OSError:
                continue
            if target == root or root not in target.parents:
                continue
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("code_agent.agentic_rejected_unlink_failed", file=rel)

    async def _generate_file(self, rel_path: str, brief: str, stack: str,
                             plan: dict[str, Any], knowledge: str = "",
                             model_override: str | None = None,
                             manifest: str = "",
                             design: dict[str, Any] | None = None,
                             design_tokens_md: str | None = None) -> str | None:
        ext = Path(rel_path).suffix.lower()
        base = Path(rel_path).name.lower()
        is_readme = base in _README_NAMES
        is_manifest = base in _MANIFEST_NAMES
        if is_readme:
            tier = Tier.DOCS
        elif ext in _FRONTEND_EXTENSIONS:
            tier = Tier.UI
        else:
            tier = Tier.BACKEND
        # Give the model the file's assigned purpose + the full project file
        # list so each independently-generated file is substantial AND coheres
        # (correct imports/wiring across siblings).
        files = plan.get("files") or []
        purpose = ""
        manifest_lines = []
        for f in files:
            if isinstance(f, dict) and f.get("path"):
                manifest_lines.append(f"  {f['path']} — {f.get('purpose', '')}")
                if f["path"] == rel_path:
                    purpose = str(f.get("purpose", ""))
        # Prefer the full cross-slice manifest (so a sliced file knows the sibling
        # paths it must import from); fall back to the (slice-local) plan files.
        file_list = manifest.strip() or "\n".join(manifest_lines) or "(see scaffold)"
        design_summary = self._design_summary(design)
        design_block = ""
        if (
            ext in _FRONTEND_EXTENSIONS
            and (stack or "").lower() in _DESIGN_WEB_STACKS
        ):
            design_block = f"\n{_DESIGN_DIRECTIVE}\n{design_tokens_md or design_md_block(brief)}"
            if design_summary:
                design_block += f"\nFollow this design direction: {design_summary}"
        prompt = (
            f"{knowledge}"
            f"Project brief: {brief}\n"
            f"Stack: {stack}\n"
            f"Architecture summary: {plan.get('summary', '')}\n"
            f"All files in this project:\n{file_list}\n\n"
            f"File to write: {rel_path}\n"
            f"This file's purpose: {purpose or 'implement the part of the brief this path implies'}\n\n"
            "Write the COMPLETE, production-quality implementation of THIS file. "
            "Fully implement every behavior it owns, with real logic and error "
            "handling. Import from the other project files above where appropriate "
            "so the codebase coheres. No placeholders, no TODOs, no stub functions. "
            "This is a brand-new file with no pre-existing version — write it IN FULL "
            "and NEVER elide code with an edit-style placeholder like "
            "'/* ... unchanged ... */' or '// rest of the file is unchanged'."
            + design_block
            + (f"\n{_README_INSTR}" if is_readme else "")
            + (f"\n{_MANIFEST_INSTR}" if is_manifest else "")
        )
        result = await self.llm.complete(
            prompt, tier=tier, system=self.system_prompt(_SYSTEM), file_hint=rel_path,
            max_tokens=None,  # codegen must not truncate complete files at a fixed output ceiling
            task_type=self.agent_type, model_override=model_override,
        )
        # If the call degraded to the stub backend (CLI failure/timeout, missing
        # key), do NOT write stub prose over the runnable scaffold — keep it.
        if result.backend == "stub":
            return None
        from skyn3t.agents.validate import validate_source
        code = extract_code(result.text)
        ok, err = validate_source(rel_path, code)
        if not ok:
            retry = await self.llm.complete(
                prompt + f"\n\nThe previous attempt had an error: {err}\n"
                "Return the COMPLETE corrected file.",
                tier=tier, system=self.system_prompt(_SYSTEM), file_hint=rel_path,
                max_tokens=None,
                task_type=self.agent_type, model_override=model_override,
            )
            if retry.backend != "stub":
                recode = extract_code(retry.text)
                ok2, _ = validate_source(rel_path, recode)
                if ok2:
                    return recode
            # Both attempts produced invalid source. Return None so the caller
            # keeps the runnable scaffold instead of writing broken code over it
            # (the original `code` already failed validation — it's not "work").
            return None
        return code

    # import './x.css'  OR  import styles from '../x.scss'  (LOCAL paths only)
    _CSS_IMPORT_RE = re.compile(
        r"""import\s+(?:[\w*{},\s]+\s+from\s+)?['"](\.[^'"]*\.(?:css|scss|sass|less))['"]"""
    )

    @classmethod
    def _stub_missing_css_imports(cls, files: dict[str, str]) -> dict[str, str]:
        """Create an empty stub for any LOCAL stylesheet that is imported but not
        delivered. A dangling ``import './index.css'`` (frequent when codegen is
        cut short) otherwise fails the whole app on an unresolved import; an empty
        stylesheet resolves it harmlessly. Only touches relative ('.'-prefixed)
        paths, so package imports (e.g. 'normalize.css') are left alone."""
        import posixpath
        additions: dict[str, str] = {}
        for path, content in list(files.items()):
            if path.rsplit(".", 1)[-1] not in ("tsx", "jsx", "ts", "js", "mjs", "cjs", "vue", "svelte"):
                continue
            base = posixpath.dirname(path)
            for m in cls._CSS_IMPORT_RE.finditer(content or ""):
                target = posixpath.normpath(posixpath.join(base, m.group(1)))
                if target not in files and target not in additions:
                    additions[target] = "/* stub stylesheet — import target was missing */\n"
        files.update(additions)
        return files

    @staticmethod
    def _clear_worktree(worktree: Path) -> None:
        """Remove the agent's writes (everything but the git pointer) so a
        scaffold fallback ships clean. Best-effort; never raises."""
        try:
            children = list(worktree.iterdir())
        except OSError:
            return
        for child in children:
            if child.name == ".git":
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
            except OSError:
                pass

    def _write_files(self, worktree: Path, files: dict[str, str]) -> list[str]:
        written: list[str] = []
        root = worktree.resolve()
        for raw_rel, content in files.items():
            # Generated file maps can contain terminal escape fragments when a
            # model accidentally treats colored build output as a path.  Reuse
            # the agentic writer's portable path guard so those fragments never
            # become junk files such as ``35massets/index.js`` on Windows.
            rel = canonical_project_relpath(raw_rel)
            if rel is None:
                continue
            target = (worktree / rel).resolve()
            # Confinement: never escape the worktree.
            if os.path.commonpath([str(root), str(target)]) != str(root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(rel)
        return sorted(written)

    # ---- entrypoint repair ----------------------------------------------
    @staticmethod
    def _has_manifest(files: dict[str, str]) -> bool:
        names = {f.replace("\\", "/").rsplit("/", 1)[-1] for f in files}
        return bool(names & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"})

    @staticmethod
    def _next_route_key(path: str) -> str | None:
        """The pathname a Next.js page-route file serves, or None if it is not a
        page route. app/page.* -> '/', app/foo/page.* -> '/foo', pages/index.* ->
        '/', pages/foo.* -> '/foo'. pages/api/* and pages/_app|_document are not
        page routes (they never collide on a UI pathname)."""
        if path.startswith("app/") and re.search(r"(^|/)page\.(jsx|tsx|js|ts)$", path):
            inner = re.sub(r"/?page\.(jsx|tsx|js|ts)$", "", path[len("app/"):])
            return "/" + inner
        if path.startswith("pages/"):
            name = path[len("pages/"):]
            if name.startswith("api/") or name.startswith("_"):
                return None
            m = re.match(r"(.+)\.(jsx|tsx|js|ts)$", name)
            if not m:
                return None
            base = m.group(1)
            return "/" if base == "index" else "/" + base
        return None

    def _normalize_nextjs_router(self, files: dict[str, str]) -> dict[str, str]:
        """Guarantee ONE Next.js router per route. The scaffold always seeds the
        App Router (app/page.jsx); if codegen also emitted a Pages Router file on
        the SAME pathname, `next build` fails with 'Conflicting app and page
        file'. Resolve each colliding pathname by KEEPING THE LARGER file (real
        content beats a scaffold stub) and dropping the other. Non-colliding
        routes across the two routers are valid Next.js and left untouched."""
        routes: dict[str, list[tuple[str, int]]] = {}
        for p, content in files.items():
            key = self._next_route_key(p)
            if key is None:
                continue
            routes.setdefault(key, []).append((p, len(content or "")))
        for entries in routes.values():
            if len(entries) < 2:
                continue
            entries.sort(key=lambda e: e[1], reverse=True)  # largest first
            for path, _ in entries[1:]:
                files.pop(path, None)
        return files

    def _repair_entrypoints(
        self, stack: str, files: dict[str, str], app_name: str = "app"
    ) -> dict[str, str]:
        """Fix the most common entrypoint/manifest gaps left by the codegen.

        The agentic backend reliably authors real package code but often forgets
        the runnable root + manifest the rest of the pipeline expects. For python
        stacks we synthesize a wired ``main.py`` and a real ``pyproject.toml`` so
        a package-only delivery is genuinely runnable (not a dangling import).
        """
        # A LOCAL stylesheet that is imported but never written (common when
        # codegen is cut short — e.g. main.tsx imports ./index.css) makes the
        # whole app an unresolved-import failure. Stub it so a near-complete app
        # isn't no_go'd over an empty file.
        files = self._stub_missing_css_imports(files)
        if stack in ("nextjs", "next"):
            files = self._normalize_nextjs_router(files)
        if stack in ("python_cli", "python"):
            entry = synthesize_python_entrypoint(files)
            if entry:
                files.update(entry)
            if not self._has_manifest(files):
                files["pyproject.toml"] = default_pyproject(app_name)
            return files
        if stack == "react_vite":
            main = files.get("src/main.jsx")
            app = files.get("src/App.jsx")
            if app is not None and "export default" not in app and "export {" not in app:
                # Ensure App has a default export.
                if re.search(r"function\s+App\b", app):
                    app = app + "\n\nexport default App\n"
                    files["src/App.jsx"] = app
            if main is not None:
                # Ensure main imports App as default from ./App.jsx.
                if "App" not in main:
                    main = "import App from './App.jsx'\n" + main
                main = main.replace("import { App }", "import App")
                files["src/main.jsx"] = main
            # index.html must reference the real entry module.
            html = files.get("index.html")
            if html is not None:
                # Already wired to a REAL entry — main.tsx, or any module script
                # whose src points to a generated file (e.g. a custom
                # /src/index.jsx)? Then leave it: the old repair clobbered a valid
                # src with /src/main.jsx (which may not exist), breaking the page.
                srcs = re.findall(r'<script\s+type="module"[^>]*\bsrc="([^"]+)"', html)
                already_wired = "main.tsx" in html or any(s.lstrip("/") in files for s in srcs)
                if not already_wired:
                    # Fill in an EMPTY (src-less) module script, else append one.
                    html, n = re.subn(
                        r'<script\s+type="module"\s*></script>',
                        '<script type="module" src="/src/main.jsx"></script>',
                        html,
                    )
                    if n == 0 and "/src/main.jsx" not in html:
                        html = html.replace(
                            "</body>",
                            '  <script type="module" src="/src/main.jsx"></script>\n  </body>',
                        )
                    files["index.html"] = html
        elif stack == "node_express":
            server = files.get("server.js")
            if server is not None and "module.exports" not in server:
                files["server.js"] = server + "\nmodule.exports = app;\n"
        return files

    async def health_check(self) -> bool:
        return self.llm is not None
