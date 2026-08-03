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
import re
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
        # Make the change survive a restart: persist the allow-listed keys to
        # settings_overrides.json (Settings reads it back on construction).
        # ``durable`` is honest — True only when EVERY observed key persists;
        # a non-durable APPLIED record must not dedupe-block a re-proposal
        # forever (the enacted effect evaporates with the process).
        durable = False
        if observed and self.data_dir is not None:
            from skyn3t.cortex.tuning_store import PERSISTABLE_TUNING, persist_overrides

            to_persist = {k: v for k, v in observed.items() if k in PERSISTABLE_TUNING}
            if to_persist:
                persist_overrides(self.data_dir, to_persist)  # never raises; merge-writes
            durable = set(observed) <= PERSISTABLE_TUNING
        result: dict[str, Any] = {
            "applied": True,
            "changed": applied,
            "previous": before,
            "observed": observed,
            "durable": durable,
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
            from skyn3t.agents.github_fetch import fetch_github_repo_evidence

            evidence = await fetch_github_repo_evidence(url)
        except Exception:  # noqa: BLE001
            evidence = None
        if evidence is None:
            staged = self._stage(proposal, "ingest")  # offline -> keep intent, retryable
            staged["ingested"] = 0
            staged["degraded"] = True
            return staged
        text = evidence.text
        rag = self.rag
        if rag is None:
            return self._stage(proposal, "ingest")
        github_metadata: dict[str, object] = {
            "external_unreviewed": True,
            "source_kind": "github_readme",
            "source_url": evidence.source_url,
            "source_path": evidence.source_path,
        }
        if evidence.pinned_revision:
            github_metadata["pinned_revision"] = evidence.pinned_revision
        if evidence.license:
            github_metadata["license"] = evidence.license
        try:
            try:
                n = rag.ingest_text(
                    text,
                    source=url,
                    kind="github",
                    metadata=github_metadata,
                )
            except TypeError as exc:
                # Preserve compatibility with older pluggable RAG engines. The
                # Runner also treats legacy ``kind=github`` sources as
                # unreviewed, so this fallback cannot make them prompt-injectable.
                if "metadata" not in str(exc):
                    raise
                n = rag.ingest_text(text, source=url, kind="github")
        except Exception as exc:  # noqa: BLE001 - transient RAG error -> degraded, retryable
            staged = self._stage(proposal, "ingest")
            staged["ingested"] = 0
            staged["degraded"] = True
            staged["error"] = f"rag ingest failed: {exc}"
            return staged
        # Also distill a reusable, advisory skill so the approval surfaces on the
        # Skills page (best-effort; never affects the ingest result). Remote text
        # is kept quarantined until a later, explicit promotion checks its proof.
        provenance = None
        if self.skills is not None:
            from skyn3t.intelligence.skill_library import SkillProvenance

            provenance = SkillProvenance(
                source_url=evidence.source_url,
                pinned_revision=evidence.pinned_revision,
                license=evidence.license,
                source_path=evidence.source_path,
                metadata={"skyn3t-source-kind": "github-readme"},
            ).with_content_hash(text)
        skill_slug, skill_error = self._distill_repo_skill(
            url,
            text,
            proposal.payload or {},
            provenance=provenance,
        )
        result = {"applied": True, "ingested": n, "source": url}
        if skill_slug:
            result["skill"] = skill_slug
        elif skill_error:
            # A refused/failed distill is reported as data: the RAG ingest
            # still applied, but no hollow skill file was written.
            result["skill_error"] = skill_error
        return result

    # ---- skill distillation from an ingested repo ------------------------
    _LANG_STACK = {
        "python": "python",
        "javascript": "react",
        "typescript": "react",
        "tsx": "react",
        "jsx": "react",
        "go": "go",
        "rust": "rust",
    }

    # A distilled skill must carry enough real, concrete content to be worth
    # injecting later. Below this many body chars we refuse to write a file at
    # all (the ingest is reported as a failed distill, not an applied skill) —
    # anything thinner is the "consider this repo's structure" placeholder
    # junk that produced hundreds of hollow gh-*.md husks.
    _DISTILL_MIN_BODY_CHARS = 400
    _DISTILL_MAX_BODY_CHARS = 4000
    _DISTILL_MAX_COMMANDS = 8
    _DISTILL_MAX_SECTIONS = 6
    _DISTILL_MAX_EXCERPT_CHARS = 900
    _DISTILL_MAX_TOPIC_TAGS = 4

    _CMD_LINE_RE = re.compile(
        r"^\$?\s*(npm|npx|pnpm|yarn|bun|deno|node|pip|pip3|pipx|python|python3|"
        r"uv|poetry|pdm|hatch|pytest|tox|nox|cargo|go|make|cmake|docker|"
        r"docker-compose|git|curl|brew|apt|apt-get|mvn|gradle|composer|bundle|"
        r"rails|rake|dotnet|swift|xcodebuild|flutter)\b"
    )
    _HEADING_RE = re.compile(r"^#{1,3}\s+(.+?)\s*#*\s*$")
    _HTML_TAG_RE = re.compile(r"<[^>]+>")
    _HTML_BLOCK_RE = re.compile(
        r"^\s*</?(div|picture|source|img|p|a|br|table|thead|tbody|tr|td|th|"
        r"span|details|summary|h[1-6]|ul|ol|li|blockquote|kbd|sup|sub|video|"
        r"figure|figcaption|center|font|!--)\b",
        re.IGNORECASE,
    )
    _MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
    # Rows that carry zero prose: only links/badges/separators (e.g. language
    # switchers "English | 中文", badge bars, "[Docs](...) [Demo](...)").
    _LINK_ONLY_RE = re.compile(r"^(?:\[[^\]]*\]\([^)]*\)|[|·•*_\s\-–—]|<[^>]*>)+$")
    _BOILERPLATE_HEADING_RE = re.compile(
        r"licen[cs]e|contribut|sponsor|acknowledg|credit|changelog|conduct|"
        r"disclaimer|translat|badge|star|donat|backer|author|thank|faq|"
        r"support|community|contact|security|roadmap|related|see also",
        re.IGNORECASE,
    )

    @staticmethod
    def _repo_meta_from_text(text: str) -> dict[str, str]:
        """Parse the header lines fetch_github_repo_text prepends to the README
        (Description / Language · Stars / Topics) as a fallback for payload."""
        meta: dict[str, str] = {}
        m = re.search(r"^Description:\s*(.+)$", text, re.MULTILINE)
        if m:
            meta["description"] = m.group(1).strip()
        m = re.search(r"^Language:\s*([^·\n]+?)\s*·\s*Stars:\s*(\d+)", text, re.MULTILINE)
        if m:
            meta["language"] = m.group(1).strip()
            meta["stars"] = m.group(2)
        m = re.search(r"^Topics:\s*(.+)$", text, re.MULTILINE)
        if m:
            meta["topics"] = m.group(1).strip()
        return meta

    @classmethod
    def _readme_clean_lines(cls, readme: str) -> list[str]:
        """Keep meaningful README lines: drop raw HTML blocks, badge/image
        rows, link-only rows, and comments; strip inline tags. Deterministic,
        no LLM — fence markers survive so command blocks stay extractable."""
        out: list[str] = []
        in_fence = False
        for raw in readme.splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("```"):
                in_fence = not in_fence
                out.append(s)
                continue
            if in_fence:
                out.append(s)
                continue
            if cls._HTML_BLOCK_RE.match(s):
                continue
            no_img = cls._MD_IMAGE_RE.sub("", s)
            if not no_img.strip() or cls._LINK_ONLY_RE.match(no_img.strip()):
                continue
            cleaned = cls._HTML_TAG_RE.sub("", no_img)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" |·•").strip()
            if cleaned:
                out.append(cleaned)
        return out

    @classmethod
    def _extract_commands(cls, lines: list[str]) -> list[str]:
        """Build/run commands from fenced code blocks (``$ `` prompts stripped)."""
        cmds: list[str] = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
            c = line.strip()
            c = c[2:].strip() if c.startswith("$ ") else c
            if cls._CMD_LINE_RE.match(c) and 3 <= len(c) <= 160 and c not in cmds:
                cmds.append(c)
            if len(cmds) >= cls._DISTILL_MAX_COMMANDS:
                break
        return cmds

    @classmethod
    def _extract_sections(cls, lines: list[str]) -> list[tuple[str, str]]:
        """First N meaningful sections (heading + first prose lines), skipping
        boilerplate like License/Contributing/Sponsors and code fences."""
        sections: list[tuple[str, str]] = []
        heading: str | None = None
        buf: list[str] = []
        in_fence = False

        def _flush() -> None:
            if heading and buf:
                sections.append((heading, " ".join(buf)[:280].strip()))

        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = cls._HEADING_RE.match(line)
            if m:
                _flush()
                heading = m.group(1).strip()
                buf = []
                continue
            if heading is not None and len(buf) < 3:
                buf.append(line)
        _flush()
        out: list[tuple[str, str]] = []
        for head, summary in sections:
            if cls._BOILERPLATE_HEADING_RE.search(head) or len(summary) < 24:
                continue
            out.append((head, summary))
            if len(out) >= cls._DISTILL_MAX_SECTIONS:
                break
        return out

    @classmethod
    def _distill_body(
        cls,
        full: str,
        text: str,
        *,
        desc: str,
        lang: str,
        stars: object,
        topics: list[str],
    ) -> str:
        """Compose the skill body: concrete commands + layout/convention
        sections extracted from the README, then a short cleaned excerpt."""
        readme = text.split("README:", 1)[1].strip() if "README:" in text else ""
        lines = cls._readme_clean_lines(readme)
        commands = cls._extract_commands(lines)
        sections = cls._extract_sections(lines)
        excerpt_lines: list[str] = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or cls._HEADING_RE.match(line):
                continue
            excerpt_lines.append(line)
            if len(excerpt_lines) >= 8:
                break
        excerpt = "\n".join(excerpt_lines)[: cls._DISTILL_MAX_EXCERPT_CHARS].strip()

        parts = [
            f"Reusable patterns distilled from **{full}**"
            + (f" ({stars}★)" if stars else "")
            + (f" — {desc.rstrip('.')}" if desc else "")
            + "."
        ]
        if lang:
            parts.append(f"Primary language/stack: {lang}.")
        if topics:
            parts.append("Topics: " + ", ".join(topics[:8]) + ".")
        if commands:
            parts.append(
                "Build/run commands (from the repo's own docs):\n"
                + "\n".join(f"- `{c}`" for c in commands)
            )
        if sections:
            parts.append(
                "Layout & conventions worth copying:\n"
                + "\n".join(f"- **{h}**: {s}" for h, s in sections)
            )
        if excerpt:
            parts.append(f"Reference notes:\n{excerpt}")
        body = "\n\n".join(parts).strip()
        if len(body) > cls._DISTILL_MAX_BODY_CHARS:
            body = body[: cls._DISTILL_MAX_BODY_CHARS].rstrip() + "\n…"
        return body

    def _distill_repo_skill(
        self,
        url: str,
        text: str,
        payload: dict[str, Any],
        *,
        provenance: Any | None = None,
    ) -> tuple[str | None, str | None]:
        """Turn an ingested repo into an advisory Skill.

        Returns ``(slug, None)`` when a skill file was written, ``(None,
        reason)`` when the distill was refused or failed — thin/empty content
        must NOT produce a file (that is how hundreds of 0-byte / placeholder
        gh-*.md husks accumulated), so the caller can report a failed distill
        instead of an applied one. ``(None, None)`` when no library is wired.
        """
        if self.skills is None:
            return None, None
        try:
            if not text or not text.strip():
                return None, "empty repo text"
            m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url)
            owner, repo = (m.group(1), m.group(2)) if m else ("", url.rsplit("/", 1)[-1])
            full = f"{owner}/{repo}".strip("/") or repo
            meta = self._repo_meta_from_text(text)
            desc = str(payload.get("description") or meta.get("description") or "").strip()
            lang = str(payload.get("language") or meta.get("language") or "").strip()
            stars = payload.get("stars") or meta.get("stars") or ""
            topics = [t.strip() for t in str(meta.get("topics") or "").split(",") if t.strip()]
            stack = self._LANG_STACK.get(lang.lower(), "generic")
            body = self._distill_body(full, text, desc=desc, lang=lang, stars=stars, topics=topics)
            if len(body) < self._DISTILL_MIN_BODY_CHARS:
                return None, (
                    f"distilled body too thin ({len(body)} chars < {self._DISTILL_MIN_BODY_CHARS})"
                )
            from skyn3t.agents._common import slugify

            slug = slugify(f"gh-{full}", "gh-repo")
            tags = ["github-distilled", "external-candidate", "hygiene:quarantine"]
            if lang:
                tags.append(lang.lower())
            if stack != "generic" and stack.lower() not in {t.lower() for t in tags}:
                tags.append(stack)
            for topic in topics[: self._DISTILL_MAX_TOPIC_TAGS]:
                t = topic.lower()
                if re.fullmatch(r"[a-z0-9][a-z0-9.+-]{0,39}", t) and t not in tags:
                    tags.append(t)

            # Never trust mutable proposal text to identify its own provenance.
            # The handler supplies facts from GitHub's API when available; direct
            # callers still get a source URL, README endpoint path, and a hash of
            # the retained evidence, but no invented revision or license.
            from skyn3t.intelligence.skill_library import SkillProvenance, content_sha256

            base = provenance if isinstance(provenance, SkillProvenance) else SkillProvenance()
            metadata = dict(base.metadata)
            metadata.setdefault("skyn3t-source-kind", "github-readme")
            skill_provenance = SkillProvenance(
                source_url=base.source_url or url,
                pinned_revision=base.pinned_revision,
                license=base.license,
                content_hash=content_sha256(text),
                source_path=base.source_path or "README",
                tools=base.tools,
                metadata=metadata,
                compatibility=base.compatibility,
            )
            self.skills.add(
                title=f"Patterns: {full}",
                body=body,
                stack=stack,
                tags=tags,
                source="github-distilled",
                slug=slug,
                description=(
                    desc or f"Concrete build/run commands and layout patterns distilled from {full}"
                ),
                provenance=skill_provenance,
            )
            return slug, None
        except Exception as exc:  # noqa: BLE001 - distillation is best-effort
            return None, f"distillation failed: {exc}"

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
