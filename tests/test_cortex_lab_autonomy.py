"""Focused Lab Autonomy triage coverage for Cortex."""

from __future__ import annotations

from pathlib import Path

import pytest

import skyn3t.agents.github_fetch as github_fetch
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.cortex.bootstrap import Cortex
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import Proposal, ProposalStatus, ProposalType
from skyn3t.intelligence.skill_library import SkillLibrary


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "approval_gates": True,
        "cortex_auto_approve_safe": False,
        "lab_autonomy": False,
        "data_dir": tmp_path / "data",
        "logs_dir": tmp_path / "logs",
    }
    values.update(overrides)
    return Settings(**values)


def _decision_statuses(bus: EventBus) -> list[str]:
    return [
        str(event.payload.get("status"))
        for event in bus.history(event_type=EventType.PROPOSAL_DECIDED)
    ]


def _repo_scout_ingest(payload: dict[str, object]) -> Proposal:
    return Proposal(
        type=ProposalType.INGEST,
        title="ingest RepoScout research",
        source="repo_scout",
        payload=payload,
        confidence=0.99,
        safe=False,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "https://github.com/pallets/flask"},
        {"repo": "pallets/flask"},
        {"repo": "octo-org/repo_name.v2"},
    ],
)
def test_repo_scout_identity_accepts_only_safe_canonical_forms(
    payload: dict[str, object],
) -> None:
    assert Cortex._is_repo_scout_github_research(_repo_scout_ingest(payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "http://github.com/pallets/flask"},
        {"url": "HTTPS://github.com/pallets/flask"},
        {"url": "https://www.github.com/pallets/flask"},
        {"url": "https://github.com:443/pallets/flask"},
        {"url": "https://token@github.com/pallets/flask"},
        {"url": "https://github.com/pallets/flask/"},
        {"url": "https://github.com/pallets/flask?tab=readme"},
        {"url": "https://github.com/pallets/flask#readme"},
        {"url": "https://github.com/pallets/flask/tree/main"},
        {"url": "https://github.com/pallets/../flask"},
        {"url": "https://github.com/pallets/%2e%2e"},
        {"url": "https://github.com/pallets%2fflask"},
        {"url": "https://github.com/pallets/flask.git"},
        {"url": " https://github.com/pallets/flask"},
        {"url": 123, "repo": "pallets/flask"},
        {"repo": "pallets/flask/README.md"},
        {"repo": "pallets/../flask"},
        {"repo": "pallets/%2e%2e"},
        {"repo": "pallets/flask?tab=readme"},
        {"repo": "pallets/flask.git"},
        {"repo": "pallets\\flask"},
        {"repo": "pallets /flask"},
        {"repo": "pallets/flask "},
        {"repo": "-pallets/flask"},
        {"repo": "pallets//flask"},
    ],
)
def test_repo_scout_identity_rejects_url_and_identifier_variants(
    payload: dict[str, object],
) -> None:
    assert not Cortex._is_repo_scout_github_research(_repo_scout_ingest(payload))


async def test_lab_repo_scout_invalid_url_cannot_fall_back_to_safe_repo(
    tmp_path: Path,
) -> None:
    """The handler consumes url first, so a malformed url must remain gated."""
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings(tmp_path, lab_autonomy=True))

    proposal = await cortex.submit(
        _repo_scout_ingest(
            {
                "url": "https://github.com/pallets/flask?tab=readme",
                "repo": "pallets/flask",
            }
        )
    )

    assert proposal.status is ProposalStatus.GATED
    assert _decision_statuses(bus) == ["gated"]


async def test_lab_repo_scout_safe_identifier_auto_applies(tmp_path: Path) -> None:
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings(tmp_path, lab_autonomy=True))

    proposal = await cortex.submit(_repo_scout_ingest({"repo": "pallets/flask"}))

    assert proposal.status is ProposalStatus.APPLIED
    assert _decision_statuses(bus) == ["approved", "applied"]


async def test_lab_autonomy_auto_applies_safe_local_feature(tmp_path: Path) -> None:
    """The Lab toggle must affect Cortex, even if standard auto-approve is off."""
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings(tmp_path, lab_autonomy=True))

    proposal = await cortex.submit(
        Proposal(
            type=ProposalType.FEATURE,
            title="stage a safe local feature",
            confidence=0.95,
            safe=True,
        )
    )

    assert proposal.status is ProposalStatus.APPLIED
    assert _decision_statuses(bus) == ["approved", "applied"]
    assert bus.history(event_type=EventType.PROPOSAL_DECIDED)[0].payload["reason"] == (
        "auto-approved (safe)"
    )


