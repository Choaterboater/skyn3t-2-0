from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class AuditFinding:
    severity: str
    category: str
    claim: str
    evidence: list[str] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AuditSection:
    title: str
    score: float
    findings: list[AuditFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "score": self.score,
            "findings": [f.to_dict() for f in self.findings],
            "notes": list(self.notes),
            "metrics": dict(self.metrics),
        }


@dataclass(slots=True)
class AuditReport:
    repo_state: dict[str, Any]
    sections: list[AuditSection]
    overall_rating: float
    prioritized_handoff: list[AuditFinding]
    generated_at: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_state": dict(self.repo_state),
            "sections": [s.to_dict() for s in self.sections],
            "overall_rating": self.overall_rating,
            "prioritized_handoff": [f.to_dict() for f in self.prioritized_handoff],
            "generated_at": self.generated_at,
            "summary": self.summary,
        }
