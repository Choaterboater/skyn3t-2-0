"""Safe parser for external agent/role catalogs.

This module normalizes third-party Markdown/TOML agent profiles into compact
SkyN3t advisory entries. It deliberately does not execute catalog scripts or
install global Codex agents; import is a read-only filesystem operation.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.intelligence.skill_library import SkillLibrary, _slugify


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_MAX_BODY_CHARS = 2400


@dataclass(slots=True)
class CatalogEntry:
    """One normalized external role/profile."""

    id: str
    title: str
    description: str
    body: str
    source_path: str
    source_kind: str = "markdown"
    stages: list[str] = field(default_factory=list)
    stacks: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    risk: str = "low"

    def compact_body(self) -> str:
        parts = [
            f"External role: {self.title}",
            f"Description: {self.description}" if self.description else "",
            f"Recommended stages: {', '.join(self.stages) or 'generic'}",
            f"Recommended stacks: {', '.join(self.stacks) or 'generic'}",
            "",
            _compact_markdown(self.body, limit=_MAX_BODY_CHARS),
        ]
        return "\n".join(p for p in parts if p).strip()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FM_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, match.group(2)


def _compact_markdown(text: str, *, limit: int) -> str:
    """Keep high-signal headings/bullets while capping prompt footprint."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if lines and lines[-1]:
                lines.append("")
            continue
        if stripped.startswith(("```", "digraph ")):
            continue
        if stripped.startswith(("#", "-", "*", "1.", "2.", "3.")) or len(stripped) <= 180:
            lines.append(stripped)
        if sum(len(x) + 1 for x in lines) >= limit:
            break
    compact = "\n".join(lines).strip()
    return compact[:limit].rstrip()


def _wordset(*values: str) -> set[str]:
    text = " ".join(values).lower()
    return set(re.findall(r"[a-z0-9]+", text))


def _classify(title: str, description: str, path: Path) -> tuple[list[str], list[str], list[str], str]:
    content_words = _wordset(title, description)
    path_words = _wordset(str(path))
    words = content_words | path_words
    stages: set[str] = set()
    stacks: set[str] = set()
    tags: set[str] = set()
    risk = "low"

    def has(*terms: str) -> bool:
        return bool(words & set(terms))

    def has_content(*terms: str) -> bool:
        return bool(content_words & set(terms))

    is_game = has("game", "level", "unity", "godot", "unreal", "minecraft", "sprite")
    path_text = str(path).lower()

    if (
        not is_game
        and (has_content("ui", "ux", "designer", "brand", "visual", "interface")
             or path_text.startswith("design/"))
    ):
        stages.add("designer")
        tags.update({"design", "visual"})
        stacks.update({"react", "static"})
    if has("frontend", "react", "next", "component", "mobile", "vite"):
        stages.add("code")
        stacks.add("react")
        tags.add("frontend")
    if not is_game and has("backend", "api", "database", "fastapi", "node", "architecture", "architect"):
        stages.update({"architect", "code"})
        stacks.update({"fastapi", "node"})
        tags.add("backend")
    if is_game:
        stages.update({"designer", "code", "qa_playtest"})
        stacks.add("phaser")
        tags.add("game")
    if has("test", "testing", "qa", "reality", "evidence", "accessibility", "eval", "playtest"):
        stages.update({"test_author", "verify_build", "review"})
        tags.add("testing")
    if has("reviewer", "review", "auditor", "security", "appsec", "threat"):
        stages.update({"critic", "review"})
        tags.update({"security", "review"})
        risk = "medium"
    if has("seo", "marketing", "content", "landingpage", "landing"):
        stages.update({"writer", "seo_check"})
        stacks.update({"static", "nextjs", "astro"})
        tags.add("seo")
    if has("deploy", "deployment", "devops", "sre", "cloud", "cicd"):
        stages.update({"package", "deploy"})
        tags.add("deployment")
    # Do not classify every file under an `agents/` directory as an agent-pack
    # builder. Require the role itself, or a specific catalog folder, to discuss
    # agent/plugin/skill orchestration.
    agent_pack_path = any(
        marker in path_text
        for marker in ("agent-pack", "agent_pack", "role-pack", "role_pack")
    )
    if has_content("agent", "agents", "orchestrator", "multi", "plugin", "codex", "skill") or agent_pack_path:
        stages.update({"architect", "code", "review"})
        stacks.add("agent_pack")
        tags.add("agents")
        risk = "medium"
    if has("mcp", "tool", "tools"):
        stacks.add("mcp")
        tags.add("tools")
    if has("rag", "retrieval", "memory", "knowledge"):
        stacks.add("rag")
        tags.add("retrieval")
    if has("workflow", "automation", "pipeline", "orchestration"):
        stacks.add("workflow")
        tags.add("workflow")

    if not stages:
        stages.add("architect")
    if not stacks:
        stacks.add("generic")
    if has("bash", "script", "hooks", "hook", "command"):
        risk = "medium"

    return sorted(stages), sorted(stacks), sorted(tags), risk