async def test_standard_mode_keeps_safe_feature_behind_approval_gate(tmp_path: Path) -> None:
    """Lab autonomy must not soften the established non-lab Cortex policy."""
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings(tmp_path, lab_autonomy=False))

    proposal = await cortex.submit(
        Proposal(
            type=ProposalType.FEATURE,
            title="stage a safe local feature",
            confidence=0.95,
            safe=True,
        )
    )

    assert proposal.status is ProposalStatus.GATED
    assert _decision_statuses(bus) == ["gated"]
    assert proposal.decision_reason == "awaiting human approval"


async def test_lab_repo_scout_research_auto_applies_but_keeps_skill_quarantined(
    tmp_path: Path, monkeypatch
) -> None:
    """Lab research can ingest evidence without implicitly promoting its skill."""

    class _Rag:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def ingest_text(self, text, source="", kind="", metadata=None):
            self.calls.append((text, source, kind, metadata))
            return 2

    async def _fetch(url: str):
        return github_fetch.GitHubRepoEvidence(
            source_url=url,
            source_path="README.md",
            pinned_revision="a" * 40,
            license="MIT",
            text=(
                "GitHub repo: pallets/flask\n\n"
                "Description: A web framework with a small core and explicit patterns.\n\n"
                "Language: Python · Stars: 70000\n\n"
                "Topics: python, web, api\n\n"
                "README:\n"
                "# Flask\n\n"
                "Flask is a web application framework that provides routing, request "
                "handling, configuration, testing, and extension points for maintainable "
                "services. Use its application factory pattern when a project grows beyond "
                "one module, keep route registration explicit, and test behavior through the "
                "public HTTP boundary.\n\n"
                "## Setup\n\n"
                "```bash\n"
                "python -m venv .venv\n"
                ".venv/bin/pip install Flask\n"
                "python -m flask --app app run\n"
                "```\n\n"
                "## Layout\n\n"
                "- `src/app.py` defines the application factory and routes.\n"
                "- `src/config.py` contains environment-specific configuration.\n"
                "- `tests/` verifies route behavior with the Flask test client.\n"
            ),
        )

    monkeypatch.setattr(github_fetch, "fetch_github_repo_evidence", _fetch)
    bus = EventBus()
    rag = _Rag()
    skills = SkillLibrary(tmp_path / "skills")
    handlers = HandlerRegistry(rag=rag, skills=skills)
    cortex = Cortex(
        bus,
        settings=_settings(tmp_path, lab_autonomy=True),
        handlers=handlers,
    )

    proposal = await cortex.submit(
        Proposal(
            type=ProposalType.INGEST,
            title="ingest patterns from pallets/flask",
            source="repo_scout",
            payload={
                "repo": "pallets/flask",
                "url": "https://github.com/pallets/flask",
                "language": "Python",
            },
            confidence=0.1,
            safe=False,
        )
    )

    assert proposal.status is ProposalStatus.APPLIED
    assert _decision_statuses(bus) == ["approved", "applied"]
    assert bus.history(event_type=EventType.PROPOSAL_DECIDED)[0].payload["reason"] == (
        "auto-approved (lab autonomy GitHub research; external content quarantined)"
    )
    assert rag.calls[0][3]["external_unreviewed"] is True

    skill = skills.get(str(proposal.result["skill"]))
    assert skill is not None
    assert {"external-candidate", "hygiene:quarantine"} <= set(skill.tags)
    assert "external-promoted" not in skill.tags
    assert skills.relevant("python") == []


async def test_lab_autonomy_keeps_unscoped_ingest_gated(tmp_path: Path) -> None:
    """The research exception cannot turn arbitrary external ingest into a skill source."""
    bus = EventBus()
    cortex = Cortex(bus, settings=_settings(tmp_path, lab_autonomy=True))

    proposal = await cortex.submit(
        Proposal(
            type=ProposalType.INGEST,
            title="ingest arbitrary remote content",
            source="repo_scout",
            payload={"url": "https://example.invalid/untrusted"},
            confidence=0.99,
            safe=False,
        )
    )

    assert proposal.status is ProposalStatus.GATED
    assert _decision_statuses(bus) == ["gated"]
    assert proposal.decision_reason == "awaiting human approval"
