"""The shared verdict shape for deterministic end-of-build gates.

Extracted when the THIRD gate (cli_check) arrived, exactly as the rag_check/
workflow_check comments promised. The shape is gate-agnostic:

* ``ok`` is a property: True when the gate RAN and found no issues.
* ``skipped`` marks a degrade-open (the gate could not run): ``ok=False`` yet
  ``gaps()`` is EMPTY — a could-not-run gate must never false-flag a build.
* ``issues`` are authored to be directly actionable, so they ARE the gaps the
  repair loop consumes.

``rag_check`` re-exports this as ``RagVerdict`` (and ``workflow_check`` as
``WorkflowVerdict``) so existing imports, tests, and manifests are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GateVerdict:
    skipped: bool = False
    issues: list[str] = field(default_factory=list)
    checked: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return (not self.skipped) and (not self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "issues": list(self.issues),
            "checked": dict(self.checked),
            "reason": self.reason,
            "gaps": self.gaps(),
        }

    def gaps(self) -> list[str]:
        """Actionable repair strings; empty when ok or skipped (a could-not-run
        gate must never false-flag)."""
        if self.skipped or self.ok:
            return []
        return list(self.issues)