def _entry_from_markdown(path: Path, root: Path) -> CatalogEntry | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    title = meta.get("name") or meta.get("title") or path.stem
    description = meta.get("description", "")
    if not body.strip():
        return None
    stages, stacks, tags, risk = _classify(title, description, path.relative_to(root))
    rel = str(path.relative_to(root))
    return CatalogEntry(
        id=_slugify(f"{root.name}-{rel}"),
        title=title,
        description=description,
        body=body.strip(),
        source_path=rel,
        stages=stages,
        stacks=stacks,
        tags=tags,
        risk=risk,
    )


def _entry_from_toml(path: Path, root: Path) -> CatalogEntry | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    title = str(data.get("name") or path.stem)
    description = str(data.get("description") or "")
    body = str(data.get("developer_instructions") or "")
    if not body.strip():
        return None
    stages, stacks, tags, risk = _classify(title, description, path.relative_to(root))
    rel = str(path.relative_to(root))
    return CatalogEntry(
        id=_slugify(f"{root.name}-{rel}"),
        title=title,
        description=description,
        body=body.strip(),
        source_path=rel,
        source_kind="codex_toml",
        stages=stages,
        stacks=stacks,
        tags=tags,
        risk=risk,
    )


def discover_catalog_entries(path: Path | str, *, limit: int | None = None) -> list[CatalogEntry]:
    """Return normalized external catalog entries from Markdown and Codex TOML files."""
    root = Path(path)
    if not root.is_dir():
        return []
    entries: list[CatalogEntry] = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        if any(part in {".git", "node_modules", "__pycache__"} for part in file_path.parts):
            continue
        entry: CatalogEntry | None = None
        if file_path.suffix.lower() == ".md":
            entry = _entry_from_markdown(file_path, root)
        elif file_path.suffix.lower() == ".toml":
            entry = _entry_from_toml(file_path, root)
        if entry is None:
            continue
        entries.append(entry)
        if limit is not None and len(entries) >= limit:
            break
    return entries


def import_catalog_as_skills(
    path: Path | str,
    library: SkillLibrary,
    *,
    limit: int | None = None,
    source: str = "agent_catalog",
) -> int:
    """Import normalized catalog roles as compact advisory skills."""
    count = 0
    for entry in discover_catalog_entries(path, limit=limit):
        stack = entry.stacks[0] if entry.stacks else "generic"
        tags = sorted(set(entry.tags + [f"stage:{s}" for s in entry.stages] + [entry.source_kind]))
        library.add(
            entry.title,
            entry.compact_body(),
            stack=stack,
            tags=tags,
            source=source,
            slug=entry.id,
        )
        count += 1
    return count


def catalog_summary(entries: list[CatalogEntry]) -> dict[str, Any]:
    """Small serializable summary for UI/API use."""
    by_stack: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for entry in entries:
        for stack in entry.stacks:
            by_stack[stack] = by_stack.get(stack, 0) + 1
        for stage in entry.stages:
            by_stage[stage] = by_stage.get(stage, 0) + 1
        by_risk[entry.risk] = by_risk.get(entry.risk, 0) + 1
    return {
        "entries": len(entries),
        "by_stack": dict(sorted(by_stack.items())),
        "by_stage": dict(sorted(by_stage.items())),
        "by_risk": dict(sorted(by_risk.items())),
    }
