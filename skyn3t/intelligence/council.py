"""Mixture-of-Agents advisory council.

N tool-free advisor models read the state of a build BEFORE any code exists and
hand private engineering guidance to the one agent that actually writes the app.
The coding agent is the aggregator: it holds every tool, it authors every file,
and the advisors only advise.

Adapted from NousResearch/hermes-agent (MIT, Copyright (c) 2025 Nous Research).
Borrowed mechanisms and their sources:

* tool-free advisor system prompt (not-the-actor / never-claim-execution, with
  bad-vs-good examples) — ``agent/moa_loop.py:233-262``
* "judge the state above" advisory trailer — ``agent/moa_loop.py:947-952``
* guidance block handed to the aggregator — ``agent/moa_loop.py:2042-2054``
* ``[failed: {exc}]`` sentinel; a failed advisor is never fatal —
  ``agent/moa_loop.py:574-583``
* failed advisors filtered out of the aggregator prompt —
  ``agent/moa_loop.py:1998-2018``
* all-advisors-failed => the aggregator simply acts alone —
  ``agent/moa_loop.py:2020-2031``
* bounded parallel fan-out with a worker cap — ``agent/moa_loop.py:154-160,
  732-830``
* per-advisor cost at that advisor's OWN model rate — ``agent/moa_loop.py:163-214``
* head-truncation of oversized advisory material — ``agent/moa_loop.py:216-224``
* once-per-turn (``user_turn``) fan-out cadence as the default —
  ``agent/moa_loop.py:1764-1782``
* static multi-provider advisor slot list — ``hermes_cli/moa_config.py:14-22``

Deliberately NOT ported, and why:

* the virtual-provider swap (``agent/agent_init.py:1065-1084``) — Hermes funnels
  every call through one chat loop, but here codegen goes through
  ``LLMClient.agentic_build`` (a CLI subprocess or OpenRouter's own tool loop),
  so a facade at ``complete()`` would advise the cheap stages and MISS codegen.
* ``per_iteration`` / ``every_n`` cadences (``moa_loop.py:1783-1856``) — four of
  five backends are one-shot subprocesses with no per-iteration injection point.
* the degraded-mode notice (``moa_loop.py:2032-2041``) — Hermes' aggregator talks
  to a human and may need to disclose degradation; ours writes source files,
  where "your advisors failed" is prompt noise that can only confuse codegen.
* the privacy filter (``moa_loop.py:24-150``) — this is a local lab.

Design rules this module answers to (docs/ARCHITECTURE.md): #5 cheap by default
(off unless configured, one fan-out per build), #6 degrade don't crash (an
advisor failure, a dead provider, or a council exception can never fail a
build). It adds NO gate: the council never inspects output, never scores, never
touches the verdict.

Import has zero side effects.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from skyn3t.adapters.model_slot import ModelSlot, parse_slots
from skyn3t.core.model_router import Tier, is_free_model_id

log = structlog.get_logger(__name__)

# Adapted from hermes-agent ``agent/moa_loop.py:233-262``. The six structural
# moves are kept (not the actor -> no tools -> never claim execution, with
# bad/good examples -> your job is judgement -> assume referenced things exist ->
# respond directly, private guidance), retargeted from "analyse a live tool
# transcript" to "read a build spec before any code exists".
_ADVISOR_SYSTEM = (
    "You are a reference advisor in a Mixture of Agents (MoA) council for an "
    "automated app builder. You are NOT the agent that writes the app and you do "
    "NOT execute anything: you cannot call tools, read or write files, run "
    "commands, install packages, browse, or access repositories or URLs, and you "
    "should not try to or apologize for being unable to. A separate coding agent "
    "holds those capabilities and will author every file itself.\n\n"
    "CRITICAL: you must NEVER claim or imply that you have written, run, "
    "inspected, or tested anything. You can only reason about the brief, stack "
    "and plan below and advise the coding agent. Examples of what to avoid:\n"
    '- Bad: "I created src/game.js and it runs."\n'
    '- Bad: "I checked package.json and the dependencies are fine."\n'
    '- Bad: "I ran the tests and two of them fail."\n'
    '- Good: "src/game.js should own the update loop; keep rendering out of it."\n'
    '- Good: "This stack needs vite in devDependencies or the build step fails."\n'
    '- Good: "The plan has no path for an empty result set — add one."\n\n'
    "The material below is the complete state of this build BEFORE any code "
    "exists. Give the coding agent your single most useful engineering judgement "
    "about THIS app. Cover, briefly and concretely:\n"
    "1. The one architectural decision that most determines whether this app "
    "actually works, and which way to decide it.\n"
    "2. Features or behaviours the brief clearly implies but the plan omits.\n"
    "3. The specific failure modes this stack and this app shape are prone to — "
    "broken wiring, imports that resolve to nothing, missing dependency-manifest "
    "entries, a dead entrypoint, unhandled empty and error states — and how to "
    "avoid them here.\n"
    "4. Anything in the plan that is wrong, thin, or over-engineered.\n\n"
    "Be specific to this brief: generic engineering advice is worthless here. "
    "Name real files, real functions, real libraries. Assume every referenced "
    "file, dependency or asset exists and reason about it from the context given "
    "rather than asking for access. Do NOT write the app: no full file bodies, no "
    "long code listings — short illustrative snippets only.\n\n"
    "Respond with your advice directly — no preamble, no restating the brief, no "
    "disclaimers about tools or access. Your response is private guidance handed "
    "to the coding agent, not text shown to a user. Keep it under 400 words. "
    "NEVER claim to have executed anything."
)

# Adapted from hermes-agent's ``_ADVISORY_INSTRUCTION`` (``moa_loop.py:947-952``).
_ADVISOR_TASK = (
    "Brief:\n{brief}\n\n"
    "Stack: {stack}\n"
    "{plan_block}"
    "\n[The above is the full state of this build before any code is written. "
    "Give your most useful engineering judgement per your instructions.]"
)

# Adapted from hermes-agent's guidance block (``moa_loop.py:2042-2054``). The
# final clause has no Hermes counterpart and is load-bearing here: their
# aggregator writes a chat reply, ours writes SOURCE FILES THAT SHIP, and a cheap
# model will otherwise write "// per the advisory council" into main.js.
_GUIDANCE_HEADER = (
    "ADVISORY COUNCIL (Mixture of Agents) — private engineering guidance for you, "
    "the coding agent. You are the aggregator and the ONLY model that writes "
    "files.\n"
    "These advisors had no tools and did not inspect this workspace, so treat "
    "their advice as informed opinion, not fact: take what makes the app better, "
    "ignore what does not. They do NOT override the brief, the stack, the planned "
    "file list, or any directive above. Never mention the council, the advisors, "
    "or this guidance in the code, comments, commit messages, or README.\n"
    "Advisors: {labels}\n"
)

_TRUNCATION_NOTE = "\n[... advice truncated ...]"


@dataclass(slots=True)
class AdvisorOutput:
    """One advisor's contribution, successful or not."""

    label: str
    provider: str = ""
    model: str = ""
    text: str = ""
    ok: bool = False
    error: str = ""
    cost_usd: float = 0.0
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
            "ok": self.ok,
            "chars": len(self.text),
            "cost_usd": round(self.cost_usd, 6),
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


