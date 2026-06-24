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
import json
import os
import shutil
import signal
import tempfile
from dataclasses import dataclass, field

import httpx
import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.model_router import ModelRouter, Tier

# Per-asyncio-task LLM route capture. The LLMClient is SHARED across agents, but
# each agent's run() is its own task; task-local vars isolate "the completions
# THIS run produced" without a global lock (a per-AGENT lock can't — the client
# is shared, so two agents would race on shared instance attrs). The only reader
# (core/agent.py._run_locked) runs in the SAME task as the completions it reads.
_LAST_MODEL: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_last_model", default=None)
_LAST_ROUTE: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_last_route", default=None)
_ROUTES: contextvars.ContextVar = contextvars.ContextVar("skyn3t_llm_routes", default=None)

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
_CLI_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "-p"],
    "kimi": ["kimi", "-p"],
    "copilot": ["copilot", "-p"],
}
_KNOWN_CLI_PROVIDERS = ("claude", "kimi", "copilot")

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


@dataclass
class LLMResult:
    text: str
    model: str
    backend: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class BudgetTracker:
    """Hard backstop on spend. Raising stops a runaway loop."""
    per_build_cap: float
    daily_cap: float
    token_cap: int
    spent_build: float = 0.0
    spent_day: float = 0.0
    tokens_day: int = 0
    calls: list[LLMResult] = field(default_factory=list)

    def record(self, r: LLMResult) -> None:
        self.spent_build += r.cost_usd
        self.spent_day += r.cost_usd
        self.tokens_day += r.prompt_tokens + r.completion_tokens
        self.calls.append(r)

    def check(self) -> None:
        if self.spent_build > self.per_build_cap:
            raise BudgetExceeded(f"per-build cap ${self.per_build_cap} exceeded (${self.spent_build:.4f})")
        if self.spent_day > self.daily_cap:
            raise BudgetExceeded(f"daily cap ${self.daily_cap} exceeded (${self.spent_day:.4f})")
        if self.tokens_day > self.token_cap:
            raise BudgetExceeded(f"daily token cap {self.token_cap} exceeded ({self.tokens_day})")

    def reset_build(self) -> None:
        self.spent_build = 0.0


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
        )
        # Route capture is HYBRID: a task-local contextvar (so concurrent agent
        # runs sharing this client don't clobber each other) plus a global
        # last-write (so a SYNC / cross-context reader — e.g. `asyncio.run(
        # complete())` then read last_model — still sees the result). In-task
        # reads prefer the contextvar; out-of-task reads fall back to the globals.
        self._g_last_model: str | None = None
        self._g_last_route: tuple[str, str] | None = None
        self._g_routes: list[tuple[str, str, str]] = []

    # ---- per-run route capture (task-local + global fallback) ---------------
    def begin_run_capture(self) -> None:
        """Start an isolated, task-local route capture for THIS run so concurrent
        agent runs that share this client don't clobber each other. Agents call
        this at run start (replacing a bare ``routes.clear()``)."""
        _LAST_MODEL.set(None)
        _LAST_ROUTE.set(None)
        _ROUTES.set([])

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
            return "openrouter" if self.settings.openrouter_api_key else "stub"
        if pref.endswith("_cli"):
            prov = pref[:-4]
            return f"{prov}_cli" if self._cli_available(prov) else "stub"
        # auto: OpenRouter key wins, else a detected CLI, else stub.
        if self.settings.openrouter_api_key:
            return "openrouter"
        preferred = (self.settings.cli_llm_provider or "claude").lower()
        for prov in (preferred, *_KNOWN_CLI_PROVIDERS):
            if self._cli_available(prov):
                return f"{prov}_cli"
        return "stub"

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
        # Pass task_type so the LearnedModelRouter can serve per-task picks. It
        # was dead-wired (resolve(tier, file_hint) only) -> the learned router
        # always queried the empty task bucket and could never serve.
        # ``model_override`` pins a specific model (best-of-N cross-model sampling)
        # and bypasses the router; the (tier, task_type) bucket is unchanged.
        model = model_override or self.router.resolve(tier, file_hint, task_type=task_type)
        backend = self.backend
        # An attached image only matters to the openrouter backend (the only one
        # that speaks the multimodal message shape). stub/CLI ignore it and behave
        # exactly as today — degrade, don't crash (design rule #6).
        if backend == "openrouter" and images:
            model, send_images = self._resolve_vision(model, model_override)
            if send_images:
                result = await self._openrouter(model, prompt, system, max_tokens, json_mode, images)
            else:
                # free_only with no usable free vision model: stay text-only rather
                # than silently billing a paid model (design rule #5 cheap-by-default).
                result = await self._openrouter(model, prompt, system, max_tokens, json_mode)
        elif backend == "openrouter":
            result = await self._openrouter(model, prompt, system, max_tokens, json_mode)
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
            ml = m.lower()
            return ml.endswith(":free") or "free" in ml

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
        except asyncio.TimeoutError:
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

    @property
    def supports_agentic(self) -> bool:
        """CLI backends are full coding agents that can write a whole project."""
        return self.backend.endswith("_cli")

    async def agentic_build(self, prompt: str, workdir: str, timeout: int | None = None,
                            model: str | None = None) -> dict:
        """Run a local coding-agent CLI that writes files directly into workdir.

        This is the RIGHT way to use claude/kimi/copilot for codegen: one
        agentic session that authors a coherent multi-file app, instead of N
        slow per-file completion calls that spin up an agent each and time out.
        ``model`` optionally pins the CLI to a specific model (used by parallel
        code-slicing to route cheap/strong tiers per slice). Returns
        {ok, backend, error}. Only meaningful for *_cli backends.
        """
        backend = self.backend
        if not backend.endswith("_cli"):
            return {"ok": False, "backend": backend, "error": "agentic unsupported"}
        provider = backend[:-4]
        # acceptEdits lets the headless agent write files without prompting.
        # _no_mcp_args keeps the agent from loading the host's ambient MCP fleet.
        nm = _no_mcp_args(self.settings, provider)
        # Stream the agent's NDJSON event log (claude/kimi) so we can detect the
        # terminal `result` event (an accurate success signal — claude -p can
        # exit 0 on a reported error) and watch for a stalled session via an idle
        # guard instead of always burning the full ceiling. copilot has no such
        # mode, so it keeps the blocking path.
        stream = provider in ("claude", "kimi")
        stream_args = ["--output-format", "stream-json", "--verbose"] if stream else []
        # Optional per-call model pin (claude/kimi accept --model); ignored when
        # no model is given so the CLI's default applies (today's behaviour).
        model_args = ["--model", model] if (model and provider in ("claude", "kimi")) else []
        argv = {
            "claude": ["claude", "-p", prompt, "--permission-mode", "acceptEdits", *model_args, *stream_args, *nm],
            "kimi": ["kimi", "-p", prompt, "--permission-mode", "acceptEdits", *model_args, *stream_args, *nm],
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
            # A full multi-file app needs real time — the old cli_llm_timeout*3
            # (15 min) killed claude -p mid-build, shipping a partial/stub. Use a
            # dedicated, larger agentic budget (default 30 min, configurable).
            agentic_timeout = (
                timeout
                or int(getattr(self.settings, "agentic_build_timeout", 0))
                or (self.settings.cli_llm_timeout * 3)
            )
            if stream:
                idle_timeout = int(getattr(self.settings, "agentic_idle_timeout", 0))
                ok = await asyncio.wait_for(
                    self._consume_agentic_stream(proc, provider, idle_timeout),
                    timeout=agentic_timeout,
                )
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
        return {"ok": ok, "backend": backend}

    async def _consume_agentic_stream(self, proc, provider: str, idle_timeout: int) -> bool:
        """Drive a stream-json agentic CLI to completion and report success.

        Reads the NDJSON event stream to EOF (the agent closes stdout when it
        exits), capturing the terminal ``result`` event's error flag for an
        accurate success signal. When ``idle_timeout`` > 0, a gap of that many
        seconds with NO stream activity means the agent has stalled — we kill the
        whole tree and report failure rather than waiting out the hard ceiling.
        stderr is drained concurrently so a chatty agent can't deadlock on a full
        pipe. Returns ``ok``. The outer ``agentic_build`` still wraps this in the
        hard-timeout / CancelledError tree-kill guard.
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
                    # makes readline() raise. Don't fail the build over telemetry —
                    # stop streaming and fall back to the returncode below.
                    log.warning("llm.agentic_stream_overrun", provider=provider)
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
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()  # fall back to single-process kill
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        except Exception:  # noqa: BLE001
            pass

    # ---- backends --------------------------------------------------------
    async def _openrouter(self, model, prompt, system, max_tokens, json_mode,
                          images: list[str] | None = None) -> LLMResult:
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
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "HTTP-Referer": "https://github.com/skyn3t",
            "X-Title": "SkyN3t",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(OPENROUTER_URL, json=body, headers=headers)
            # HTTP errors (429/5xx) propagate so the orchestrator's transient
            # retry classification can retry them.
            resp.raise_for_status()
        # A 200 with a malformed/empty body must degrade, not crash the build —
        # this includes a body that isn't valid JSON (truncation, gateway HTML),
        # so resp.json() is inside the guard too (it raises ValueError/JSONDecodeError).
        try:
            data = resp.json()
            # content can be JSON null (tool-only/empty completions) — coalesce to
            # "" so the contract "text is always a str" holds (matches the CLI path)
            # and downstream len()/json parsing never hits None.
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("llm.openrouter_malformed", error=str(exc)[:160])
            return self._stub(model, prompt, system, json_mode)
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        # OpenRouter sometimes omits `usage` on a 200. Defaulting to 0 made PAID
        # models report $0 — corrupting cost tracking + budget caps. Estimate
        # tokens from text length (~4 chars/token) so a paid call is never free.
        if not model.endswith(":free") and pt == 0 and ct == 0:
            pt = max(1, (len(system or "") + len(prompt or "")) // 4)
            ct = max(1, len(text or "") // 4)
            log.warning("llm.openrouter_usage_missing", model=model, est_tokens=pt + ct)
        # :free models cost $0; otherwise rough estimate.
        cost = 0.0 if model.endswith(":free") else (pt + ct) / 1_000_000 * 0.5
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
