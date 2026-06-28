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
    "Avoid the unstyled create-react-app look."
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
    "Use ONLY plain .js. Do NOT use React, Next.js, Vue, JSX/TSX, or TypeScript, and "
    "do NOT create app/, pages/, components/, next.config, tsconfig, or any "
    "web-framework routes — those build a website, not this game. Everything renders "
    "to a single Phaser canvas."
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
                    self.llm.agentic_build(prompt, str(worktree), provider=_codegen_prov)
                    if _codegen_prov
                    else self.llm.agentic_build(prompt, str(worktree))
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
            + (f"{self._game_depth_directive(brief, game_design)}\n\n" if stack == "phaser" else "")
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
            return (
                "GAME ART — this is an open-ended game: render the entities THIS "
                "brief implies, using roles APPROPRIATE to the actual game (not a "
                "fixed list). RULE per entity: a character / creature / vehicle / "
                "collectible / themed object = a SPRITE; a geometric shape, "
                "projectile, platform, wall, HUD bar, or abstract element = a clean "
                "styled PRIMITIVE from the palette. Use this EXACT shared palette "
                f"(hex): {palette}. BASELINE sprites ARE generated at "
                f"/assets/sprites/<role>.png for: {sprite_list} — load those in "
                "preload() and render WITH a colored-primitive FALLBACK: `const v = "
                "this.textures.exists('<role>') ? this.add.sprite(x, y, '<role>') : "
                "this.add.rectangle(x, y, w, h, 0x"
                f"{plan.palette[1].lstrip('#')})`. For any other role this game "
                "needs, reuse a fitting baseline sprite or draw a styled primitive "
                f"from the palette; background ~{plan.palette[0]}. Art is a RENDER "
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
        return (
            f"GAME ART — genre '{plan.genre}'. Use this EXACT shared palette (hex): "
            f"{palette}. "
            f"SPRITE ROLES — {sprite_list}: in preload() load each from "
            "`/assets/sprites/<role>.png` via "
            "`this.load.image('<role>', '/assets/sprites/<role>.png')`, then in "
            "create() render each WITH a colored-primitive FALLBACK so a missing "
            "sprite never breaks the game: "
            f"`const v = this.textures.exists('{example}') ? this.add.sprite(x, y, "
            f"'{example}') : this.add.rectangle(x, y, w, h, 0x{hex_no_hash})`. "
            f"PRIMITIVE ROLES — {prim_list}: draw as clean styled shapes using the "
            "palette color shown (NO sprite file). The sprite files are generated "
            "into public/assets/sprites/ by the build — do NOT create them yourself, "
            "just reference them. Art is a RENDER concern in src/main.js ONLY; keep "
            "ALL game logic in the pure src/sim.js unchanged."
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
        n_pow = max(2, min(3, len(gd.powerups)))
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
            res = await self.llm.agentic_build(prompt, str(worktree), model=model_override)
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
    def _clean_agentic_files(
        disk: dict[str, str], scaffold: dict[str, str]
    ) -> tuple[dict[str, str], list[str]]:
        """Drop source files the agent wrote that are PROSE, not code.

        Returns ``(clean_files, rejected_paths)``. A rejected file is reverted to
        its scaffold version when one exists (a runnable baseline), else dropped
        entirely — so chat prose never ships as source. Non-code files are kept.

        DELIBERATELY LENIENT on syntax: a coding agent authors real files that the
        authoritative gate (proof_run's real `npm build` / pytest, then the
        fix-loop) validates downstream. The only hard rejects here are (a) a CLEAR
        prose file — the agent chatted instead of coding — and (b) a Python file
        that won't even compile. We do NOT run the cheap JS/TS brace-balance
        heuristic: it false-positives on valid code (regex literals, nested
        template literals) and silently reverting the agent's app to the offline
        scaffold stub guarantees a no_go on a build that would otherwise compile.
        """
        from skyn3t.agents.validate import _CODE_EXTS, _looks_like_prose

        clean: dict[str, str] = {}
        rejected: list[str] = []
        for path, content in disk.items():
            p = path.lower()
            drop = p.endswith(_CODE_EXTS) and _looks_like_prose(content)
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
            "so the codebase coheres. No placeholders, no TODOs, no stub functions."
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
