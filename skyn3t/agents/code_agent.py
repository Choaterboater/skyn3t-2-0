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
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import structlog

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, extract_code, knowledge_block, slugify
from skyn3t.agents._scaffold import (
    default_pyproject,
    scaffold_for,
    synthesize_python_entrypoint,
)
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

log = structlog.get_logger(__name__)

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
    "feature card). Pick a palette that fits THIS product instead."
)
# Config hygiene — keeps codegen from hardcoding API keys/endpoints. Reading
# config through env/an accessor lets the post-build config surfacer detect it and
# wire it to a generated Settings screen.
_CONFIG_DIRECTIVE = (
    "CONFIGURATION: never hardcode API keys, tokens, secrets or external endpoint "
    "URLs. Read them from configuration — `import.meta.env.VITE_*` / `process.env.*` "
    "for web/Node, `os.getenv(...)` for Python — with a sensible fallback. For a "
    "web UI that needs user-supplied values (e.g. an API key), expose them via a "
    "config module and a Settings screen the user can fill in, not literals in code."
)
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
    "react", "react_vite", "vite", "nextjs", "next", "astro", "remix",
    "static", "html", "node", "node_express", "express", "vue", "svelte",
    "phaser",  # a Phaser game has real HUD/menu visual-design concerns
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
    "`platforms: [.macOS(.v13)]`) declaring THREE targets — a `.target` library "
    "`AppCore` for the pure logic, an `.executableTarget` `App` (dependencies: "
    '["AppCore"]) for the UI, and a `.testTarget` `AppCoreTests` (dependencies: '
    '["AppCore"]). Sources live under `Sources/AppCore/*.swift`, '
    "`Sources/App/*.swift`, and tests under `Tests/AppCoreTests/*.swift`. "
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
    "asserts the core's real behavior. "
    "TARGET: macOS 13+; keep it compiling cleanly under the installed toolchain "
    "(avoid unavailable APIs). Implement the brief's real features, not a stub."
)


