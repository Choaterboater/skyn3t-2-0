"""Game visual REPAIR loop (roadmap #9, the "feeds the fix-loop" rung).

The advisory game visual check (``game_visual_check.py``) tells us a delivered game
looks EMPTY or has TINY entities; this loop makes that signal ACT — it feeds the gap to
the improver, rebuilds/re-serves, and re-judges. Because the verdict is a FUZZY vision
call, the loop is do-no-harm: a repair is kept ONLY if it (a) preserves headless-gate
correctness, (b) actually improves the visual verdict, and (c) still builds — otherwise
the tree is rolled back. It never blocks the build verdict.

Every collaborator (judge / improver / gate / build) is injected, so the control flow
and the rollback guards are testable without a browser, vision model, node, or build.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.studio.game_visual_check import GameVisualVerdict
from skyn3t.studio.game_visual_loop import (
    GameVisualRepairResult,
    repair_game_visual,
    select_game_source_files,
    visual_improved,
    visual_regressed,
)

# ── pure verdict comparisons ────────────────────────────────────────────────

def _v(pop=True, read=True, issues=None, skipped=False):
    return GameVisualVerdict(populated=pop, entities_readable_size=read,
                             issues=list(issues or []), skipped=skipped)


def test_regressed_when_a_populated_field_becomes_empty():
    assert visual_regressed(_v(pop=True), _v(pop=False, issues=["empty"])) is True


def test_regressed_when_readable_entities_become_tiny():
    assert visual_regressed(_v(read=True), _v(read=False, issues=["tiny"])) is True


def test_fixing_a_failure_is_not_a_regression():
    assert visual_regressed(_v(pop=False, issues=["empty"]), _v(pop=True)) is False


def test_a_skipped_after_verdict_is_not_a_regression():
    # Could-not-judge is unknown, not a regression (it just isn't an improvement).
    assert visual_regressed(_v(pop=True), _v(skipped=True)) is False


def test_improved_when_empty_field_becomes_populated():
    assert visual_improved(_v(pop=False, issues=["empty"]), _v(pop=True)) is True


def test_improved_when_tiny_entities_become_readable():
    assert visual_improved(_v(read=False, issues=["tiny"]), _v(read=True)) is True


def test_not_improved_when_still_empty():
    assert visual_improved(_v(pop=False, issues=["empty"]),
                           _v(pop=False, issues=["empty"])) is False


def test_not_improved_when_a_fix_introduces_a_new_failure():
    # Fixed "empty" but made them "tiny" — net not an improvement.
    assert visual_improved(_v(pop=False, read=True, issues=["empty"]),
                           _v(pop=True, read=False, issues=["tiny"])) is False


def test_not_improved_when_after_is_skipped():
    assert visual_improved(_v(pop=False, issues=["empty"]), _v(skipped=True)) is False


def test_not_improved_when_before_was_already_clean():
    assert visual_improved(_v(), _v()) is False


# ── game source-file selection (what the improver is steered at) ─────────────

def test_selects_existing_game_source_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text("// entry")
    (tmp_path / "src" / "sim.js").write_text("// pure sim")
    (tmp_path / "src" / "renderer.js").write_text("// draw")
    (tmp_path / "index.html").write_text("<html></html>")  # not a game source file
    files = select_game_source_files(tmp_path)
    assert "src/sim.js" in files
    assert "src/renderer.js" in files
    assert "src/main.js" in files
    assert "index.html" not in files


def test_selects_nested_sim_core(tmp_path: Path):
    (tmp_path / "src" / "sim").mkdir(parents=True)
    (tmp_path / "src" / "sim" / "sim.js").write_text("// pure sim")
    (tmp_path / "src" / "main.js").write_text("// entry")
    files = select_game_source_files(tmp_path)
    assert "src/sim/sim.js" in files


def test_no_game_files_returns_empty(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html></html>")
    assert select_game_source_files(tmp_path) == []


# ── orchestrator: injected collaborators ─────────────────────────────────────

class _Judge:
    """Returns a scripted sequence of verdicts (the vision model's view of the
    tree at each call); the last is repeated if calls exceed the script."""

    def __init__(self, verdicts):
        self._v = list(verdicts)
        self.calls = 0

    async def __call__(self, project_dir):
        v = self._v[min(self.calls, len(self._v) - 1)]
        self.calls += 1
        return v


class _Improver:
    """A fake code_improver: appends a marker to each target file (so rollback is
    observable) and records the gaps/files it was handed."""

    def __init__(self, *, succeeds=True):
        self.calls = 0
        self.gaps: list[list[str]] = []
        self.files: list[list[str]] = []
        self._succeeds = succeeds

    async def __call__(self, gaps, files):
        self.calls += 1
        self.gaps.append(list(gaps))
        self.files.append(list(files))
        if not self._succeeds:
            return False
        return True


class _Gate:
    def __init__(self, passed=True, applicable=True):
        self.passed = passed
        self.applicable = applicable


def _game_tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text("// entry\n")
    (tmp_path / "src" / "sim.js").write_text("// ORIGINAL SIM\n")
    (tmp_path / "src" / "renderer.js").write_text("// ORIGINAL RENDER\n")
    return tmp_path


def _mutating_improver(tmp_path: Path):
    async def _imp(gaps, files):
        _imp.calls += 1  # type: ignore[attr-defined]
        for rel in files:
            p = tmp_path / rel
            if p.is_file():
                p.write_text(p.read_text() + "// EDITED\n")
        return True
    _imp.calls = 0  # type: ignore[attr-defined]
    return _imp


def _run(tmp_path, **kw):
    return asyncio.run(repair_game_visual(tmp_path, settings=object(), **kw))


def test_soft_skip_does_nothing(tmp_path: Path):
    _game_tree(tmp_path)
    imp = _Improver()
    res = _run(tmp_path, judge=_Judge([_v(skipped=True)]), run_improver=imp,
               run_gate=lambda p: _Gate())
    assert isinstance(res, GameVisualRepairResult)
    assert res.skipped is True and res.repaired is False
    assert imp.calls == 0


def test_already_good_does_not_repair(tmp_path: Path):
    _game_tree(tmp_path)
    imp = _Improver()
    res = _run(tmp_path, judge=_Judge([_v()]), run_improver=imp,
               run_gate=lambda p: _Gate())
    assert res.repaired is False
    assert imp.calls == 0


def test_no_improver_capability_records_only(tmp_path: Path):
    # A gap exists, but with no improver we degrade to record-only (today's behavior).
    _game_tree(tmp_path)
    res = _run(tmp_path, judge=_Judge([_v(pop=False, issues=["empty"])]),
               run_improver=None, run_gate=lambda p: _Gate())
    assert res.repaired is False
    assert res.final.gap() is not None
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


def test_empty_field_repaired_and_kept(tmp_path: Path):
    _game_tree(tmp_path)
    judge = _Judge([_v(pop=False, issues=["empty play field"]), _v()])  # empty -> good
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert res.repaired is True
    assert res.final.ok is True
    # the edit was kept
    assert "// EDITED" in (tmp_path / "src" / "sim.js").read_text()


def test_gap_and_files_are_handed_to_the_improver(tmp_path: Path):
    _game_tree(tmp_path)
    imp = _Improver()  # returns True but doesn't mutate; after still empty -> reject
    judge = _Judge([_v(pop=False, issues=["empty"]), _v(pop=False, issues=["empty"])])
    _run(tmp_path, judge=judge, run_improver=imp, run_gate=lambda p: _Gate())
    assert imp.calls == 1
    assert any("empty" in g.lower() for g in imp.gaps[0])  # the gap text was fed
    assert "src/sim.js" in imp.files[0]                    # steered at game source


def test_repair_that_does_not_improve_is_rolled_back(tmp_path: Path):
    _game_tree(tmp_path)
    judge = _Judge([_v(pop=False, issues=["empty"]), _v(pop=False, issues=["empty"])])
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert res.repaired is False
    # the unhelpful edit was reverted
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


def test_repair_that_regresses_visuals_is_rolled_back(tmp_path: Path):
    _game_tree(tmp_path)
    # empty -> now populated but TINY (introduced a new failure) => not an improvement
    judge = _Judge([_v(pop=False, issues=["empty"]),
                    _v(pop=True, read=False, issues=["tiny"])])
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


def test_repair_that_breaks_correctness_is_rolled_back(tmp_path: Path):
    _game_tree(tmp_path)
    # the visual got better, but the headless gate now FAILS -> reject (do-no-harm)
    judge = _Judge([_v(pop=False, issues=["empty"]), _v()])
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=lambda p: _Gate(passed=False, applicable=True),
               build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


def test_accepted_repair_that_breaks_the_build_is_fully_reverted(tmp_path: Path):
    _game_tree(tmp_path)
    judge = _Judge([_v(pop=False, issues=["empty"]), _v()])  # visual improved + kept
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: False)
    assert res.repaired is False
    assert res.build_reverted is True
    assert res.final.gap() is not None  # final reflects the original (un-repaired) verdict
    # ALL edits discarded
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


def test_two_stage_repair_is_bounded_by_attempts(tmp_path: Path):
    _game_tree(tmp_path)
    # empty -> populated+readable but with a leftover issue (improved populated axis,
    # not yet clean) -> clean. Two consecutive accepts, capped at attempts.
    judge = _Judge([_v(pop=False, issues=["empty"]),
                    _v(pop=True, read=True, issues=["needs more enemies"]),
                    _v()])
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp, attempts=2,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert res.repaired is True
    assert res.final.ok is True
    assert imp.calls == 2  # two accepted improver passes, capped at attempts


def test_no_game_source_files_records_only(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html></html>")  # no game source
    imp = _Improver()
    res = _run(tmp_path, judge=_Judge([_v(pop=False, issues=["empty"])]),
               run_improver=imp, run_gate=lambda p: _Gate())
    assert res.repaired is False
    assert imp.calls == 0


# ── finding 7: a repair that introduces NEW concrete defects is a regression ──

def test_introducing_new_issues_is_a_regression():
    # before: empty (one issue). after: populated+readable but with TWO brand-new
    # concrete defects the model flagged -> not an improvement.
    before = _v(pop=False, read=True, issues=["empty"])
    after = _v(pop=True, read=True, issues=["sprites overlapping", "HUD covers field"])
    assert visual_regressed(before, after) is True
    assert visual_improved(before, after) is False


def test_fixing_empty_while_adding_new_issues_is_rolled_back(tmp_path: Path):
    _game_tree(tmp_path)
    judge = _Judge([_v(pop=False, issues=["empty"]),
                    _v(pop=True, read=True, issues=["sprites overlapping", "HUD over field"])])
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


# ── finding 1: collateral damage outside the steered files is rolled back ─────

def test_rejected_repair_undoes_collateral_file_changes(tmp_path: Path):
    _game_tree(tmp_path)
    (tmp_path / "package.json").write_text('{"name":"orig"}')

    async def _imp(gaps, files):
        # edit a steered file, mutate an UNTRACKED file, and create a NEW module
        (tmp_path / "src" / "sim.js").write_text("// MUTATED SIM\n")
        (tmp_path / "package.json").write_text('{"name":"BROKEN"}')
        (tmp_path / "src" / "extra.js").write_text("// improver-created\n")
        return True

    judge = _Judge([_v(pop=False, issues=["empty"]), _v(pop=False, issues=["empty"])])
    res = _run(tmp_path, judge=judge, run_improver=_imp,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert res.repaired is False
    # the steered edit, the untracked edit, AND the created file are all undone
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"
    assert (tmp_path / "package.json").read_text() == '{"name":"orig"}'
    assert not (tmp_path / "src" / "extra.js").exists()


def test_accepted_repair_breaking_build_via_new_file_is_fully_reverted(tmp_path: Path):
    _game_tree(tmp_path)

    async def _imp(gaps, files):
        (tmp_path / "src" / "sim.js").write_text("// MUTATED SIM\n")
        (tmp_path / "src" / "newmod.js").write_text("// dangling\n")
        return True

    judge = _Judge([_v(pop=False, issues=["empty"]), _v()])  # visual improved + kept
    res = _run(tmp_path, judge=judge, run_improver=_imp,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: False)
    assert res.repaired is False and res.build_reverted is True
    # the build backstop reverts to the pre-loop tree, deleting the created file too
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"
    assert not (tmp_path / "src" / "newmod.js").exists()


# ── finding 2: a repair that destroys gate verifiability is rejected ──────────

def test_repair_that_makes_a_verified_game_unverifiable_is_rejected(tmp_path: Path):
    _game_tree(tmp_path)
    # The game started with a VERIFIED, applicable+passing gate; the repair leaves the
    # sim core no longer verifiable (gate degrades to applicable=False) -> reject.
    judge = _Judge([_v(pop=False, issues=["empty"]), _v()])
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               gate=_Gate(passed=True, applicable=True),
               run_gate=lambda p: _Gate(passed=True, applicable=False),
               build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


# ── finding 5: a None/errored gate fails CLOSED (repair not kept) ─────────────

def test_errored_gate_fails_closed(tmp_path: Path):
    _game_tree(tmp_path)

    def _boom(p):
        raise RuntimeError("gate harness crashed")

    judge = _Judge([_v(pop=False, issues=["empty"]), _v()])
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=_boom, build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


# ── finding 4: an improver that edits then FAILS is rolled back ───────────────

def test_improver_that_mutates_then_raises_is_rolled_back(tmp_path: Path):
    _game_tree(tmp_path)

    async def _imp(gaps, files):
        (tmp_path / "src" / "sim.js").write_text("// HALF-APPLIED\n")
        raise RuntimeError("improver blew up mid-edit")

    res = _run(tmp_path, judge=_Judge([_v(pop=False, issues=["empty"])]),
               run_improver=_imp, run_gate=lambda p: _Gate(), build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


def test_improver_that_mutates_then_returns_false_is_rolled_back(tmp_path: Path):
    _game_tree(tmp_path)

    async def _imp(gaps, files):
        (tmp_path / "src" / "sim.js").write_text("// HALF-APPLIED\n")
        return False

    res = _run(tmp_path, judge=_Judge([_v(pop=False, issues=["empty"])]),
               run_improver=_imp, run_gate=lambda p: _Gate(), build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


# ── finding 9: the production (awaitable) run_gate / build_check path ─────────

def test_async_gate_failure_rolls_back_repair(tmp_path: Path):
    _game_tree(tmp_path)

    async def _gate(p):
        return _Gate(passed=False, applicable=True)

    judge = _Judge([_v(pop=False, issues=["empty"]), _v()])  # visual improved
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=_gate, build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


def test_async_build_check_failure_reverts(tmp_path: Path):
    _game_tree(tmp_path)

    async def _gate(p):
        return _Gate(passed=True, applicable=True)

    async def _build(p):
        return False

    judge = _Judge([_v(pop=False, issues=["empty"]), _v()])
    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=judge, run_improver=imp,
               run_gate=_gate, build_check=_build)
    assert res.repaired is False and res.build_reverted is True
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


# ── finding 10: the judge raising on the mid-loop re-judge ────────────────────

def test_mid_loop_judge_crash_rolls_back(tmp_path: Path):
    _game_tree(tmp_path)

    class _CrashJudge:
        def __init__(self):
            self.calls = 0

        async def __call__(self, project_dir):
            self.calls += 1
            if self.calls == 1:
                return _v(pop=False, issues=["empty"])  # real gap on the baseline
            raise RuntimeError("vision crashed on the re-judge")

    imp = _mutating_improver(tmp_path)
    res = _run(tmp_path, judge=_CrashJudge(), run_improver=imp,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert res.repaired is False
    assert (tmp_path / "src" / "sim.js").read_text() == "// ORIGINAL SIM\n"


# ── finding 11: reject breaks the loop; accept-then-reject keeps round 0 ──────

def test_a_rejected_round_stops_the_loop(tmp_path: Path):
    _game_tree(tmp_path)
    imp = _Improver()  # returns True, does not mutate -> after stays empty -> reject
    judge = _Judge([_v(pop=False, issues=["empty"]), _v(pop=False, issues=["empty"])])
    _run(tmp_path, judge=judge, run_improver=imp, attempts=3,
         run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert imp.calls == 1  # reject -> break, NOT continue


def test_accept_then_reject_keeps_only_the_first_round(tmp_path: Path):
    _game_tree(tmp_path)

    calls = {"n": 0}

    async def _imp(gaps, files):
        tag = f"// EDIT{calls['n']}\n"
        calls["n"] += 1
        for rel in files:
            p = tmp_path / rel
            if p.is_file():
                p.write_text(p.read_text() + tag)
        return True

    # round 0: empty -> populated+readable w/ leftover issue (accept)
    # round 1: same verdict again -> not an improvement (reject)
    judge = _Judge([_v(pop=False, issues=["empty"]),
                    _v(pop=True, read=True, issues=["needs polish"]),
                    _v(pop=True, read=True, issues=["needs polish"])])
    res = _run(tmp_path, judge=judge, run_improver=_imp, attempts=2,
               run_gate=lambda p: _Gate(passed=True), build_check=lambda p: True)
    assert res.repaired is True
    sim = (tmp_path / "src" / "sim.js").read_text()
    assert "// EDIT0" in sim       # round-0 edit survives
    assert "// EDIT1" not in sim   # round-1 edit reverted
