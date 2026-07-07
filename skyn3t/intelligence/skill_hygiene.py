"""Skill taxonomy hygiene for the local advisory skill library.

The live skill library can accumulate imported README/reference material. This
module keeps that material available on disk for inspection while preventing it
from being injected as default build advice.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text
from skyn3t.intelligence.skill_library import Skill, parse_skill

_QUARANTINE_TAG = "hygiene:quarantine"
_INFERENCE_BODY_CHARS = 2400
_QUARANTINE_TAGS = frozenset({_QUARANTINE_TAG, "quarantine", "disabled"})
_DEFAULT_SOURCE_TAGS = {
    "github-distilled": {"github-distilled", "reference"},
    "github-curated": {"github-curated"},
    "agent-skills": {"agent-skills"},
    "build-distilled": {"build-distilled"},
    "auto-promoted": {"auto-promoted"},
    "authored": {"authored"},
    "seed": {"seed"},
}
_UNIVERSAL_TAGS = {
    "quality",
    "verification",
    "testing",
    "ci",
    "delivery",
    "security",
    "smoke-test",
    "healthcheck",
    "reproducibility",
    "dependencies",
    "secrets",
    "docs",
    "documentation",
    "packaging",
    "observability",
    "performance",
    "debugging",
}
_GENERATED_TAGS = _UNIVERSAL_TAGS | {
    _QUARANTINE_TAG,
    "agent-skills",
    "agent_pack",
    "agents",
    "api",
    "astro",
    "automation",
    "backend",
    "build-distilled",
    "authored",
    "auto-promoted",
    "cli",
    "desktop",
    "design",
    "expo",
    "fastapi",
    "frontend",
    "game",
    "github-curated",
    "github-distilled",
    "html",
    "mcp",
    "mobile",
    "nextjs",
    "node",
    "orchestration",
    "phaser",
    "python",
    "rag",
    "react",
    "react-native",
    "reference",
    "remix",
    "retrieval",
    "seed",
    "static",
    "tools",
    "ui",
    "uncategorized",
    "web",
    "workflow",
}


@dataclass(frozen=True)
class HygieneDecision:
    slug: str
    action: str
    stack: str
    tags: list[str]
    reason: str
    source: str


def _contains(text: str, *needles: str) -> bool:
    for needle in needles:
        pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def _normal_tags(tags: list[str] | set[str]) -> set[str]:
    return {str(tag).strip().lower() for tag in tags if str(tag).strip()}


def infer_tags(skill: Skill) -> set[str]:
    """Infer conservative taxonomy tags from title/body/stack/source."""
    body_head = (skill.body or "")[:_INFERENCE_BODY_CHARS]
    text = f"{skill.title}\n{body_head}\n{skill.stack}".lower()
    tags = {tag for tag in _normal_tags(skill.tags) if tag not in _GENERATED_TAGS}
    stack = (skill.stack or "generic").strip().lower()
    if stack and stack != "generic":
        tags.add(stack)
    tags.update(_DEFAULT_SOURCE_TAGS.get((skill.source or "").strip().lower(), set()))

    if _contains(
        text,
        "frontend",
        "browser",
        "responsive",
        "semantic html",
        "accessibility",
        "a11y",
        "tailwind",
        "css",
        "component",
        "user interface",
    ):
        tags.update({"frontend", "design", "ui", "web"})
    if _contains(text, "react", "vite", "jsx", "tsx"):
        tags.update({"frontend", "react", "ui", "web"})
    if _contains(text, "next.js", "nextjs"):
        tags.update({"frontend", "nextjs", "ui", "web"})
    if _contains(text, "astro"):
        tags.update({"frontend", "astro", "static", "web"})
    if _contains(text, "remix"):
        tags.update({"frontend", "remix", "ui", "web"})
    if _contains(text, "static site", "static html", "plain html"):
        tags.update({"static", "html", "web"})

    if _contains(text, "fastapi", "flask", "django", "api endpoint", "openapi"):
        tags.update({"api", "backend", "python"})
    if _contains(text, "python", "pytest", "pip", "virtualenv"):
        tags.add("python")
    if _contains(text, "node", "express", "npm", "package.json"):
        tags.add("node")
    if _contains(text, "cli", "command line", "argparse", "click command"):
        tags.add("cli")

    if _contains(text, "phaser", "sprite", "game loop", "arcade", "canvas game"):
        tags.update({"game", "phaser"})
    if _contains(text, "react native", "expo", "mobile app"):
        tags.update({"mobile", "react-native", "expo"})
    if _contains(text, "tauri", "desktop app", "swiftui", "menubar", "menu bar"):
        tags.add("desktop")

    if _contains(text, "rag", "retrieval", "vector", "embedding", "knowledge base"):
        tags.update({"rag", "retrieval"})
    if _contains(text, "mcp", "tool schema", "model context protocol"):
        tags.update({"mcp", "tools"})
    if _contains(text, "workflow", "orchestration", "pipeline", "automation"):
        tags.update({"workflow", "automation", "orchestration"})
    if _contains(text, "agent", "persona", "role pack", "prompt pack"):
        tags.update({"agent_pack", "agents"})

    if _contains(text, "test", "pytest", "vitest", "playwright", "smoke"):
        tags.update({"testing", "verification"})
    if _contains(text, "ci", "github actions"):
        tags.add("ci")
    if _contains(text, "security", "secret", "auth", "token", "credential"):
        tags.update({"security", "secrets"})
    if _contains(text, "performance", "perf", "latency", "bundle size"):
        tags.add("performance")
    if _contains(text, "log", "metric", "trace", "observability"):
        tags.add("observability")
    if _contains(text, "debug", "diagnose", "root cause"):
        tags.add("debugging")
    if _contains(text, "docs", "documentation", "readme"):
        tags.update({"docs", "documentation"})
    if _contains(text, "docker", "container", "deploy", "release", "package"):
        tags.update({"delivery", "packaging"})

    return tags


def classify_skill(skill: Skill) -> HygieneDecision:
    """Return the hygiene action for a parsed skill."""
    source = (skill.source or "manual").strip().lower()
    current_tags = _normal_tags(skill.tags)
    inferred_tags = infer_tags(skill)
    stack = (skill.stack or "generic").strip().lower() or "generic"

    if current_tags & _QUARANTINE_TAGS:
        tags = sorted(inferred_tags | {_QUARANTINE_TAG})
        return HygieneDecision(
            slug=skill.slug,
            action="quarantine",
            stack=stack,
            tags=tags,
            reason="already tagged as quarantined",
            source=source,
        )

    if source == "github-distilled":
        tags = sorted(inferred_tags | {"github-distilled", "reference", _QUARANTINE_TAG})
        return HygieneDecision(
            slug=skill.slug,
            action="quarantine",
            stack=stack,
            tags=tags,
            reason="github-distilled README/reference skill is too noisy for default injection",
            source=source,
        )

    specific_tags = inferred_tags - _DEFAULT_SOURCE_TAGS.get(source, set())
    if stack == "generic" and not current_tags and not specific_tags:
        return HygieneDecision(
            slug=skill.slug,
            action="quarantine",
            stack=stack,
            tags=sorted(inferred_tags | {"uncategorized", _QUARANTINE_TAG}),
            reason="generic imported skill has no actionable taxonomy tags",
            source=source,
        )

    if source == "agent-skills" and stack == "generic":
        useful = specific_tags & (_UNIVERSAL_TAGS | {"frontend", "design", "ui", "web"})
        useful |= specific_tags & {
            "api",
            "backend",
            "python",
            "node",
            "cli",
            "game",
            "phaser",
            "rag",
            "mcp",
            "workflow",
            "automation",
            "agent_pack",
            "agents",
            "desktop",
            "mobile",
        }
        if not useful:
            return HygieneDecision(
                slug=skill.slug,
                action="quarantine",
                stack=stack,
                tags=sorted(inferred_tags | {"uncategorized", _QUARANTINE_TAG}),
                reason="generic agent skill lacks a factory-relevant category",
                source=source,
            )

    next_tags = sorted(inferred_tags)
    if next_tags != sorted(current_tags):
        return HygieneDecision(
            slug=skill.slug,
            action="retag",
            stack=stack,
            tags=next_tags,
            reason="taxonomy tags inferred from skill content",
            source=source,
        )
    return HygieneDecision(
        slug=skill.slug,
        action="keep",
        stack=stack,
        tags=next_tags,
        reason="taxonomy already looks usable",
        source=source,
    )


def _apply_decision(skill: Skill, decision: HygieneDecision) -> bool:
    old_tags = list(skill.tags)
    skill.tags = decision.tags
    return old_tags != skill.tags


def _report_entry(skill: Skill, path: Path, decision: HygieneDecision) -> dict[str, Any]:
    return {
        "slug": skill.slug,
        "title": skill.title,
        "file": str(path),
        "source": decision.source,
        "stack": decision.stack,
        "action": decision.action,
        "reason": decision.reason,
        "tags": decision.tags,
    }


def hygiene_directory(
    skills_dir: Path | str,
    *,
    apply: bool = False,
    report_path: Path | str | None = None,
) -> dict[str, Any]:
    """Review every markdown skill in a directory and optionally rewrite tags."""
    root = Path(skills_dir)
    entries: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    rewritten = 0

    for path in sorted(root.glob("*.md")):
        skill = parse_skill(path.read_text(encoding="utf-8"), fallback_slug=path.stem)
        decision = classify_skill(skill)
        counts[decision.action] += 1
        source_counts[decision.source] += 1
        entries.append(_report_entry(skill, path, decision))
        if apply and decision.action in {"retag", "quarantine"}:
            changed = _apply_decision(skill, decision)
            if changed:
                atomic_write_text(path, skill.to_markdown())
                rewritten += 1

    report = {
        "skills_dir": str(root),
        "total": len(entries),
        "rewritten": rewritten,
        "counts": dict(sorted(counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "entries": entries,
    }
    if report_path is not None:
        atomic_write_text(Path(report_path), json.dumps(report, indent=2, sort_keys=True))
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review and retag SkyN3t skill markdown files.")
    parser.add_argument("skills_dir", type=Path, help="Directory containing *.md skill files")
    parser.add_argument("--apply", action="store_true", help="Rewrite skill tags in place")
    parser.add_argument("--report", type=Path, help="Write a JSON hygiene report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = hygiene_directory(args.skills_dir, apply=args.apply, report_path=args.report)
    print(json.dumps({k: report[k] for k in ("total", "rewritten", "counts", "source_counts")}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