class CodeAgent(BaseAgent):
    # Max concurrent per-file generations (bounds nested claude -p instances).
    _gen_concurrency = 4

    def __init__(self, name: str = "code", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="codegen", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="codegen", description="Write runnable source files into the worktree",
            tags=("generative", "code")))
        self.llm = llm or LLMClient()

    async def initialize(self) -> None:
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        # Per-run reset: this agent is a long-lived singleton, so a `degraded`
        # flag set by a PRIOR build must not leak into this one (it would emit a
        # false no_go + halved score on a clean build). Clear before any work.
        self.metadata.pop("degraded", None)
        self.metadata.pop("degraded_reason", None)
        brief = p.get("brief", "") or p.get("slug", "app")
        # An art plan the runner computed + threaded (LLM-tailored or the floor); the
        # game-art directive uses it so codegen lists the SAME roles the sprite
        # generator produced. Absent for non-game builds / older payloads.
        _extra = p.get("extra")
        _art_plan = _extra.get("art_plan") if isinstance(_extra, dict) else None
        # The runner-threaded GDD (LLM-tailored or the deterministic floor); the
        # depth directive uses it so a retry keeps the SAME design the run committed.
        _game_design = _extra.get("game_design") if isinstance(_extra, dict) else None
        raw_plan = p.get("plan")
        plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
        stack = detect_stack(
            brief=brief, plan=plan,
            explicit=p.get("stack", "") or (plan.get("stack", "") if plan else ""),
        )
        app_name = slugify(p.get("slug") or brief, "app")

        worktree = self._resolve_worktree(p)

        # Hermes orchestrator-worker: a parallel SLICE writes ONLY its own files
        # (no whole-app scaffold floor / under-delivery revert — a slice is small
        # by design, and the merged tree's wiring is repaired by the post-merge
        # proof/fix-loop). Dispatched before the monolithic path below.
        slice_scope = p.get("slice_scope") if isinstance(p.get("slice_scope"), dict) else None
        if slice_scope:
            return await self._execute_slice(
                task, p, brief, stack, plan, app_name, worktree, slice_scope)

        # Decide what files to write. Prefer the architect's plan; otherwise the
        # canonical scaffold. The scaffold guarantees a runnable baseline. For game
        # stacks, make the scaffold art-aware (preload role sprites + primitive
        # fallback) when game_art is enabled — the sprites themselves are written by
        # the runner's role-sprite step (#6).
        from skyn3t.config.settings import get_settings as _gs_art

        art = stack == "phaser" and bool(getattr(_gs_art(), "game_art_enabled", True))
        scaffold = scaffold_for(stack, app_name, brief, art=art)
        files: dict[str, str] = dict(scaffold)

        knowledge = knowledge_block(p)
        # Codegen-only CLI routing: a configured `codegen_cli_provider` (e.g.
        # "claude") runs the agentic whole-app build on that CLI even when the
        # global backend is cheap (OpenRouter) — high-quality codegen without
        # paying for the CLI on every other stage.
        from skyn3t.config.settings import get_settings as _gs
        _codegen_prov = (getattr(_gs(), "codegen_cli_provider", "") or "").lower()
        _codegen_model = (getattr(_gs(), "codegen_cli_model", "") or None)
        _codegen_cli_ok = bool(_codegen_prov) and self.llm._cli_available(_codegen_prov)
        if self.llm.backend == "stub" and not _codegen_cli_ok:
            # Offline: deliver the runnable scaffold as-is.
            pass
        elif getattr(self.llm, "supports_agentic", False) or _codegen_cli_ok:
            # CLI backend is a coding AGENT: ONE agentic session authors the
            # whole coherent multi-file app — including its OWN entrypoint —
            # into a CLEAN worktree. (Pre-laying the scaffold left a stub main.py
            # masquerading as the entrypoint while the real code sat in a package.)
            # The scaffold is a fallback only if the agent under-delivers.
            from skyn3t.config.settings import get_settings

            # "Did the agent add a real app beyond the scaffold?" A delivery barely
            # above the scaffold's own code size is the placeholder leaking through,
            # so require a real margin over it (not a flat 800-byte floor).
            threshold = max(800, self._code_bytes(scaffold) * 2)
            max_retries = max(0, int(getattr(get_settings(), "agentic_retries", 1)))

            disk: dict[str, str] = {}
            code_bytes = 0
            agentic_ok = True
            agentic_error = ""
            prose_files: list[str] = []
            attempt = 0
            while True:
                prompt = (
                    self._agentic_prompt(
                        brief, stack, plan, knowledge,
                        art_plan=_art_plan, game_design=_game_design)
                    if attempt == 0
                    else self._agentic_retry_prompt(
                        brief, stack, plan, knowledge, code_bytes,
                        art_plan=_art_plan, game_design=_game_design)
                )
                res = await (
                    self.llm.agentic_build(prompt, str(worktree), provider=_codegen_prov,
                                           model=_codegen_model, stack=stack)
                    if _codegen_prov
                    else self.llm.agentic_build(prompt, str(worktree), stack=stack)
                )
                self.metadata["agentic"] = res
                agentic_ok = bool(res.get("ok", True))
                agentic_error = res.get("error", "")
                disk = self._read_files(worktree)
                # The CLI writes files directly (bypassing extract/validate). Guard
                # against it writing chat prose instead of code: reject prose source
                # files so they don't count as "delivered" and never ship.
                disk, prose_files = self._clean_agentic_files(disk, scaffold)
                code_bytes = self._code_bytes(disk)
                under_delivered = not (disk and code_bytes >= threshold)
                # Stop as soon as a real app is on disk — even if the call did NOT
                # exit cleanly (a TIMEOUT mid-build still produced real code; do not
                # throw it away to retry). Retry ONLY on genuine under-delivery.
                if not under_delivered or attempt >= max_retries:
                    break
                log.warning("code_agent.agentic_retry", attempt=attempt + 1,
                            code_bytes=code_bytes, threshold=threshold, ok=agentic_ok)
                attempt += 1

            if prose_files:
                # Some files were prose (not code) and were reverted to the
                # scaffold. This DEGRADES the build only if it left the app
                # under-delivered (checked next) — a substantial, building app with
                # one auto-reverted util still works and must NOT be no_go'd over it
                # (proof/build/liveness already verify the app actually runs).
                log.warning("code_agent.agentic_prose_rejected", files=prose_files)
                self.metadata["prose_rejected"] = list(prose_files)
            under_delivered = not (disk and code_bytes >= threshold)
            if under_delivered:
                # Genuine under-delivery: no-op'd / left a stub, or a prose-revert
                # dropped the real code below threshold.
                if not agentic_ok:
                    degraded_reason = (f"agentic build failed: {agentic_error}"
                                       if agentic_error else "agentic build returned ok=False")
                elif prose_files:
                    degraded_reason = (f"prose (not code) in {prose_files} left only "
                                       f"{code_bytes} code bytes (threshold {threshold})")
                else:
                    degraded_reason = (f"agentic build under-delivered after {attempt} retr"
                                       f"{'y' if attempt == 1 else 'ies'}: {code_bytes} code "
                                       f"bytes in {len(disk)} files (threshold {threshold})")
                log.warning(
                    "code_agent.agentic_degraded", agentic_ok=agentic_ok,
                    code_bytes=code_bytes, files_on_disk=len(disk),
                    retries=attempt, reason=degraded_reason,
                )
                self.metadata["degraded"] = True
                self.metadata["degraded_reason"] = degraded_reason
            elif not agentic_ok:
                # A SUBSTANTIAL app was delivered but the call didn't exit cleanly
                # — almost always a timeout mid-build. KEEP it (the verifier gates
                # judge whether the possibly-truncated app actually works); a real
                # app is far better than reverting to the scaffold stub.
                log.warning("code_agent.agentic_timeout_kept", code_bytes=code_bytes,
                            files_on_disk=len(disk), error=agentic_error or "(timeout)")
            if disk and code_bytes >= threshold:
                files = disk  # the agent's real app becomes the delivery
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
                # Under-delivered -> deliver a CLEAN scaffold. The agent wrote
                # stray files (rejected prose, a partial app, scratch notes) into
                # the worktree; _write_files only overwrites the scaffold keys and
                # would leave the strays alongside it, so wipe the agent's output
                # first.
                self._clear_worktree(worktree)
                self._write_files(worktree, files)  # under-delivered -> scaffold floor
        else:
            # Completion backend (OpenRouter): per-file, generated CONCURRENTLY
            # (bounded) so a multi-file app's wall-clock is the slowest file.
            planned = self._planned_paths(plan, scaffold)
            sem = asyncio.Semaphore(self._gen_concurrency)

            # Cross-model best-of-N pins this trajectory to a specific model.
            model_override = p.get("model_override")

            async def _one(rel_path: str) -> tuple[str, str | None]:
                async with sem:
                    try:
                        return rel_path, await self._generate_file(
                            rel_path, brief, stack, plan, knowledge,
                            model_override=model_override)
                    except Exception:  # noqa: BLE001 - keep scaffold fallback for this file
                        return rel_path, None

            for rel_path, content in await asyncio.gather(*(_one(p) for p in planned)):
                if content and content.strip():
                    files[rel_path] = content

        files = self._repair_entrypoints(stack, files, app_name)

        written = self._write_files(worktree, files)

        out: dict[str, Any] = {
            "files_written": len(written),
            "worktree_dir": str(worktree),
            "stack": stack,
            "files": written,
            "backend": self.llm.backend,
        }
        # Propagate degradation signal from the agentic path so downstream
        # scoring/verdict can see it. Never set on the stub or completion paths.
        if self.metadata.get("degraded"):
            out["degraded"] = True
            out["degraded_reason"] = self.metadata.get("degraded_reason", "unknown")

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

    def _agentic_prompt(self, brief: str, stack: str, plan: dict[str, Any], knowledge: str,
                        *, art_plan: dict[str, Any] | None = None,
                        game_design: dict[str, Any] | None = None) -> str:
        files = plan.get("files") or []
        manifest = "\n".join(
            f"  {f['path']} — {f.get('purpose', '')}"
            for f in files if isinstance(f, dict) and f.get("path")
        )
        return (
            f"{knowledge}"
            f"Build a COMPLETE, production-quality {stack} application for this brief:\n"
            f"{brief}\n\n"
            + (f"{_GAME_STACK_DIRECTIVE}\n\n" if stack == "phaser" else "")
            + (f"{_GAME_WIRING_DIRECTIVE}\n\n" if stack == "phaser" else "")
            + (f"{_GAME_FEEL_DIRECTIVE}\n\n" if stack == "phaser" else "")
            + (f"{self._game_depth_directive(brief, game_design)}\n\n" if stack == "phaser" else "")
            + (f"{_SWIFT_DIRECTIVE}\n\n" if stack == "swift" else "")
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
            + (f"{_DESIGN_DIRECTIVE}\n" if (stack or "").lower() in _WEB_STACKS else "")
            + (f"{self._game_art_directive(brief, art_plan)}\n" if self._game_art_on(stack) else "")
            + (f"{_DATA_DIRECTIVE}\n" if (stack or "").lower() in _DATA_STACKS else "")
            + (f"{_AGENT_APP_DIRECTIVE}\n" if _implies_agent_app(brief) else "")
            + f"{_FULL_FILE_CONTRACT}\n"
            + f"{_CONFIG_DIRECTIVE}\n"
            + f"{_LLM_DIRECTIVE}\n"
            + "Do not ask questions — just build it."
        )

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
    def _game_art_directive(brief: str, art_plan: dict[str, Any] | None = None) -> str:
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
            f"- MECHANICS (implement each, interacting): {', '.join(gd.mechanics)}.\n"
            f"- POWER-UPS / UPGRADES (at least {n_pow}, with REAL gameplay effects, "
            f"not just labels): {', '.join(gd.powerups)}.\n"
            f"- VARIETY (distinct types with different behavior): {', '.join(gd.variety)}.\n"
            f"- ECONOMY / SCORING: {gd.economy}.\n"
            "Put ALL of this in the pure src/sim.js state + step() (the Phaser scene "
            "only renders it), so it stays deterministic and testable."
        )

    # ---- parallel code slicing (Hermes orchestrator-worker) --------------
    async def _execute_slice(
        self, task: TaskRequest, p: dict[str, Any], brief: str, stack: str,
        plan: dict[str, Any], app_name: str, worktree: Path,
        slice_scope: dict[str, Any],
    ) -> TaskResult:
        """Generate ONLY this slice's files. The full file manifest is supplied as
        read-only context so cross-slice imports line up. On under-delivery the
        slice falls back to its own scaffold subset (a runnable floor) and flags
        degraded — mirroring the monolithic path so a vanished slice can't ship a
        silent hole."""
        name = str(slice_scope.get("name") or "slice")
        slice_files = [str(x) for x in (slice_scope.get("files") or [])]
        manifest = str(slice_scope.get("manifest") or "")
        model_override = p.get("model_override")
        knowledge = knowledge_block(p)
        files: dict[str, str] = {}
        degraded_reason = ""

        if self.llm.backend == "stub":
            # Offline: emit a minimal non-empty stub per slice file so the
            # orchestration is testable and the merge produces real files.
            files = self._slice_stub(stack, app_name, brief, slice_files)
        elif getattr(self.llm, "supports_agentic", False):
            raw_prior = p.get("prior")
            prior: dict[str, Any] = raw_prior if isinstance(raw_prior, dict) else {}
            raw_design = prior.get("design")
            design = raw_design if isinstance(raw_design, dict) else None
            prompt = self._agentic_slice_prompt(
                brief, stack, name, slice_files, manifest, knowledge, design=design)
            res = await self.llm.agentic_build(prompt, str(worktree), model=model_override,
                                               stack=stack)
            self.metadata["agentic"] = res
            disk = self._read_files(worktree)
            # Reject chat-prose source files (no scaffold to revert to -> dropped).
            disk, prose = self._clean_agentic_files(disk, {})
            if prose:
                self.metadata["prose_rejected"] = list(prose)
            if not disk:
                # The slice failed / timed out before writing / was all prose —
                # deliver its scaffold subset as a runnable floor and flag degraded.
                self._clear_worktree(worktree)
                files = self._slice_stub(stack, app_name, brief, slice_files)
                degraded_reason = f"agentic slice '{name}' delivered no files; fell back to scaffold"
            else:
                files = disk
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
                            model_override=model_override, manifest=manifest)
                    except Exception:  # noqa: BLE001 - isolate per file
                        return rel, None

            for rel, content in await asyncio.gather(*(_one(r) for r in slice_files)):
                if content and content.strip():
                    files[rel] = content
            if not files:
                files = self._slice_stub(stack, app_name, brief, slice_files)
                degraded_reason = f"completion slice '{name}' produced no files; fell back to scaffold"

        written = self._write_files(worktree, files)
        out: dict[str, Any] = {
            "files_written": len(written), "worktree_dir": str(worktree),
            "stack": stack, "files": written, "backend": self.llm.backend,
            "slice": name,
        }
        if degraded_reason:
            out["degraded"] = True
            out["degraded_reason"] = degraded_reason
        return TaskResult(task_id=task.task_id, success=True, output=out)

    def _agentic_slice_prompt(
        self, brief: str, stack: str, slice_name: str, slice_files: list[str],
        manifest: str, knowledge: str, design: dict[str, Any] | None = None,
    ) -> str:
        want = "\n".join(f"  {f}" for f in slice_files) or "  (none listed)"
        # The frontend slice owns the look — give it the design bar + the chosen
        # design tokens so it doesn't ship the generic emoji-template UI. Other
        # slices (config/tests/backend) stay lean.
        design_block = ""
        if slice_name == "frontend":
            design_block = f"\n\n{_DESIGN_DIRECTIVE}"
            summary = self._design_summary(design)
            if summary:
                design_block += f"\nFollow this design direction: {summary}"
        return (
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
        )

    @staticmethod
    def _design_summary(design: dict[str, Any] | None) -> str:
        """Condense the design stage's tokens (theme/palette/typography/layout) into
        a one-line direction for the codegen prompt. '' when none."""
        if not isinstance(design, dict):
            return ""
        bits: list[str] = []
        if design.get("theme"):
            bits.append(f"theme={design['theme']}")
        pal = design.get("palette")
        if isinstance(pal, dict) and pal:
            bits.append("palette(" + ", ".join(f"{k}:{v}" for k, v in pal.items()) + ")")
        for key in ("typography", "layout"):
            val = design.get(key)
            if val:
                bits.append(f"{key}={val}")
        return "; ".join(bits)[:400]

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
        return {rel: full.get(rel) or self._minimal_stub(rel) for rel in slice_files}

    _CODE_EXTS = ("py", "js", "jsx", "ts", "tsx", "go", "rs", "rb")

    @classmethod
    def _code_bytes(cls, files: dict[str, str]) -> int:
        """Total bytes across real code files (excludes config/docs/markup)."""
        return sum(
            len(c) for f, c in files.items()
            if f.rsplit(".", 1)[-1] in cls._CODE_EXTS
        )

    def _agentic_retry_prompt(
        self, brief: str, stack: str, plan: dict[str, Any], knowledge: str,
        code_bytes: int, *, art_plan: dict[str, Any] | None = None,
        game_design: dict[str, Any] | None = None,
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
                brief, stack, plan, knowledge, art_plan=art_plan, game_design=game_design)
        )

    # `assets` holds pre-generated binary images (Replicate). Skipping it keeps
    # those bytes off the text round-trip below — reading a PNG with errors=
    # "ignore" then re-writing it via _write_files would corrupt it.
    _SKIP_PARTS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".pytest_cache", "dist", ".next", "assets"})
    _BINARY_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                              ".bmp", ".svg", ".pdf", ".woff", ".woff2", ".ttf"})

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
                out[str(p.relative_to(root))] = p.read_text(encoding="utf-8", errors="ignore")
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
        if self._html_entrypoint_intact(html, files):
            return files  # delivered index.html already loads a real entrypoint
        if not self._html_entrypoint_intact(scaffold_html, files):
            return files  # scaffold entrypoint absent (renamed) -> can't safely repair
        (Path(worktree) / "index.html").write_text(scaffold_html, encoding="utf-8")
        files = dict(files)
        files["index.html"] = scaffold_html
        self.metadata["index_html_repaired"] = True
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
        self.metadata["scene_registry_repaired"] = True
        log.warning(
            "code_agent.scene_registry_repaired",
            stack=stack,
            scenes=[ident for _, ident in ordered],
        )
        return files


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

    async def _generate_file(self, rel_path: str, brief: str, stack: str,
                             plan: dict[str, Any], knowledge: str = "",
                             model_override: str | None = None,
                             manifest: str = "") -> str | None:
        ext = Path(rel_path).suffix.lower()
        base = Path(rel_path).name.lower()
        is_readme = base in _README_NAMES
        is_manifest = base in _MANIFEST_NAMES
        if is_readme:
            tier = Tier.DOCS
        elif ext in {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"}:
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
            + (f"\n{_README_INSTR}" if is_readme else "")
            + (f"\n{_MANIFEST_INSTR}" if is_manifest else "")
        )
        result = await self.llm.complete(
            prompt, tier=tier, system=self.system_prompt(_SYSTEM), file_hint=rel_path, max_tokens=16384,  # large data/page files truncated at 8192 -> mid-function EOF syntax error -> no_go
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
                tier=tier, system=self.system_prompt(_SYSTEM), file_hint=rel_path, max_tokens=16384,  # large data/page files truncated at 8192 -> mid-function EOF syntax error -> no_go
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
        for rel, content in files.items():
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
