"""StageSpec dataclass + the default build pipeline definitions.

A :class:`StageSpec` maps a pipeline step to the canonical agent vocabulary:
``agent_type`` is the ``TaskRequest.type`` the studio submits and ``capability``
is the single required capability. The planner consumes these specs and the
StudioRunner submits one TaskRequest per spec.

Importing this module has zero side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Stages that are objective verifiers (not creative work). Used by the runner
# to know which stage outputs gate delivery.
VERIFIER_TYPES = frozenset(
    {
        "contract_verifier",
        "build_verifier",
        "boot_verifier",
        "consistency_reviewer",
        "integration_verifier",
    }
)

# Stages whose absence (no registered agent) is acceptable -> record skipped.
OPTIONAL_TYPES = frozenset(
    {
        "brainstorm",
        "research",
        "designer",
        "critic",
        "writer",
        "code_improver",
        "consistency_reviewer",
        "integration_verifier",
    }
)


@dataclass(slots=True)
class StageSpec:
    """One step in a build pipeline.

    ``agent_type`` is submitted as ``TaskRequest.type`` and ``capability`` is the
    lone entry in ``capabilities_required``. ``gated`` marks an approval gate;
    ``optional`` means a missing agent is skipped (not failed).
    """

    name: str
    agent_type: str
    capability: str
    gated: bool = False
    optional: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_verifier(self) -> bool:
        return self.agent_type in VERIFIER_TYPES

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "agent_type": self.agent_type,
            "capability": self.capability,
            "gated": self.gated,
            "optional": self.optional,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StageSpec":
        return cls(
            name=d["name"],
            agent_type=d["agent_type"],
            capability=d["capability"],
            gated=bool(d.get("gated", False)),
            optional=bool(d.get("optional", False)),
            extra=dict(d.get("extra", {})),
        )


# (name, agent_type, capability) triples drawn from the canonical vocabulary.
_DEFAULT_SEQUENCE: tuple[tuple[str, str, str], ...] = (
    ("brainstorm", "brainstorm", "brainstorm"),
    ("research", "research", "research"),
    ("architect", "architect", "architecture"),
    ("design", "designer", "design"),
    ("code", "code", "codegen"),
    ("verify_contract", "contract_verifier", "verify_contract"),
    ("verify_build", "build_verifier", "verify_build"),
    ("verify_boot", "boot_verifier", "verify_boot"),
    ("critic", "critic", "critique"),
    ("review", "reviewer", "review"),
    ("package", "packaging", "package"),
)


def default_pipeline() -> list[StageSpec]:
    """Return a fresh copy of the canonical brief->app pipeline."""
    specs: list[StageSpec] = []
    for name, agent_type, capability in _DEFAULT_SEQUENCE:
        specs.append(
            StageSpec(
                name=name,
                agent_type=agent_type,
                capability=capability,
                optional=agent_type in OPTIONAL_TYPES,
            )
        )
    return specs


def test_author_spec() -> StageSpec:
    """The test-first stage (P1). Authors tests before the code stage."""
    return StageSpec(
        name="test_author",
        agent_type="test_author",
        capability="test_author",
        optional=True,
        extra={"test_first": True},
    )
