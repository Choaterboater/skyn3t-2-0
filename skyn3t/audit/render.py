from __future__ import annotations

import json
from pathlib import Path

from skyn3t.atomic_io import atomic_write_text
from skyn3t.audit.models import AuditFinding, AuditReport


def render_markdown(report: AuditReport) -> str:
    lines: list[str] = [
        "# Skyn3t Product Audit",
        "",
        f"Generated: {report.generated_at}",
        f"Overall rating: {report.overall_rating:.1f}/100",
        "",
        "## Executive Summary",
        "",
        report.summary,
        "",
        "Repo state:",
        f"- Root: `{report.repo_state.get('repo_root', '')}`",
        f"- Branch: `{report.repo_state.get('branch', '')}`",
        f"- Commit: `{report.repo_state.get('commit', '')}`",
        f"- Dirty: `{report.repo_state.get('dirty', False)}`",
        f"- Includes tests: `{report.repo_state.get('include_tests', False)}`",
        f"- LLM-assisted: `{report.repo_state.get('llm_assisted', False)}`",
        "",
        "## Product Rating Table",
        "",
        "| Section | Score | Findings |",
        "| --- | ---: | ---: |",
    ]
    for section in report.sections:
        lines.append(f"| {section.title} | {section.score:.1f} | {len(section.findings)} |")

    for section in report.sections:
        lines.extend(["", f"## {section.title}", "", f"Score: {section.score:.1f}/100", ""])
        if section.notes:
            lines.append("Notes:")
            for note in section.notes:
                lines.append(f"- {note}")
            lines.append("")
        if section.metrics:
            lines.append("Metrics:")
            for key, value in section.metrics.items():
                lines.append(f"- `{key}`: {value}")
            lines.append("")
        if section.findings:
            lines.append("Findings:")
            for finding in section.findings:
                lines.extend(_finding_lines(finding))
                lines.append("")
        else:
            lines.append("Findings: none from this deterministic pass.")

    lines.extend(["", "## Top 10 Prioritized Next Steps", ""])
    for i, finding in enumerate(report.prioritized_handoff, start=1):
        lines.append(f"{i}. [{finding.severity}] {finding.claim}")
        if finding.recommendation:
            lines.append(f"   Recommendation: {finding.recommendation}")
        if finding.evidence:
            lines.append(f"   Evidence: {', '.join(f'`{e}`' for e in finding.evidence[:5])}")

    lines.extend([
        "",
        "## Detailed Handoff",
        "",
        "Use this report as the next engineer/agent starting point. Start with P1/P2 findings, "
        "open the evidence paths before changing code, and rerun `skyn3t audit product` after "
        "major factory changes so the handoff stays current.",
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_audit_report(report: AuditReport, out: str | Path, json_out: str | Path) -> tuple[Path, Path]:
    md_path = Path(out)
    json_path = Path(json_out)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(md_path, render_markdown(report))
    atomic_write_text(json_path, json.dumps(report.to_dict(), indent=2))
    return md_path, json_path


def _finding_lines(finding: AuditFinding) -> list[str]:
    lines = [f"- [{finding.severity}] **{finding.category}**: {finding.claim}"]
    if finding.evidence:
        lines.append(f"  Evidence: {', '.join(f'`{e}`' for e in finding.evidence[:8])}")
    if finding.recommendation:
        lines.append(f"  Recommendation: {finding.recommendation}")
    return lines
