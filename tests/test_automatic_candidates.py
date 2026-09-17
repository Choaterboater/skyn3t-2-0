from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.cortex.automatic_candidates import AutomaticCandidates
from skyn3t.cortex.bootstrap import Cortex
from skyn3t.cortex.candidate_engine import VerificationCommand
from skyn3t.cortex.candidate_service import run_cortex_candidate
from skyn3t.cortex.proposal_store import Proposal, ProposalStatus, ProposalType


def settings(tmp_path, **kwargs):
    return Settings(
        data_dir=tmp_path / "data", logs_dir=tmp_path / "logs",
        autonomous_improvement=True, cortex_candidate_auto_merge=True,
        lab_autonomy=False, **kwargs,
    )


async def test_scheduled_research_does_not_wait_for_approval(tmp_path):
    cortex = Cortex(EventBus(), settings=settings(tmp_path))
    proposal = await cortex.submit(Proposal(
        type=ProposalType.INGEST, title="research", source="repo_scout",
        payload={"repo": "pallets/flask"}, safe=False,
    ))
    assert proposal.status is not ProposalStatus.GATED
    invalid = await cortex.submit(Proposal(
        type=ProposalType.INGEST, title="invalid research", source="repo_scout",
        payload={"url": "https://github.com/pallets/flask?untrusted=true"}, safe=False,
    ))
    assert invalid.status is ProposalStatus.GATED


@pytest.mark.parametrize("passes", [True, False])
async def test_automatic_feature_reaches_real_local_merge_only_after_proof(
    tmp_path, monkeypatch, passes,
):
    import subprocess

    from skyn3t.cortex import automatic_candidates

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@localhost")
    (repo / "README.md").write_text("Original\n")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    config = settings(tmp_path)

    def execute(config, goal):
        def apply(worktree):
            (worktree / "README.md").write_text("Adapted feature\n")
            test_dir = worktree / "tests"
            test_dir.mkdir()
            (test_dir / "test_behavior.py").write_text(
                "def test_behavior():\n    assert " + str(passes) + "\n"
            )
        return run_cortex_candidate(
            config, goal, repo_path=repo, apply=apply,
            verification_commands=[VerificationCommand(
                ("python3", "-m", "pytest", "-q", "tests/test_behavior.py"),
                label="proof", timeout_seconds=30,
            )],
        )

    # Replace only model authoring: triage, service, git isolation, proof,
    # report publication and main merge are real.
    monkeypatch.setattr(automatic_candidates, "run_cortex_candidate", execute)
    cortex = Cortex(EventBus(), settings=config)
    automatic = AutomaticCandidates(cortex, config)
    cortex.handlers.register(ProposalType.FEATURE, automatic.apply)
    proposal = await cortex.submit(Proposal(
        type=ProposalType.FEATURE, title="Adapt an application feature", safe=False,
    ))
    assert proposal.status is (ProposalStatus.APPLIED if passes else ProposalStatus.FAILED)
    assert (git("rev-parse", "HEAD") != base) is passes
    assert (repo / "README.md").read_text() == ("Adapted feature\n" if passes else "Original\n")
    assert proposal.result["remote_push"] is False


async def test_unchanged_advisory_is_not_retried_after_failed_adaptation(tmp_path, monkeypatch):
    from skyn3t.cortex import automatic_candidates

    def failed(config, goal):
        return {"candidate": {"status": "verify_failed", "errors": ["failed proof"]}}

    monkeypatch.setattr(automatic_candidates, "run_cortex_candidate", failed)
    config = settings(tmp_path)
    cortex = Cortex(EventBus(), settings=config)
    skill = SimpleNamespace(slug="example", title="Example", body="Specific guidance", provenance=None)
    skills = SimpleNamespace(all=lambda: [skill], capability_status=lambda slug: {"active": True})
    automatic = AutomaticCandidates(cortex, config, skills)
    cortex.handlers.register(ProposalType.FEATURE, automatic.apply)
    await automatic.tick()
    await automatic.tick()
    assert len(cortex.store.all()) == 1
    assert cortex.store.all()[0].status is ProposalStatus.FAILED


async def test_tick_drains_old_gate_but_not_rejected_work(tmp_path, monkeypatch):
    from skyn3t.cortex import automatic_candidates

    monkeypatch.setattr(automatic_candidates, "run_cortex_candidate", lambda *_: {
        "candidate": {"status": "no_changes"}, "remote_push": False,
    })
    config = settings(tmp_path)
    cortex = Cortex(EventBus(), settings=config)
    automatic = AutomaticCandidates(cortex, config)
    cortex.handlers.register(ProposalType.FEATURE, automatic.apply)
    rejected = Proposal(type=ProposalType.FEATURE, title="rejected", status=ProposalStatus.REJECTED)
    gated = Proposal(type=ProposalType.FEATURE, title="old gate", status=ProposalStatus.GATED)
    cortex.store.add(rejected)
    cortex.store.add(gated)
    await automatic.tick()
    assert rejected.status is ProposalStatus.REJECTED
    assert gated.status is ProposalStatus.APPLIED
    assert gated.result["changed"] is False
    automatic.stop()
    await asyncio.wait_for(automatic.run(), timeout=1)
