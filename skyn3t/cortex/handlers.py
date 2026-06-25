"""Apply-handlers — turn an approved :class:`Proposal` into a real change.

Each proposal type maps to a handler that knows how to enact it. Handlers are
deliberately conservative: they never crash the loop (design rule #6) and they
return a structured result describing what happened so the proposal record can
be updated and an event emitted (design rule #7).

Tuning proposals are applied to an in-memory overrides dict (we never rewrite
``settings.py`` — that file is owned elsewhere and overrides feed the live
process). Feature / ingest / code_patch handlers stage their intent durably so
a human or a downstream builder can pick them up; nothing destructive happens
automatically.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import time
from typing import Any

from skyn3t.config.settings import Settings, get_settings
from skyn3t.cortex.proposal_store import Proposal, ProposalType

# A handler takes a proposal and returns a result dict. It must not raise.
Handler = Callable[[Proposal], Awaitable[dict[str, Any]]]


class HandlerRegistry:
    """Routes proposals to type-specific apply handlers.

    ``overrides`` is a live dict the rest of the process can read to pick up
    tuning changes without touching the settings file. ``stage_dir`` is where
    non-tuning proposals are written for downstream pickup.

    ``settings`` is the live runtime configuration object that the rest of the
    process actually reads (Planner, StudioRunner, components). Applied tuning
    changes are pushed onto it via ``setattr`` so the change is *observable* by
    the code that uses it — otherwise the ``overrides`` dict would be a dead
    record nothing consults. When not supplied we resolve the shared
    ``get_settings()`` singleton (the same object StudioRunner/Planner read).
    """

    def __init__(
        self,
        overrides: dict[str, Any] | None = None,
        stage_dir: Path | None = None,
        settings: Settings | None = None,
        rag: Any | None = None,
        skills: Any | None = None,
        agents: dict[str, Any] | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.overrides: dict[str, Any] = overrides if overrides is not None else {}
        self.stage_dir = Path(stage_dir) if stage_dir else None
        # Live orchestrator agents (name -> agent), so an approved PROMPT proposal
        # can write its evolved instruction onto the matching agent's config and
        # actually take effect. Empty when cortex runs without an orchestrator.
        self.agents: dict[str, Any] = agents if agents is not None else {}
        # Where to persist prompt overrides so they survive a process restart.
        self.data_dir = Path(data_dir) if data_dir else None
        # Optional RAG engine: when present, INGEST proposals actually fetch +
        # ingest the source into the corpus (so recall improves future builds).
        # When None we fall back to staging intent only (unchanged behaviour).
        self.rag = rag
        # Optional SkillLibrary: when present, an ingested repo also distills a
        # reusable, advisory skill (so approvals surface on the Skills page, not
        # only in RAG recall).
        self.skills = skills
        # Fall back to the process-wide settings singleton so tuning changes
        # land on the object live consumers read (degrade gracefully if the
        # config layer is unavailable for any reason).
        if settings is None:
            try:
                settings = get_settings()
            except Exception:  # noqa: BLE001 - never let config break the loop
                settings = None
        self.settings = settings
        self._handlers: dict[ProposalType, Handler] = {
            ProposalType.TUNING: self._apply_tuning,
            ProposalType.FEATURE: self._stage_feature,
            ProposalType.INGEST: self._stage_ingest,
            ProposalType.CODE_PATCH: self._stage_code_patch,
            ProposalType.PROMPT: self._apply_prompt,
        }

    def register(self, ptype: ProposalType, handler: Handler) -> None:
        """Override a handler (used in tests or by other components)."""
        self._handlers[ptype] = handler

    async def apply(self, proposal: Proposal) -> dict[str, Any]:
        """Apply a proposal. Always returns a result dict, never raises."""
        handler = self._handlers.get(proposal.type)
        if handler is None:
            return {"applied": False, "error": f"no handler for {proposal.type.value}"}
        try:
            return await handler(proposal)
        except Exception as exc:  # noqa: BLE001 - handler errors are data, not crashes
            return {"applied": False, "error": str(exc)}

    # ---- type handlers ---------------------------------------------------
    async def _apply_tuning(self, proposal: Proposal) -> dict[str, Any]:
        """Set one or more override values from the proposal payload.

        Payload shapes accepted:
          {"setting": "best_of_n", "value": 2}
          {"overrides": {"best_of_n": 2, "debate_enabled": true}}
        """
        applied: dict[str, Any] = {}
        payload = proposal.payload or {}
        if "setting" in payload:
            applied[str(payload["setting"])] = payload.get("value")
        for k, v in (payload.get("overrides") or {}).items():
            applied[str(k)] = v
        if not applied:
            return {"applied": False, "error": "tuning proposal had no setting/overrides"}
        before = {k: self.overrides.get(k) for k in applied}
        self.overrides.update(applied)
        # Make the change actually live: push each key that is a real runtime
        # settings field onto the live settings object so Planner / runner /
        # components observe it. Keys that don't map to a known field (or fail
        # validation) stay in ``overrides`` only and are reported as unobserved.
        observed: dict[str, Any] = {}
        unobserved: list[str] = []
        for k, v in applied.items():
            if self._set_setting(k, v):
                observed[k] = v
            else:
                unobserved.append(k)
        result: dict[str, Any] = {
            "applied": True,
            "changed": applied,
            "previous": before,
            "observed": observed,
        }
        if unobserved:
            result["unobserved"] = unobserved
        return result

    def _set_setting(self, key: str, value: Any) -> bool:
        """Apply one tuning key onto the live settings object.

        Returns True only if the value was actually set (i.e. the key is a real
        settings field and the assignment validated). This keeps ``observed``
        honest: a key reported as observed is genuinely readable by consumers.
        """
        settings = self.settings
        if settings is None:
            return False
        # Only touch declared fields — never inject arbitrary attributes.
        fields = getattr(type(settings), "model_fields", None)
        if not fields or key not in fields:
            return False
        try:
            setattr(settings, key, value)
        except Exception:  # noqa: BLE001 - bad value is data, not a crash
            return False
        return True

    async def _apply_prompt(self, proposal: Proposal) -> dict[str, Any]:
        """Apply an evolved instruction to the target agent.

        Payload shape (from PromptReflectionLoop):
          {"agent": "code", "prompt_candidate": {"candidate_instruction": "..."}}

        The override is persisted (so it survives a restart) AND written onto any
        matching live agent's ``config`` so the next build's prompts use it. When
        no live agent matches (cortex running without an orchestrator), it is
        still persisted and reported with ``live: False`` — durable, not silently
        dropped.
        """
        payload = proposal.payload or {}
        target = str(payload.get("agent") or "").strip()
        cand = payload.get("prompt_candidate") or {}
        instruction = str(
            cand.get("candidate_instruction") or payload.get("instruction") or ""
        ).strip()
        if not target or not instruction:
            return {"applied": False, "error": "prompt proposal missing agent/instruction"}
        if self.data_dir is not None:
            try:
                from skyn3t.cortex.prompt_store import persist_prompt_override

                persist_prompt_override(self.data_dir, target, instruction)
            except Exception:  # noqa: BLE001 - persistence is best-effort
                pass
        live = self._apply_prompt_to_live(target, instruction)
        return {"applied": True, "agent": target, "live": live, "instruction": instruction}

    def _apply_prompt_to_live(self, target: str, instruction: str) -> bool:
        """Write the override onto every live agent matching ``target``.

        ``target`` is a capability/stage name (e.g. "code"), matched against an
        agent's type, name, or advertised capabilities. Returns True if at least
        one live agent was updated.
        """
        applied = False
        for agent in (self.agents or {}).values():
            if not self._agent_matches(agent, target):
                continue
            cfg = getattr(agent, "config", None)
            if isinstance(cfg, dict):
                cfg["prompt_override"] = instruction
                applied = True
        return applied

    @staticmethod
    def _agent_matches(agent: Any, target: str) -> bool:
        t = target.lower()
        if str(getattr(agent, "agent_type", "")).lower() == t:
            return True
        if str(getattr(agent, "name", "")).lower() == t:
            return True
        try:
            return t in {str(n).lower() for n in (getattr(agent, "capability_names", None) or ())}
        except Exception:  # noqa: BLE001
            return False

    async def _stage_feature(self, proposal: Proposal) -> dict[str, Any]:
        return self._stage(proposal, "feature")

    async def _stage_ingest(self, proposal: Proposal) -> dict[str, Any]:
        """Ingest the source into RAG when an engine is wired; else stage intent.

        Strictly opt-in on ``self.rag``: with no engine this is byte-for-byte the
        old staging behaviour. Offline / RAG errors degrade to a *retryable*
        staged record (not ``applied: False``) so a transient failure doesn't
        mark the proposal permanently FAILED.
        """
        payload = proposal.payload or {}
        repo_or_url = payload.get("url") or payload.get("repo")
        if self.rag is not None and repo_or_url:
            result = await self._ingest_github(str(repo_or_url), proposal)
            if result is not None:
                return result
        return self._stage(proposal, "ingest")

    async def _ingest_github(self, repo_or_url: str, proposal: Proposal) -> dict[str, Any] | None:
        url = repo_or_url if "github.com/" in repo_or_url else f"https://github.com/{repo_or_url}"
        try:
            from skyn3t.agents.github_fetch import fetch_github_repo_text

            text = await fetch_github_repo_text(url)
        except Exception:  # noqa: BLE001
            text = None
        if not text:
            staged = self._stage(proposal, "ingest")  # offline -> keep intent, retryable
            staged["ingested"] = 0
            staged["degraded"] = True
            return staged
        rag = self.rag
        if rag is None:
            return self._stage(proposal, "ingest")
        try:
            n = rag.ingest_text(text, source=url, kind="github")
        except Exception as exc:  # noqa: BLE001 - transient RAG error -> degraded, retryable
            staged = self._stage(proposal, "ingest")
            staged["ingested"] = 0
            staged["degraded"] = True
            staged["error"] = f"rag ingest failed: {exc}"
            return staged
        # Also distill a reusable, advisory skill so the approval surfaces on the
        # Skills page (best-effort; never affects the ingest result).
        skill_slug = self._distill_repo_skill(url, text, proposal.payload or {})
        result = {"applied": True, "ingested": n, "source": url}
        if skill_slug:
            result["skill"] = skill_slug
        return result

    # ---- skill distillation from an ingested repo ------------------------
    _LANG_STACK = {
        "python": "python", "javascript": "react", "typescript": "react",
        "tsx": "react", "jsx": "react", "go": "go", "rust": "rust",
    }

    def _distill_repo_skill(self, url: str, text: str, payload: dict[str, Any]) -> str | None:
        """Turn an ingested repo into an advisory Skill. Returns the slug or None."""
        if self.skills is None:
            return None
        try:
            import re as _re

            m = _re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url)
            owner, repo = (m.group(1), m.group(2)) if m else ("", url.rsplit("/", 1)[-1])
            full = f"{owner}/{repo}".strip("/") or repo
            desc = str(payload.get("description") or "").strip()
            lang = str(payload.get("language") or "").strip()
            stars = payload.get("stars")
            stack = self._LANG_STACK.get(lang.lower(), "generic")
            # A short README excerpt as reference material.
            excerpt = ""
            if "README:" in text:
                excerpt = text.split("README:", 1)[1].strip()[:600]
            body = (
                f"Reference patterns from **{full}**"
                + (f" ({stars}★)" if stars else "")
                + (f" — {desc}" if desc else "")
                + ".\n\nWhen building similar "
                + (f"{lang} " if lang else "")
                + "projects, consider this repo's structure and approach."
                + (f"\n\nREADME excerpt:\n{excerpt}" if excerpt else "")
            )
            from skyn3t.agents._common import slugify

            slug = slugify(f"gh-{full}", "gh-repo")
            tags = ["github-distilled"]
            if lang:
                tags.append(lang.lower())
            self.skills.add(
                title=f"Patterns: {full}",
                body=body,
                stack=stack,
                tags=tags,
                source="github-distilled",
                slug=slug,
            )
            return slug
        except Exception:  # noqa: BLE001 - distillation is best-effort
            return None

    async def _stage_code_patch(self, proposal: Proposal) -> dict[str, Any]:
        return self._stage(proposal, "code_patch")

    # ---- staging ---------------------------------------------------------
    def _stage(self, proposal: Proposal, kind: str) -> dict[str, Any]:
        """Persist the proposal as a staged artifact for downstream pickup.

        Safe by default: we never execute arbitrary code or hit the network
        here. We record intent durably so a builder/human can act on it.
        """
        record = {
            "kind": kind,
            "proposal_id": proposal.id,
            "title": proposal.title,
            "payload": proposal.payload,
            "staged_at": time(),
        }
        if self.stage_dir is None:
            # No disk target configured — keep it purely in-memory.
            return {"applied": True, "staged": "memory", "record": record}
        try:
            self.stage_dir.mkdir(parents=True, exist_ok=True)
            path = self.stage_dir / f"{kind}-{proposal.id}.json"
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            return {"applied": True, "staged": str(path)}
        except OSError as exc:
            return {"applied": False, "error": f"stage write failed: {exc}"}
