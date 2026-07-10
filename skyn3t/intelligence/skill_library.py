"""Skill library — scored skills as markdown, injected as non-binding advice.

A *skill* is a short, reusable how-to written in markdown with YAML-ish front
matter holding its score. Skills are:

  * stored as ``<slug>.md`` files under a skills directory,
  * injected into a matching build as *advisory* context (never binding),
  * scored after each build via :meth:`record_use` (helpful / not); mutable
    scores live in a sidecar JSON file so source skill markdown stays stable,
  * auto-promoted from a recurring *build pattern* to a first-class skill once
    that pattern wins reliably (>= ~90% helpful over 20+ uses) — closing the
    learning edge between :mod:`build_patterns` and the skill set.

Everything is offline & dependency-free. Disk is best-effort: if the directory
cannot be written we keep skills in memory (design rule #6). Import has no side
effects (design rule #4).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text

try:
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - defensive
    _log = None  # type: ignore[assignment]


# Promotion thresholds. The old 20-use / 90%-win gate could never fire (the
# busiest shape ever observed had 8 uses), so no pattern was ever promoted to a
# skill — the factory never "grew". Lowered to a reachable bar while still
# requiring a real, repeated win before a shape becomes advice.
PROMOTE_MIN_USES = 4
PROMOTE_MIN_RATE = 0.66
_SCORES_FILENAME = ".skill_scores.json"

_UNIVERSAL_GENERIC_TAGS = frozenset({
    "quality", "verification", "testing", "ci", "delivery", "security",
    "smoke-test", "healthcheck", "build-pattern", "reproducibility",
    "dependencies", "secrets", "docs", "documentation", "packaging",
})

_DESKTOP_TAGS = frozenset({"desktop", "tauri", "native"})
_MOBILE_TAGS = frozenset({"mobile", "expo", "react-native", "react_native"})
_GAME_TAGS = frozenset({"game", "gamedev", "phaser", "arcade", "shmup", "shooter"})
_CLI_TAGS = frozenset({"cli", "command-line", "commandline"})
_QUARANTINE_TAGS = frozenset({"hygiene:quarantine", "quarantine", "disabled"})

# Group equivalent stack vocabularies so a build's detected stack matches skills
# tagged with a sibling name (e.g. a 'cli' build should see 'python' skills).
_STACK_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"python", "python_cli", "cli", "script"}),
    frozenset({"react", "react_vite", "vite", "frontend"}),
    # Next.js / Astro / Remix are real builder stacks now — each gets its own
    # group so framework-specific skills don't bleed across (a Next.js build
    # should not be handed Astro/Remix advice, and vice versa).
    frozenset({"nextjs", "next", "next.js"}),
    frozenset({"astro"}),
    frozenset({"remix"}),
    frozenset({"vue", "vuejs", "vue.js"}),
    frozenset({"sveltekit", "svelte", "svelte-kit"}),
    frozenset({"react_ts", "react-typescript", "typescript", "ts", "tsx"}),
    frozenset({"react_native", "mobile", "expo"}),
    frozenset({"node", "node_express", "express"}),
    frozenset({"fastapi", "flask", "django", "python_api"}),
    frozenset({"static", "static_html", "html"}),
    frozenset({"agent_pack", "agent-pack", "agents", "role_pack", "role-pack"}),
    frozenset({"mcp", "mcp_server", "mcp-server", "tool_server", "tool-server"}),
    frozenset({"rag", "retrieval", "vector", "knowledge_base", "knowledge-base"}),
    frozenset({"workflow", "automation", "orchestration", "pipeline"}),
    # Phaser game: cluster game/phaser-tagged skills so a game build sees them
    # without bleeding React component advice into a canvas game.
    frozenset({"phaser", "phaser3", "phaserjs", "game", "arcade"}),
)


def _stack_aliases(stack: str) -> frozenset[str]:
    s = (stack or "").strip().lower()
    for group in _STACK_GROUPS:
        if s in group:
            return group
    return frozenset({s}) if s else frozenset()


def _skill_tags_compatible(stack: str, sk_tags: set[str]) -> bool:
    if sk_tags & _QUARANTINE_TAGS:
        return False
    aliases = _stack_aliases(stack)
    if sk_tags & _DESKTOP_TAGS and not (aliases & {"desktop", "tauri"}):
        return False
    if sk_tags & _MOBILE_TAGS and not (aliases & {"react_native", "mobile", "expo"}):
        return False
    if sk_tags & _GAME_TAGS and not (aliases & {"phaser", "game", "arcade"}):
        return False
    if sk_tags & _CLI_TAGS and not (aliases & {"python", "python_cli", "cli", "script"}):
        return False
    return True


@dataclass(slots=True)
class Skill:
    slug: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    stack: str = "generic"
    uses: int = 0
    helpful: int = 0
    created: float = field(default_factory=time.time)
    source: str = "manual"
    # Continuous reward (Phase B): sum of per-use quality in [0,1]. Defaults to
    # the binary `helpful` count for skills graded before this field existed, so
    # their score is unchanged (backward compatible).
    quality_sum: float | None = None

    def __post_init__(self) -> None:
        if self.quality_sum is None:
            self.quality_sum = float(self.helpful)

    @property
    def score(self) -> float:
        """Mean per-use quality in [0, 1]; unused skills default to 0.5.

        Continuous: a build that scored 0.92 rewards more than one that scraped a
        0.61 'go', and a 0.35 no_go still records partial signal — unlike the old
        binary helpful/uses rate.
        """
        return (self.quality_sum or 0.0) / self.uses if self.uses else 0.5

    def to_markdown(self) -> str:
        fm = [
            "---",
            f"slug: {self.slug}",
            f"title: {self.title}",
            f"stack: {self.stack}",
            f"tags: {', '.join(self.tags)}",
            f"uses: {self.uses}",
            f"helpful: {self.helpful}",
            f"quality_sum: {self.quality_sum or 0.0:.4f}",
            f"score: {self.score:.3f}",
            f"source: {self.source}",
            "---",
            "",
            self.body.strip(),
            "",
        ]
        return "\n".join(fm)

    def as_advice(self) -> str:
        return f"### Skill: {self.title} (score {self.score:.0%})\n{self.body.strip()}"


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "skill"


def parse_skill(text: str, *, fallback_slug: str = "skill") -> Skill:
    """Parse a markdown skill (front matter optional)."""
    m = _FM_RE.match(text)
    meta: dict[str, str] = {}
    body = text
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    tags = [t.strip() for t in meta.get("tags", "").split(",") if t.strip()]
    # Accept SkyN3t's own front matter (slug/title) AND external skill files
    # (e.g. addyosmani/agent-skills) that use `name`/`description`, so a repo of
    # markdown skills imports cleanly. (Phase B/B3)
    title = meta.get("title") or meta.get("name") or fallback_slug
    description = meta.get("description", "").strip()
    if description and description.lower() not in body.lower():
        body = f"{description}\n\n{body}"
    return Skill(
        slug=meta.get("slug") or _slugify(title),
        title=title,
        body=body.strip(),
        tags=tags,
        stack=meta.get("stack", "generic"),
        uses=int(meta.get("uses", "0") or 0),
        helpful=int(meta.get("helpful", "0") or 0),
        quality_sum=float(meta["quality_sum"]) if meta.get("quality_sum") else None,
        source=meta.get("source", "manual"),
    )


class SkillLibrary:
    """A scored, file-backed library of advisory skills."""

    def __init__(self, skills_dir: Path | str | None = None) -> None:
        self.dir = Path(skills_dir) if skills_dir else None
        self._skills: dict[str, Skill] = {}
        if self.dir is not None:
            self._load()

    # ---- persistence (best-effort) ------------------------------------
    def _load(self) -> None:
        if self.dir is None or not self.dir.exists():
            return
        for f in sorted(self.dir.glob("*.md")):
            try:
                sk = parse_skill(f.read_text(), fallback_slug=f.stem)
                if not sk.body.strip():
                    continue
                self._skills[sk.slug] = sk
            except Exception as exc:  # noqa: BLE001
                if _log:
                    _log.warning("skills.parse_failed", file=str(f), error=str(exc))
        self._load_scores()

    def _load_scores(self) -> None:
        if self.dir is None:
            return
        path = self.dir / _SCORES_FILENAME
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return
            for slug, raw in data.items():
                sk = self._skills.get(str(slug))
                if sk is None or not isinstance(raw, dict):
                    continue
                sk.uses = int(raw.get("uses", sk.uses) or 0)
                sk.helpful = int(raw.get("helpful", sk.helpful) or 0)
                sk.quality_sum = float(raw.get("quality_sum", sk.quality_sum or 0.0))
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("skills.score_load_failed", error=str(exc))

    def _persist(self, skill: Skill) -> bool:
        if self.dir is None:
            return False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{skill.slug}.md").write_text(
                skill.to_markdown(),
                encoding="utf-8",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("skills.persist_failed", error=str(exc))
            return False

    def _persist_scores(self) -> bool:
        if self.dir is None:
            return False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            data = {
                slug: {
                    "uses": sk.uses,
                    "helpful": sk.helpful,
                    "quality_sum": sk.quality_sum or 0.0,
                    "score": sk.score,
                }
                for slug, sk in sorted(self._skills.items())
                if sk.uses or sk.helpful or (sk.quality_sum or 0.0)
            }
            atomic_write_text(self.dir / _SCORES_FILENAME, json.dumps(data, indent=2, sort_keys=True))
            return True
        except Exception as exc:  # noqa: BLE001
            if _log:
                _log.warning("skills.score_persist_failed", error=str(exc))
            return False

    # ---- CRUD ---------------------------------------------------------
    def add(
        self,
        title: str,
        body: str,
        *,
        stack: str = "generic",
        tags: list[str] | None = None,
        source: str = "manual",
        slug: str | None = None,
    ) -> Skill:
        slug = slug or _slugify(title)
        skill = Skill(
            slug=slug,
            title=title,
            body=body,
            tags=tags or [],
            stack=stack,
            source=source,
        )
        self._skills[slug] = skill
        self._persist(skill)
        return skill

    def get(self, slug: str) -> Skill | None:
        return self._skills.get(slug)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def applies_to(self, skill: Skill, stack: str, tags: list[str] | None = None) -> bool:
        """Whether a skill is safe to inject for this stack/tag context.

        Generic skills are not a free-for-all: they need either an explicit tag
        overlap (for design/front-end requests) or a universal build-quality tag.
        This keeps broad imported docs and wrong-domain repo patterns from
        crowding out stack-native guidance.
        """
        tagset = {t.lower() for t in (tags or [])}
        aliases = _stack_aliases(stack)
        sk_stack = (skill.stack or "").strip().lower()
        sk_tags = {t.lower() for t in skill.tags}
        if not _skill_tags_compatible(stack, sk_tags):
            return False
        if sk_stack in aliases or sk_stack == (stack or "").strip().lower():
            return True
        if sk_stack != "generic":
            return False
        return bool((tagset & sk_tags) or (_UNIVERSAL_GENERIC_TAGS & sk_tags))

    def import_directory(
        self, path: Path | str, *, stack: str = "generic", source: str = "imported"
    ) -> int:
        """Import every markdown file under ``path`` as ONE advisory skill (one
        per file) — e.g. a repo/dir of skill ``.md`` files like agent-skills.

        Recurses; for nested ``SKILL.md`` files the parent dir name is the slug
        basis. Idempotent by slug (re-import overwrites). Best-effort; never
        raises. Returns the number imported.
        """
        base = Path(path)
        if not base.is_dir():
            return 0
        count = 0
        for f in sorted(base.rglob("*.md")):
            # Best-effort per the docstring: a single unreadable file OR one with
            # malformed front matter (parse_skill -> ValueError on a bad numeric
            # field) must skip that file, not abort the whole import.
            try:
                text = f.read_text(encoding="utf-8")
                fallback = _slugify(f.parent.name if f.name.lower() == "skill.md" else f.stem)
                sk = parse_skill(text, fallback_slug=fallback)
            except (OSError, ValueError):
                continue
            if not sk.body.strip():
                continue
            if sk.source == "manual":
                sk.source = source
            self._skills[sk.slug] = sk
            self._persist(sk)
            count += 1
        return count

    # ---- injection ----------------------------------------------------
    def relevant(
        self, stack: str, tags: list[str] | None = None, limit: int = 5
    ) -> list[Skill]:
        """Most relevant skills for a stack/tags, ranked by score."""
        tagset = {t.lower() for t in (tags or [])}
        aliases = _stack_aliases(stack)

        def _match(sk: Skill) -> tuple[int, int, float]:
            if not self.applies_to(sk, stack, tags=tags):
                return (0, 0, sk.score)
            sk_stack = (sk.stack or "").strip().lower()
            stack_hit = 3 if (sk_stack in aliases or sk_stack == stack) else 1
            tag_hit = len(tagset & {t.lower() for t in sk.tags})
            # When the caller asks for design/front-end tags, tag fit matters most.
            # Otherwise stack-native skills should beat generic process advice.
            return ((tag_hit, stack_hit, sk.score) if tagset else (stack_hit, tag_hit, sk.score))

        cands = [s for s in self._skills.values() if self.applies_to(s, stack, tags=tags)]
        cands.sort(key=_match, reverse=True)
        return cands[:limit]

    def inject(
        self, stack: str, tags: list[str] | None = None, limit: int = 5
    ) -> str:
        """Render relevant skills as non-binding advice for a prompt."""
        skills = self.relevant(stack, tags=tags, limit=limit)
        if not skills:
            return ""
        blocks = "\n\n".join(s.as_advice() for s in skills)
        return (
            "Relevant skills (advisory — apply only where they fit the task):\n\n"
            f"{blocks}"
        )

    def relevant_for_stage(
        self,
        stack: str,
        stages: str | list[str],
        *,
        tags: list[str] | None = None,
        limit: int = 3,
    ) -> list[Skill]:
        """Relevant skills that explicitly target one of this stage's roles.

        Imported external agent catalogs are tagged as ``stage:<name>``. This
        stricter matcher prevents a stack-level role, such as a React frontend
        implementer, from being injected into every stage just because the stack
        matches.
        """
        raw_stages = [stages] if isinstance(stages, str) else list(stages or [])
        stage_tags = {
            f"stage:{str(s).strip().lower()}"
            for s in raw_stages
            if str(s).strip()
        }
        if not stage_tags:
            return []
        tagset = {t.lower() for t in (tags or [])}
        aliases = _stack_aliases(stack)

        def _match(sk: Skill) -> tuple[int, int, int, float]:
            sk_tags = {t.lower() for t in sk.tags}
            stage_hit = len(stage_tags & sk_tags)
            if stage_hit <= 0:
                return (0, 0, 0, sk.score)
            if not self.applies_to(sk, stack, tags=tags):
                return (0, 0, 0, sk.score)
            sk_stack = (sk.stack or "").strip().lower()
            stack_hit = 3 if (sk_stack in aliases or sk_stack == stack) else 1
            if not stack_hit:
                return (0, 0, 0, sk.score)
            tag_hit = len(tagset & sk_tags)
            return (stage_hit, stack_hit, tag_hit, sk.score)

        cands = [s for s in self._skills.values() if _match(s)[0] > 0]
        cands.sort(key=_match, reverse=True)
        return cands[:limit]

    def inject_for_stage(
        self,
        stack: str,
        stages: str | list[str],
        *,
        tags: list[str] | None = None,
        limit: int = 3,
    ) -> str:
        """Render stage-specific role guidance for a prompt."""
        skills = self.relevant_for_stage(stack, stages, tags=tags, limit=limit)
        if not skills:
            return ""
        blocks = "\n\n".join(s.as_advice() for s in skills)
        return (
            "Relevant stage role guidance (advisory — apply only to this stage):\n\n"
            f"{blocks}"
        )

    # ---- scoring (close the loop) -------------------------------------
    def record_use(
        self, slugs: list[str] | str, *, helpful: bool, quality: float | None = None
    ) -> None:
        """Grade skills after a build. ``quality`` (in [0,1], e.g. the build's
        final score / 100) gives a CONTINUOUS reward; when omitted it falls back
        to the binary helpful signal (1.0 or 0.0), preserving old behavior."""
        if isinstance(slugs, str):
            slugs = [slugs]
        q = quality if quality is not None else (1.0 if helpful else 0.0)
        q = max(0.0, min(1.0, q))
        for slug in slugs:
            sk = self._skills.get(slug)
            if sk is None:
                continue
            sk.uses += 1
            if helpful:
                sk.helpful += 1
            sk.quality_sum = (sk.quality_sum or 0.0) + q
        self._persist_scores()

    # ---- auto-promotion from build patterns ---------------------------
    def maybe_promote_pattern(
        self,
        pattern: Any,
        *,
        min_uses: int = PROMOTE_MIN_USES,
        min_rate: float = PROMOTE_MIN_RATE,
    ) -> Skill | None:
        """Promote a winning build pattern to a skill if it qualifies.

        ``pattern`` is duck-typed: needs ``uses``, ``win_rate`` (or ``score``),
        ``stack``, ``shape``, and ``fp``. Returns the new Skill or ``None``.
        """
        uses = int(getattr(pattern, "uses", 0))
        rate = getattr(pattern, "win_rate", None)
        if rate is None:
            ms = getattr(pattern, "mean_score", 0.0)
            rate = float(ms) / 100.0
        if uses < min_uses or float(rate) < min_rate:
            return None

        fp = getattr(pattern, "fp", _slugify(str(uses)))
        slug = f"pattern-{getattr(pattern, 'stack', 'generic')}-{fp}"
        if slug in self._skills:
            return self._skills[slug]

        shape = getattr(pattern, "shape", {})
        stack = getattr(pattern, "stack", "generic")
        body_lines = [
            f"This build shape wins for **{stack}** "
            f"({float(rate):.0%} over {uses} builds). Reuse its structure:",
            "",
        ]
        for k, v in (shape.items() if isinstance(shape, dict) else []):
            body_lines.append(f"- **{k}**: {v}")
        body = "\n".join(body_lines)
        return self.add(
            title=f"Winning {stack} build shape",
            body=body,
            stack=stack,
            tags=["build-pattern", stack],
            source="auto-promoted",
            slug=slug,
        )


# Starter skills so the library is useful from build #1 (auto-promotion only
# kicks in after many wins). Idempotent: existing slugs are left untouched.
_SEED_SKILLS = [
    ("Vite + React app shape", "react",
     "Deliver a runnable Vite+React app: package.json with dev/build/preview "
     "scripts and react/react-dom deps; index.html with <div id=\"root\">; "
     "src/main.jsx mounting <App/>; src/App.jsx as a DEFAULT export. Keep state "
     "local with hooks; no unused imports. Ensure `npm install && npm run build` "
     "succeeds.", ["react", "vite", "frontend"]),
    ("FastAPI service shape", "fastapi",
     "Deliver a runnable FastAPI service: app = FastAPI(); a GET /health route; "
     "pydantic models for request/response; uvicorn entrypoint; requirements.txt "
     "pinning fastapi+uvicorn. Provide a Dockerfile + .env.example so a stranger "
     "can `docker compose up`.", ["fastapi", "python", "backend"]),
    ("Python CLI shape", "python",
     "Deliver a runnable Python tool: a clear entrypoint (argparse or typer), "
     "src/__init__.py, pyproject.toml with name+version, and at least one real "
     "test under tests/. Code must import and `python -m` run without errors.",
     ["python", "cli"]),
    ("Static website shape", "static",
     "Deliver a complete static site, not a placeholder: index.html with real "
     "sections matching the brief, local CSS/JS assets, accessible headings, "
     "responsive layout, favicon/metadata, and at least one real interaction or "
     "form when the brief implies it. No external stock/CDN dependencies; use "
     "generated /assets paths when provided.", ["static", "html", "frontend", "web"]),
    ("Astro visual delivery", "astro",
     "Deliver an Astro site that is visibly complete, not merely buildable. Import "
     "the global stylesheet from the shared base layout, then run Astro check and "
     "a production build. Serve the built output and verify that rendered HTML links "
     "a CSS asset which responds successfully. Establish one specific brand/title and "
     "use real imagery as product context. Avoid generic icon-card grids and nested "
     "cards: use purposeful layout bands, editorial link rows, and cards only for "
     "repeated content. Before delivery, inspect desktop and mobile output when a "
     "browser is available.", ["astro", "frontend", "web", "design", "visual", "verification"]),
    ("Phaser playable game shape", "phaser",
     "Deliver a real Phaser game with src/sim.js pure game logic, src/main.js "
     "renderer, preload for generated sprites, keyboard/touch controls, win/lose "
     "state, scoring, restart/pause, and enough entities/levels to be playable. "
     "Never ship only a counter, flat canvas, or landing page.", ["phaser", "game", "gamedev"]),
    ("RAG app contract", "rag",
     "Deliver a runnable retrieval app: ingest endpoint, query/chat endpoints, "
     "persistent document store, chunking + retrieval, citations/source IDs in "
     "answers, malformed-input handling, README curl examples, and a tiny UI or "
     "API docs page. It must retrieve an ingested marker document in proof.",
     ["rag", "retrieval", "vector", "fastapi"]),
    ("MCP server contract", "mcp",
     "Deliver an MCP stdio server with explicit tool schemas, initialize/list/call "
     "support, read-only safety for inspection tools, structured errors for bad "
     "arguments, README integration instructions, and tests that exercise each "
     "tool without requiring network or secrets.", ["mcp", "tools", "stdio"]),
    ("Agent workflow contract", "workflow",
     "Deliver a real workflow runner: /trigger or CLI entrypoint, typed input, "
     "multi-step execution state, retries/timeouts, dry-run mode, webhook/email "
     "adapters behind env config, structured logs, and tests for success plus "
     "malformed input. Do not ship a single no-op endpoint.", ["workflow", "automation", "orchestration"]),
    ("Agent persona pack contract", "agent_pack",
     "Deliver a structured agent pack: catalog.json plus role markdown files with "
     "goals, tools, handoff rules, prompt templates, evaluation checklist, and "
     "example tasks. The pack is an artifact, so completeness is the content "
     "contract, not a web entrypoint.", ["agent_pack", "agents", "personas"]),
    ("Delivered != empty", "generic",
     "Every delivered project needs a real entrypoint, a README, and a manifest, "
     "and must pass install + build + boot. Never ship a config-puzzle or an "
     "empty scaffold; verify behavior, not vibes.", ["quality", "verification"]),
]


def seed_default_skills(library: SkillLibrary) -> int:
    """Add the built-in starter skills the library doesn't already have."""
    added = 0
    for title, stack, body, tags in _SEED_SKILLS:
        slug = _slugify(title)
        if library.get(slug) is None:
            library.add(title=title, body=body, stack=stack, tags=tags, source="seed", slug=slug)
            added += 1
    return added
