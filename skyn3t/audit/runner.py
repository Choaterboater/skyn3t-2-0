from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.audit.agents import (
    AuditContext,
    ComparatorAgent,
    DebugAuditAgent,
    ExplorerAgent,
    GapAnalystAgent,
    ProductRaterAgent,
    RepoReviewAgent,
)
from skyn3t.audit.models import AuditFinding, AuditReport


def run_product_audit(
    repo_root: str | Path,
    *,
    include_tests: bool = True,
    max_findings: int = 20,
    use_llm: bool = False,
    llm: Any | None = None,
) -> AuditReport:
    """Audit SkyN3t itself as an app factory and return a durable report model."""

    root = Path(repo_root).resolve()
    ctx = AuditContext(
        repo_root=root,
        include_tests=include_tests,
        max_findings=max(1, int(max_findings)),
        use_llm=use_llm,
        llm=llm,
    )
    agents = [
        RepoReviewAgent(),
        DebugAuditAgent(),
        ProductRaterAgent(),
        GapAnalystAgent(),
        ExplorerAgent(),
        ComparatorAgent(),
    ]
    sections = [agent.run(ctx) for agent in agents]
    overall = round(sum(s.score for s in sections) / len(sections), 2) if sections else 0.0
    handoff = _prioritize(sections, limit=10)
    summary = (
        "SkyN3t was audited as an app factory using deterministic repo-local analyzers. "
        "The report focuses on product capability, proof/debug maturity, roadmap gaps, "
        "comparison rubric coverage, and next-engineer handoff steps."
    )
    return AuditReport(
        repo_state=_repo_state(root, include_tests=include_tests, use_llm=use_llm and llm is not None),
        sections=sections,
        overall_rating=overall,
        prioritized_handoff=handoff,
        generated_at=datetime.now(UTC).isoformat(),
        summary=summary,
    )


def _prioritize(sections, *, limit: int) -> list[AuditFinding]:
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    seen: set[tuple[str, str]] = set()
    findings: list[AuditFinding] = []
    for section in sections:
        for finding in section.findings:
            key = (finding.category, finding.claim)
            if key in seen:
                continue
            seen.add(key)
            findings.append(finding)
    return sorted(findings, key=lambda f: (rank.get(f.severity, 9), f.category, f.claim))[:limit]


def _repo_state(root: Path, *, include_tests: bool, use_llm: bool) -> dict[str, Any]:
    return {
        "repo_root": root.name,
        "branch": _git(root, "branch", "--show-current"),
        "commit": _git(root, "rev-parse", "--short", "HEAD"),
        "dirty": bool(_git(root, "status", "--short")),
        "include_tests": include_tests,
        "llm_assisted": use_llm,
    }


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except Exception:  # noqa: BLE001 - repo metadata is best-effort
        return ""
    return proc.stdout.strip()
