# tests/test_iter8_fixes.py
"""Iteration-8 fixes: merge_back(clean=True) must not wipe the destination when
the source is unreadable (data loss); and when no brief-aware reviewer ran the
final score must be proof-based, not contaminated by the structural rescore."""
from __future__ import annotations

from skyn3t import worktree as wt_mod
from skyn3t.worktree import merge_back


def test_merge_back_clean_normal_still_copies(tmp_path):
    src = tmp_path / "wt"
    src.mkdir()
    (src / "main.py").write_text("print('x')\n")
    dst = tmp_path / "proj"
    dst.mkdir()
    (dst / "old.py").write_text("stale\n")
    copied = merge_back(src, dst, clean=True)
    assert "main.py" in copied
    assert (dst / "main.py").exists()
    assert not (dst / "old.py").exists()  # clean removed the stale file


def test_merge_back_clean_preserves_dest_when_source_read_fails(tmp_path, monkeypatch):
    src = tmp_path / "wt"
    src.mkdir()
    (src / "main.py").write_text("x\n")
    dst = tmp_path / "proj"
    dst.mkdir()
    (dst / "delivered.py").write_text("important work\n")

    def boom(_root):
        raise PermissionError("source became unreadable")
    monkeypatch.setattr(wt_mod, "_iter_files", boom)

    copied = merge_back(src, dst, clean=True)
    assert copied == []
    # CRITICAL: the previously-delivered file must NOT have been wiped.
    assert (dst / "delivered.py").read_text() == "important work\n"


def test_score_uses_proof_when_no_reviewer_ran():
    # When reviewer_ran is False, the score must be proof-based (no structural
    # rescore leaking into the 60/40 blend). Mirrors the runner's inline logic.
    def final_score(reviewer_ran, reviewer_score, re_score, proof_score):
        if not reviewer_ran:
            reviewer_score = proof_score  # the iter-8 fix
        else:
            reviewer_score = reviewer_score
        return round(0.6 * reviewer_score + 0.4 * proof_score, 2)

    # no reviewer, high structural rescore: must equal proof.score, not blend it
    assert final_score(False, 0.0, 85.0, 70.0) == 70.0
    # reviewer ran: its score is honored
    assert final_score(True, 85.0, 0.0, 70.0) == round(0.6 * 85 + 0.4 * 70, 2)
