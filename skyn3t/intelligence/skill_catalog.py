"""Read-only skill browsing and provisional prompt-readiness assessments.

These ratings describe retained text, not measured effectiveness. They never
change selection scores, promotion, quarantine, or skill bodies.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.agents._common import _MAX_SKILL_ADVICE, _MAX_STAGE_ROLE_GUIDANCE
from skyn3t.atomic_io import atomic_write_text
from skyn3t.intelligence.skill_library import SkillLibrary, parse_skill

METHOD = "static-prompt-readiness-v1"
RATING_SCALE = {
    1: "Unsuitable for the current prompt: empty, oversized, or a weak build example.",
    2: "Needs distillation: reference excerpt, sparse instructions, or excessive size.",
    3: "Usable advisory text with scope, validation, or budget limitations.",
    4: "Compact scoped procedure with validation/failure signals; effectiveness unproven.",
    5: "Reserved for an independent content/effectiveness review; never assigned by this heuristic.",
}
LIMITS = [
    "Every retained body is scanned in full by deterministic rules, not independently reviewed by an LLM.",
    "Keyword/structure signals are provisional and can miss technical errors or misunderstand quoted examples.",
    "Short text is not automatically useful; long text is not automatically incorrect.",
    "Source integrity and activation are separate from content usefulness and observed effectiveness.",
    "Historical build scores are associations; selection receipts do not prove prompt exposure or contribution.",
    "No new upstream freshness checks, paid model calls, or runtime evaluations are performed.",
    "Categories aid browsing only; they do not change build-stack eligibility.",
]

_CATEGORY_TERMS = {
    "MCP & tool servers": {"mcp", "model context protocol"},
    "Frontend & UI": {"frontend", "ui", "web", "react", "reactjs", "vue", "svelte",
                      "sveltekit", "nextjs", "astro", "css", "html", "accessibility", "layout"},
    "Backend & APIs": {"backend", "api", "apis", "fastapi", "django", "flask",
                       "express", "node", "nodejs", "graphql", "rest"},
    "Desktop & mobile": {"desktop", "editor", "ide", "mobile", "native", "expo",
                         "react-native", "react_native", "swift", "swiftui", "tauri", "electron"},
    "AI & agents": {"ai", "agents", "agent", "agent_pack", "llm", "rag",
                    "retrieval", "embedding", "embeddings", "prompt", "machine-learning"},
    "Testing & debugging": {"testing", "test", "tests", "verification", "quality",
                            "debugging", "debug", "playwright", "pytest", "tdd"},
    "Security & access": {"security", "cybersecurity", "authentication", "authorization",
                          "auth", "oauth", "secrets", "pentesting", "cryptography"},
    "Delivery & operations": {"delivery", "packaging", "devops", "deploy", "deployment",
                              "docker", "kubernetes", "ci", "cloud", "observability", "monitoring"},
    "Data & storage": {"data", "database", "databases", "sql", "sqlite", "postgres",
                       "postgresql", "redis", "etl", "analytics", "visualization"},
    "Docs & planning": {"docs", "documentation", "readme", "planning", "architecture",
                        "specification", "spec", "adr", "git", "review"},
    "Business & content": {"business", "marketing", "seo", "content", "strategy",
                           "product-management", "copywriting", "writing"},
    "Games & graphics": {"game", "games", "phaser", "game-engine", "graphics",
                          "webgl", "threejs", "animation", "canvas"},
}
_HIDDEN_TOPICS = {
    "generic", "authored", "seed", "manual", "github-distilled", "github-curated",
    "build-distilled", "external-promoted", "external-candidate", "catalog-promoted",
    "catalog-candidate", "quarantine", "disabled", "hygiene:quarantine", "legacy-migrated",
}


def describe_skill(
    *, title: str, body: str, stack: str, tags: list[str], source: str, score: float = 0.5,
) -> dict[str, Any]:
    """Assess the complete body without granting it new eligibility or trust."""
    topics = {tag.removeprefix("stack:").strip().lower() for tag in tags}
    topics.add(stack.strip().lower())
    topics = {
        tag for tag in topics
        if tag and tag not in _HIDDEN_TOPICS
        and not tag.startswith(("stage:", "catalog:", "hub:", "hygiene:"))
    }
    title_words = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", title.lower()))
    # Split slug-style titles too, so desktop-editor-layout remains discoverable.
    title_words.update(re.findall(r"[a-z0-9]+", title.lower()))
    signals = topics | title_words
    categories = sorted(
        name for name, terms in _CATEGORY_TERMS.items()
        if terms & signals or ("model context protocol" in title.lower() and "mcp" in terms)
    ) or ["General engineering"]

    text = body.strip()
    lower = text.lower()
    advice_chars = len(f"### Skill: {title} (score {score:.0%})\n{text}")
    flags: list[str] = []
    reasons: list[str] = []
    steps = len(re.findall(r"^\s*(?:\d+[.)]|[-*])\s+\S", text, re.MULTILINE))
    scoped = bool(re.search(r"\b(?:apply|use|when|only|scope|before|preserve)\b", lower))
    validation = bool(re.search(r"\b(?:test|tests|verify|validate|assert|assertions|check|exercise)\b", lower))
    failure = bool(re.search(r"\b(?:error|errors|failure|failures|refuse|refused|avoid|rollback|negative)\b", lower))
    rating = 4 if steps >= 3 and scoped and validation and failure else 3
    if not text:
        rating = 1
        flags.append("empty_body")
        reasons.append("No retained instructions.")
    elif not validation:
        rating = min(rating, 3 if steps >= 3 else 2)
        flags.append("validation_not_explicit")
        reasons.append("No explicit test/verification signal found; inspect acceptance criteria.")
    if text and not scoped:
        rating = min(rating, 3)
        flags.append("scope_not_explicit")
        reasons.append("Applicability/scope is not explicit in the retained text.")
    if re.search(r"^Reusable patterns distilled from\b", text, re.MULTILINE):
        rating = min(rating, 2)
        flags.append("reference_excerpt")
        reasons.append("Repository-reference extraction, not a reviewed standalone procedure; distill before prioritizing.")
    winning_score = re.search(r"\bbuild scored\s+(\d+(?:\.\d+)?)\s*\(go\)", lower)
    if source == "build-distilled" and winning_score and float(winning_score[1]) < 70:
        rating = 1
        flags.append("weak_build_example")
        reasons.append(f"Advertises a {winning_score[1]}-point build as a winning example; GO alone is insufficient.")
    if re.search(r"from\s+mcp\.server\.fastmcp\s+import", text):
        flags.append("legacy_mcp_import")
        reasons.append("Contains the legacy FastMCP import; check compatibility with the selected SDK before reuse.")
    if advice_chars > _MAX_SKILL_ADVICE:
        rating = 1
        budget_fit = "over_global_budget"
        flags.append(budget_fit)
        reasons.append("This single rendered skill exceeds the entire global advice budget.")
    elif advice_chars > _MAX_SKILL_ADVICE // 2:
        rating = min(rating, 2)
        budget_fit = "over_half_budget"
        flags.append(budget_fit)
        reasons.append("Consumes over half the global advice budget before other skills or wrapper text.")
    elif advice_chars > _MAX_SKILL_ADVICE // 3:
        rating = min(rating, 3)
        budget_fit = "large"
        reasons.append("Consumes over a third of the global advice budget; use a focused excerpt.")
    else:
        budget_fit = "compact"
    if any(tag.lower().startswith("stage:") for tag in tags) and advice_chars > _MAX_STAGE_ROLE_GUIDANCE:
        rating = min(rating, 2)
        flags.append("over_stage_budget")
        reasons.append(
            f"This stage-role skill exceeds the entire {_MAX_STAGE_ROLE_GUIDANCE:,}-character stage guidance budget."
        )
    if not reasons:
        reasons.append("Compact instructions with scope and verification signals; technical correctness still needs review.")
    return {
        "categories": categories,
        "topics": sorted(topics),
        "content_audit": {
            "method": METHOD, "rating": rating, "effectiveness": "not_measured",
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "body_chars": len(body), "advice_chars": advice_chars,
            "global_budget_chars": _MAX_SKILL_ADVICE,
            "budget_percent": round(100 * advice_chars / _MAX_SKILL_ADVICE, 1),
            "budget_fit": budget_fit, "flags": flags, "reasons": reasons,
        },
    }


def export_audit(
    skills_dir: Path, output: Path, *, repository: Path, prior_review: Path | None = None,
) -> dict[str, Any]:
    """Retain one rating per live or registered archived skill; fail on missing evidence."""
    captured_at = datetime.now(UTC).isoformat()
    snapshot = [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(skills_dir.glob("*.md"))
    ]
    library = SkillLibrary(skills_dir)
    records = []
    for path, text in snapshot:
        parsed = parse_skill(text, fallback_slug=path.stem)
        if library.is_retired(parsed.slug):
            raise ValueError(f"Retired skill also exists in live inventory: {parsed.slug}")
        loaded = library.get(parsed.slug)
        if loaded is not None and loaded.body != parsed.body:
            raise ValueError(f"Skill changed during audit capture; retry: {parsed.slug}")
        records.append((loaded or parsed, path, "loaded" if loaded else "unloaded"))
    registry = skills_dir / ".skill_retirements.json"
    retirements = json.loads(registry.read_text())["skills"] if registry.exists() else {}
    for slug, entry in sorted(retirements.items()):
        path = (repository / entry["archive_path"]).resolve()
        if not path.is_relative_to(repository.resolve()):
            raise ValueError(f"Archive escapes repository: {slug}")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["file_sha256"]:
            raise ValueError(f"Archived file changed: {slug}")
        skill = parse_skill(raw.decode("utf-8"), fallback_slug=slug)
        if hashlib.sha256(skill.body.encode()).hexdigest() != entry["body_sha256"]:
            raise ValueError(f"Archived body changed: {slug}")
        records.append((skill, path, entry["disposition"]))

    prior = {
        row["slug"]: row for row in json.loads(prior_review.read_text())["skills"]
    } if prior_review is not None else {}
    rows = []
    duplicates: dict[str, list[str]] = defaultdict(list)
    for skill, path, status in records:
        metadata = describe_skill(
            title=skill.title, body=skill.body, stack=skill.stack,
            tags=skill.tags, source=skill.source, score=skill.score,
        )
        audit = metadata["content_audit"]
        row = {
            "slug": skill.slug, "title": skill.title, "status": status,
            "path": str(path.resolve().relative_to(repository.resolve())),
            "source": skill.source, "stack": skill.stack, **metadata,
            "historical_uses": skill.uses, "historical_build_association": skill.score,
            "eligible_for_mcp": status == "loaded" and library.applies_to(skill, "mcp"),
            "capability_status": library.capability_status(skill.slug) if status == "loaded" else None,
        }
        old = prior.get(skill.slug)
        if old and old.get("local_body_sha256") == audit["body_sha256"]:
            row["prior_editorial_review"] = {
                "date": "2026-09-08", "rating": old["rating"],
                "rationale": old["rationale"], "body_matches": True,
            }
        rows.append(row)
        duplicates[audit["body_sha256"]].append(skill.slug)
    slugs = [row["slug"] for row in rows]
    if len(slugs) != len(set(slugs)):
        raise ValueError("Duplicate skill identities in audit inventory")
    for row in rows:
        row["identical_body_skills"] = [
            slug for slug in duplicates[row["content_audit"]["body_sha256"]] if slug != row["slug"]
        ]
    report = {
        "schema_version": 1, "captured_at": captured_at,
        "generated_at": datetime.now(UTC).isoformat(),
        "method": METHOD, "rating_scale": RATING_SCALE, "limits": LIMITS,
        "summary": {
            "total": len(rows), "status": dict(Counter(row["status"] for row in rows)),
            "ratings": dict(Counter(row["content_audit"]["rating"] for row in rows)),
            "flags": dict(Counter(flag for row in rows for flag in row["content_audit"]["flags"])),
            "categories": dict(Counter(cat for row in rows for cat in row["categories"])),
            "prior_editorial_hash_matches": sum("prior_editorial_review" in row for row in rows),
            "duplicate_body_groups": sum(len(group) > 1 for group in duplicates.values()),
            "live_files_at_capture": len(snapshot),
            "new_live_files_after_capture": len(
                set(skills_dir.glob("*.md")) - {path for path, _ in snapshot}
            ),
        },
        "skills": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output.with_suffix(".json"), json.dumps(report, indent=2, ensure_ascii=True) + "\n")
    with output.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "slug", "title", "status", "categories", "topics", "prompt_readiness",
            "body_chars", "advice_chars", "budget_percent", "flags", "reasons",
            "eligible_for_mcp", "historical_uses", "historical_build_association",
            "prior_editorial_rating", "identical_body_skills", "body_sha256", "path",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            audit = row["content_audit"]
            cells = {
                **{key: row[key] for key in ("slug", "title", "status", "eligible_for_mcp",
                                             "historical_uses", "historical_build_association", "path")},
                **{key: audit[key] for key in ("body_chars", "advice_chars", "budget_percent", "body_sha256")},
                **{key: " | ".join(row[key]) for key in ("categories", "topics", "identical_body_skills")},
                "prompt_readiness": audit["rating"],
                "flags": " | ".join(audit["flags"]), "reasons": " | ".join(audit["reasons"]),
                "prior_editorial_rating": row.get("prior_editorial_review", {}).get("rating", ""),
            }
            # CSV is meant for spreadsheet use; imported titles remain untrusted.
            writer.writerow({
                key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
                for key, value in cells.items()
            })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path, default=Path("data/skills"))
    parser.add_argument("--output", type=Path, required=True, help="Output basename for JSON and CSV")
    parser.add_argument("--prior-review", type=Path, help="Optional prior editorial JSON; reuse only exact body hashes")
    args = parser.parse_args()
    report = export_audit(
        args.skills_dir, args.output, repository=Path.cwd(), prior_review=args.prior_review,
    )
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
