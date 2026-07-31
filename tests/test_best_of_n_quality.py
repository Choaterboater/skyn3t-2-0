from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.core.agent import TaskResult
from skyn3t.studio import best_of_n as bon
from skyn3t.studio.best_of_n import Candidate
from skyn3t.studio.build_summary import build_summary
from skyn3t.studio.proof_run import ProofResult
from skyn3t.web.deps import BuildRecord
from skyn3t.worktree import cleanup_worktree, create_worktree


def _proof(
    passed: bool,
    *,
    score: float,
    files: int = 10,
    substantive: int = 6,
    missing: list[str] | None = None,
) -> ProofResult:
    return ProofResult(
        passed=passed,
        mode="local",
        files_total=files,
        files_substantive=substantive,
        checklist_total=files,
        checklist_present=files - len(missing or []),
        missing=list(missing or []),
        score=score,
        detail={"build": "passed" if passed else "failed", "tests": "passed" if passed else "failed"},
    )


def test_proof_passer_strictly_dominates_successful_high_score_proof_failure():
    proof_failure = Candidate(
        index=0,
        worktree=None,
        result=TaskResult(task_id="bad", success=True, output={}),
        proof=_proof(False, score=100.0, files=90, substantive=80),
        files_written=90,
        source_bytes=1_000_000,
    )
    late_timeout_passer = Candidate(
        index=1,
        worktree=None,
        result=TaskResult(
            task_id="proved",
            success=False,
            error="agent session timed out after writing the complete tree",
            output={"agentic": {"complete": False}},
        ),
        proof=_proof(True, score=72.0, files=87, substantive=60),
        files_written=87,
        source_bytes=250_000,
    )

    selection = bon.select([proof_failure, late_timeout_passer])

    assert late_timeout_passer.proof_passed is True
    assert late_timeout_passer.passed is False
    assert selection.winner is late_timeout_passer
    assert selection.any_passed is True
    assert selection.to_evidence()["selection_class"] == "proof_passed"
    assert [c["selected"] for c in selection.to_evidence()["candidates"]] == [False, True]


def test_exact_rank_tie_is_deterministic_by_candidate_index():
    candidates = [
        Candidate(
            index=index,
            worktree=None,
            result=TaskResult(task_id=str(index), success=True),
            proof=_proof(True, score=90.0),
            files_written=10,
            source_bytes=50_000,
        )
        for index in (7, 2, 5)
    ]

    assert bon.select(candidates).winner is candidates[1]
    assert bon.select(list(reversed(candidates))).winner.index == 2


def test_selection_evidence_is_frozen_and_bounded_before_cleanup():
    candidate = Candidate(
        index=0,
        worktree=None,
        result=TaskResult(task_id="failed", success=True, duration_ms=123.456),
        proof=_proof(
            False,
            score=91.0,
            missing=["public/assets/hero.png"],
        ),
        files_written=87,
        source_bytes=400_000,
        duration_ms=456.789,
        proof_duration_ms=33.333,
    )
    selection = bon.select([candidate])
    frozen = selection.to_evidence()

    candidate.files_written = 999
    candidate.proof.missing.append("mutation-after-selection.txt")

    assert selection.to_evidence() == frozen
    evidence = frozen["candidates"][0]
    assert evidence["files_written"] == 87
    assert evidence["duration_ms"] == 456.79
    assert evidence["proof"]["missing"] == ["public/assets/hero.png"]
    assert "detail" not in evidence["proof"]