@dataclass(slots=True)
class CouncilAdvice:
    """The council's assembled output. ``guidance`` is what codegen sees."""

    guidance: str = ""
    advisors: list[AdvisorOutput] = field(default_factory=list)
    degraded: bool = False
    dropped: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for a in self.advisors if a.ok)

    @property
    def cost_usd(self) -> float:
        return round(sum(a.cost_usd for a in self.advisors), 6)

    def to_dict(self) -> dict[str, Any]:
        """Bounded record for ``manifest.extra["moa"]`` — never advisor text."""
        return {
            "advisors": [a.to_dict() for a in self.advisors],
            "failed": [a.label for a in self.advisors if not a.ok],
            "dropped": list(self.dropped),
            "guidance_chars": len(self.guidance),
            "cost_usd": self.cost_usd,
            "degraded": self.degraded,
        }


class CouncilEngine:
    """Runs one bounded, tool-free advisor fan-out and assembles guidance."""

    def __init__(self, llm: Any, settings: Any, advisors: str | None = None) -> None:
        self.llm = llm
        self.settings = settings
        # Per-build advisor selection (the dashboard's pre-build picker) wins
        # over the configured default, so a single operator can vary the council
        # per build without editing settings. ``None`` = use the setting; an
        # explicit empty string = deliberately no advisors for this build.
        self.advisors_override = advisors

    # ---- configuration -------------------------------------------------
    def slots(self) -> list[ModelSlot]:
        raw = (
            self.advisors_override
            if self.advisors_override is not None
            else (getattr(self.settings, "moa_advisors", "") or "")
        )
        return parse_slots(raw)

    def enabled(self) -> bool:
        """Whether a council run should happen at all.

        The stub backend short-circuits deliberately: it keeps the entire test
        suite's codegen prompts byte-stable and offline builds at $0, and stops
        a stub's canned "Offline response." text being injected as if it were
        real advice.
        """
        if not bool(getattr(self.settings, "moa_enabled", False)):
            return False
        if not self.slots():
            return False
        backend = str(getattr(self.llm, "backend", "") or "")
        return backend != "stub"

    # ---- fan-out -------------------------------------------------------
    async def _run_advisor(self, slot: ModelSlot, task: str) -> AdvisorOutput:
        label = slot.address
        started = time.monotonic()
        out = AdvisorOutput(label=label, provider=slot.provider, model=slot.model)
        timeout = max(10, int(getattr(self.settings, "moa_advisor_timeout", 180)))
        try:
            kwargs: dict[str, Any] = {}
            if slot.provider:
                kwargs["provider_override"] = slot.provider
            if slot.model:
                kwargs["model_override"] = slot.model
            result = await asyncio.wait_for(
                self.llm.complete(
                    task,
                    tier=Tier.CHEAP,
                    system=_ADVISOR_SYSTEM,
                    max_tokens=int(getattr(self.settings, "moa_advisor_max_tokens", 1200)),
                    task_type="moa_advisor",
                    **kwargs,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            out.error = f"timed out after {timeout}s"
            log.warning("moa.advisor_timeout", label=label, timeout=timeout)
            out.duration_ms = (time.monotonic() - started) * 1000.0
            return out
        except Exception as exc:  # noqa: BLE001 - an advisor may never break a build
            out.error = str(exc)[:160]
            log.warning("moa.advisor_failed", label=label, error=out.error)
            out.duration_ms = (time.monotonic() - started) * 1000.0
            return out
        out.duration_ms = (time.monotonic() - started) * 1000.0
        out.cost_usd = float(getattr(result, "cost_usd", 0.0) or 0.0)
        text = str(getattr(result, "text", "") or "").strip()
        backend = str(getattr(result, "backend", "") or "")
        status = str(getattr(result, "status", "") or "")
        # A slot whose provider was unavailable degrades to the stub inside
        # complete(). That canned text must NOT reach the codegen prompt dressed
        # up as advice, so treat it as a failed advisor rather than a success.
        if backend == "stub" and slot.provider not in ("", "stub"):
            out.error = "provider unavailable (degraded to stub)"
            log.warning("moa.advisor_unavailable", label=label)
            return out
        if status.startswith("failed_cli"):
            out.error = status
            log.warning("moa.advisor_cli_failed", label=label, status=status)
            return out
        if not text:
            out.error = "empty response"
            return out
        out.text = text
        out.ok = True
        return out

    async def advise(self, *, brief: str, stack: str = "", plan: str = "") -> CouncilAdvice:
        """Fan out to every configured advisor and assemble guidance.

        Never raises. Returns empty guidance when the council is off, when every
        advisor fails, or on any unexpected error — in which case the codegen
        prompt is byte-identical to a council-off build.
        """
        if not self.enabled():
            return CouncilAdvice()
        try:
            return await self._advise(brief=brief, stack=stack, plan=plan)
        except Exception as exc:  # noqa: BLE001 - the council may never break a build
            log.warning("moa.council_failed", error=str(exc)[:160])
            return CouncilAdvice(degraded=True)

    async def _advise(self, *, brief: str, stack: str, plan: str) -> CouncilAdvice:
        slots, dropped = self._admissible_slots()
        if not slots:
            return CouncilAdvice(degraded=bool(dropped), dropped=dropped)
        plan_block = f"Planned files / architecture:\n{plan}\n" if plan.strip() else ""
        task = _ADVISOR_TASK.format(
            brief=brief.strip() or "(no brief supplied)",
            stack=stack.strip() or "(unpinned)",
            plan_block=plan_block,
        )
        limit = max(1, int(getattr(self.settings, "moa_max_concurrency", 4)))
        sem = asyncio.Semaphore(limit)

        async def _one(slot: ModelSlot) -> AdvisorOutput:
            async with sem:
                return await self._run_advisor(slot, task)

        results = await asyncio.gather(
            *(_one(slot) for slot in slots), return_exceptions=True
        )
        advisors: list[AdvisorOutput] = []
        for slot, res in zip(slots, results, strict=False):
            if isinstance(res, BaseException):
                advisors.append(
                    AdvisorOutput(
                        label=slot.address,
                        provider=slot.provider,
                        model=slot.model,
                        error=str(res)[:160],
                    )
                )
            else:
                advisors.append(res)
        advice = CouncilAdvice(advisors=advisors, dropped=dropped)
        advice.guidance = self._assemble(advisors)
        advice.degraded = bool(dropped) or any(not a.ok for a in advisors)
        if not advice.guidance:
            log.warning("moa.all_advisors_failed", count=len(advisors))
        return advice

    def _admissible_slots(self) -> tuple[list[ModelSlot], list[dict[str, str]]]:
        """Split configured slots into runnable ones and recorded drops.

        Dropping is recorded, never silent: a council that looks configured but
        is empty because ``free_only`` filtered every paid pin is otherwise
        indistinguishable from one that ran.
        """
        free_only = bool(getattr(self.settings, "free_only", True))
        no_claude = bool(getattr(self.settings, "no_claude", False))
        keep: list[ModelSlot] = []
        dropped: list[dict[str, str]] = []
        for slot in self.slots():
            if no_claude and "claude" in f"{slot.provider}{slot.model}".lower():
                dropped.append({"slot": slot.address, "reason": "no_claude"})
                continue
            # free_only governs metered spend only. A signed-in CLI bills a
            # subscription the operator already holds, so filtering CLI slots
            # here would silently empty a perfectly valid local council.
            # Uses the canonical predicate rather than an inline ":free" test:
            # spelling it by hand here dropped genuinely free ids that the
            # router accepts (e.g. "openrouter/free"), and recorded them as
            # free_only drops — a confusing lie in the manifest.
            if (
                free_only
                and slot.provider == "openrouter"
                and slot.model
                and not is_free_model_id(slot.model)
            ):
                dropped.append({"slot": slot.address, "reason": "free_only"})
                continue
            keep.append(slot)
        for entry in dropped:
            log.info("moa.slot_dropped", slot=entry["slot"], reason=entry["reason"])
        return keep, dropped

    def _assemble(self, advisors: list[AdvisorOutput]) -> str:
        """Build the guidance block from the advisors that actually returned."""
        good = [a for a in advisors if a.ok and a.text]
        if not good:
            return ""
        budget = max(500, int(getattr(self.settings, "moa_advisor_block_bytes", 3000)))
        blocks: list[str] = []
        for index, advisor in enumerate(good, start=1):
            text = advisor.text
            if len(text) > budget:
                text = text[: budget - len(_TRUNCATION_NOTE)] + _TRUNCATION_NOTE
            blocks.append(f"Advisor {index} — {advisor.label}:\n{text}")
        header = _GUIDANCE_HEADER.format(labels=", ".join(a.label for a in good))
        return header + "\n" + "\n\n".join(blocks)
