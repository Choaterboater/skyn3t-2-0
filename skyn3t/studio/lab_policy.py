"""Personal-lab autonomy policy.

Lab mode removes repetitive build-time approval and budget friction while
deliberately retaining proof, test, and delivery gates. Actions that cross the
local project boundary remain explicit even in a personal lab.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_ALWAYS_GATED_ACTIONS = frozenset(
    {
        "remote_deploy",
        "write_secret",
        "delete_host_data",
        "publish_release",
        "merge_protected_branch",
    }
)


@dataclass(frozen=True, slots=True)
class LabAutonomyPolicy:
    enabled: bool = False
    verification_required: bool = True

    @classmethod
    def from_settings(cls, settings: Any) -> LabAutonomyPolicy:
        return cls(enabled=bool(getattr(settings, "lab_autonomy", False)))

    @property
    def approval_gates_enabled(self) -> bool:
        return not self.enabled

    @property
    def budget_guards_enabled(self) -> bool:
        return not self.enabled

    def approval_required(self, action: str) -> bool:
        normalized = str(action or "").strip().lower()
        if normalized in _ALWAYS_GATED_ACTIONS:
            return True
        return not self.enabled