def test_sample_keeps_compact_loser_evidence_after_loser_tree_cleanup(tmp_path, monkeypatch):
    async def trajectory(worktree, index):
        source = worktree.path / "src" / "main.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"candidate = {index}\n", encoding="utf-8")
        return TaskResult(
            task_id=str(index),
            success=index == 0,
            error=None if index == 0 else "late session timeout",
            output={"files_written": 1},
            duration_ms=10.0 + index,
        )

    def fake_proof(path, **_kwargs):
        index = 1 if "cand1" in str(path) else 0
        return _proof(index == 1, score=70.0 + index)

    monkeypatch.setattr(bon, "proof_run", fake_proof)
    selection = asyncio.run(
        bon.sample(
            str(tmp_path / "projects"),
            "quality",
            2,
            trajectory,
            checklist=["src/main.py"],
        )
    )
    try:
        assert selection.winner is not None and selection.winner.index == 1
        loser = next(candidate for candidate in selection.candidates if candidate.index == 0)
        assert not Path(loser.worktree.dir).exists()
        evidence = selection.to_evidence()
        assert evidence["winner_index"] == 1
        assert evidence["candidates"][0]["proof_passed"] is False
        assert evidence["candidates"][1]["proof_passed"] is True
        assert evidence["candidates"][1]["selected"] is True
    finally:
        cleanup_worktree(selection.winner.worktree)


def test_sample_forwards_build_and_test_knobs_to_proof_run(tmp_path, monkeypatch):
    """Candidate ranking must use the same objective proof the delivered tree
    later faces, so the runner's build/test settings thread into proof_run."""
    captured: list[dict] = []

    async def trajectory(worktree, index):
        return TaskResult(task_id=str(index), success=True, output={"files_written": 0})

    def fake_proof(path, **kwargs):
        captured.append(kwargs)
        return _proof(True, score=80.0)

    monkeypatch.setattr(bon, "proof_run", fake_proof)
    selection = asyncio.run(
        bon.sample(
            str(tmp_path / "projects"),
            "knobs",
            1,
            trajectory,
            checklist=["src/main.py"],
            stack="react",
            run_tests=True,
            test_timeout=45,
            run_build=True,
            build_timeout=120,
            brief="a build-aware candidate proof",
        )
    )
    try:
        assert captured and captured[0]["run_build"] is True
        assert captured[0]["run_tests"] is True
        assert captured[0]["build_timeout"] == 120
        assert captured[0]["test_timeout"] == 45
        assert captured[0]["brief"] == "a build-aware candidate proof"
        assert captured[0]["stack"] == "react"
    finally:
        cleanup_worktree(selection.winner.worktree)


def test_sample_defaults_keep_the_structural_proof(tmp_path, monkeypatch):
    """Direct callers that pass no knobs must keep proof_run's own defaults."""
    captured: list[dict] = []

    async def trajectory(worktree, index):
        return TaskResult(task_id=str(index), success=True, output={"files_written": 0})

    def fake_proof(path, **kwargs):
        captured.append(kwargs)
        return _proof(True, score=80.0)

    monkeypatch.setattr(bon, "proof_run", fake_proof)
    selection = asyncio.run(bon.sample(str(tmp_path / "projects"), "plain", 1, trajectory))
    try:
        assert captured[0]["run_build"] is False
        assert captured[0]["run_tests"] is False
    finally:
        cleanup_worktree(selection.winner.worktree)


def test_build_summary_and_live_api_record_surface_compact_evidence(tmp_path):
    worktree = create_worktree(tmp_path, "summary")
    try:
        candidate = Candidate(
            index=0,
            worktree=worktree,
            result=TaskResult(task_id="winner", success=True, model_id="vendor/model"),
            proof=_proof(True, score=94.0),
            files_written=12,
            source_bytes=64_000,
        )
        evidence = bon.select([candidate]).to_evidence()
        summary = build_summary({"extra": {"best_of_n": evidence}})
        record = BuildRecord(build_id="build", brief="brief", best_of_n=summary["best_of_n"])

        assert summary["best_of_n"]["winner_index"] == 0
        assert summary["best_of_n"]["candidates"][0]["proof"]["score"] == 94.0
        assert record.to_dict()["best_of_n"] == summary["best_of_n"]
    finally:
        cleanup_worktree(worktree)
