"""Bounded fix loop: a failing objective proof is repaired, not just reported."""

from __future__ import annotations

from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.planner import Planner
from skyn3t.studio.proof_run import proof_run
from skyn3t.studio.runner import StudioRunner


def _runner():
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def test_fill_missing_repairs_python_checklist(tmp_path):
    runner = _runner()
    plan = Planner(Settings()).plan("build a million dollar idea", "mdi")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.py").write_text("def main():\n    print('idea')\n")
    (proj / "README.md").write_text("# mdi\n")

    before = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    assert before.passed is False and before.checklist_present < before.checklist_total

    filled = runner._fill_missing(str(proj), plan, "build a million dollar idea", list(before.missing))
    assert filled >= 1
    after = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    assert after.passed is True
    assert after.checklist_present == after.checklist_total


def test_stub_for_known_files():
    runner = _runner()
    plan = Planner(Settings()).plan("x", "x")
    assert "[project]" in runner._stub_for("pyproject.toml", plan, "x")
    assert runner._stub_for("src/__init__.py", plan, "x") == ""
    assert "def test" in runner._stub_for("tests/test_basic.py", plan, "x")
    assert runner._stub_for("weird.xyz", plan, "x") is None


async def test_fix_loop_stops_when_proof_passes(tmp_path):
    # A loop with already-passing proof returns immediately (no infinite loop).
    runner = _runner()
    plan = Planner(Settings()).plan("a python tool", "t")
    proj = tmp_path / "p"
    proj.mkdir()
    for rel in plan.checklist:
        f = proj / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n" if rel.endswith(".py") else "# ok\n")
    (proj / "main.py").write_text("print('hi')\n")

    class _M:
        build_id = "b"
        slug = "t"
        brief = "a python tool"
        files = []
        extra = {}

    proof = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    out = await runner._fix_loop(_M(), plan, str(proj), proof, "cid", {"max_fix_attempts": 2})
    assert out.passed is True


# ---- substance gate + best-of-N richness ranking -------------------------
def test_largest_source_bytes(tmp_path):
    runner = _runner()
    proj = tmp_path / "p"
    (proj / "src").mkdir(parents=True)
    (proj / "main.py").write_text("x" * 50)            # thin source
    (proj / "README.md").write_text("y" * 5000)         # not source (excluded)
    (proj / "test_x.py").write_text("z" * 9000)         # test (excluded)
    assert runner._largest_source_bytes(str(proj)) == 50
    # __init__.py counts (can hold the real implementation); bytes SUM
    (proj / "src" / "__init__.py").write_text("z" * 2000)
    assert runner._largest_source_bytes(str(proj)) == 2050
    assert runner._substance_floor == 1500


def test_best_of_n_prefers_richer_over_more_files():
    from skyn3t.studio.best_of_n import Candidate, select

    class _Proof:
        passed = True
        score = 90.0
        files_substantive = 3

    rich = Candidate(index=0, worktree=None, proof=_Proof(), files_written=2, source_bytes=28000)
    thin = Candidate(index=1, worktree=None, proof=_Proof(), files_written=8, source_bytes=559)
    res = select([thin, rich])
    assert res.winner is rich  # substance beats raw file count
