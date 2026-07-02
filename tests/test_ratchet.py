"""The reliability ratchet — keep a change only if the bench measurably improves.

The flywheel's decision brain, tested fully injected (fake build_fn + apply/revert),
so no real spine or Cortex is needed: an improving change is KEPT (never reverted),
a regressing one is REVERTED, and — critically — a change that lifts the AGGREGATE
go-rate while breaking ONE app-type is still reverted by the per-app-type guard.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from skyn3t.cortex.ratchet import (
    evaluate_change,
    per_stack_go_rate_regressions,
    restore_overrides,
    snapshot_overrides,
)
from skyn3t.studio.bench import BenchCase, run_bench


def _outcome(verdict="go", score=90.0, stack="python", slug="s"):
    return SimpleNamespace(
        verdict=verdict, score=score, status="completed", stack=stack, slug=slug,
        cost_usd=0.05,
        manifest={"extra": {"intent": {"score": 80}, "proof": {"passed": verdict == "go"}}},
    )


def _harness(effect):
    """Build (state, apply, revert, make_build_fn, revert_count) where `effect` maps
    (applied, case) -> (verdict, score)."""
    state = {"applied": False}
    reverts = {"n": 0}

    def apply():
        state["applied"] = True

    def revert():
        reverts["n"] += 1
        state["applied"] = False

    def make_build_fn():
        applied = state["applied"]  # captured at bench-run time (fresh per run)

        async def build_fn(case):
            v, sc = effect(applied, case)
            return _outcome(verdict=v, score=sc, stack=case.stack, slug=case.id)

        return build_fn

    return state, apply, revert, make_build_fn, reverts


def test_keeps_an_improving_change():
    cases = [BenchCase("a", "x", "python"), BenchCase("b", "y", "react")]
    _, apply, revert, make, reverts = _harness(
        lambda applied, c: ("go", 90.0) if applied else ("no_go", 30.0)
    )
    res = asyncio.run(evaluate_change(apply_change=apply, revert_change=revert,
                                      make_build_fn=make, cases=cases))
    assert res["kept"] is True
    assert reverts["n"] == 0                      # a kept change is never reverted
    assert res["go_rate_delta"] > 0


def test_reverts_a_regressing_change():
    cases = [BenchCase("a", "x", "python"), BenchCase("b", "y", "react")]
    _, apply, revert, make, reverts = _harness(
        lambda applied, c: ("no_go", 20.0) if applied else ("go", 90.0)
    )
    res = asyncio.run(evaluate_change(apply_change=apply, revert_change=revert,
                                      make_build_fn=make, cases=cases))
    assert res["kept"] is False
    assert reverts["n"] == 1                      # the regression was rolled back
    assert res["reasons"]


def test_per_stack_guard_reverts_when_one_app_type_drops():
    # python IMPROVES (offsetting the aggregate) while swift REGRESSES. The
    # aggregate go-rate rises and gate_change is told to allow verdict regressions,
    # so ONLY the per-app-type guard can catch the swift break.
    cases = [BenchCase("p1", "x", "python"), BenchCase("p2", "x", "python"),
             BenchCase("s1", "y", "swift")]

    def effect(applied, c):
        if c.stack == "python":
            return ("go", 90.0) if applied else ("no_go", 30.0)
        return ("no_go", 20.0) if applied else ("go", 90.0)  # swift breaks

    _, apply, revert, make, reverts = _harness(effect)
    res = asyncio.run(evaluate_change(
        apply_change=apply, revert_change=revert, make_build_fn=make, cases=cases,
        gate_kwargs={"allow_verdict_regressions": True, "min_mean_score_delta": -1000.0},
    ))
    assert res["kept"] is False                   # per-app-type guard vetoes it
    assert reverts["n"] == 1
    assert any("swift" in s for s in res["stack_regressions"])


def test_reverts_on_apply_error():
    cases = [BenchCase("a", "x", "python")]
    reverts = {"n": 0}

    def apply():
        raise RuntimeError("apply blew up")

    def make():
        async def build_fn(case):
            return _outcome()
        return build_fn

    res = asyncio.run(evaluate_change(apply_change=apply,
                                      revert_change=lambda: reverts.__setitem__("n", reverts["n"] + 1),
                                      make_build_fn=make, cases=cases))
    # apply raised AFTER being marked applied -> revert fires; error recorded.
    assert res["kept"] is False and res.get("error") is True
    assert reverts["n"] == 1


def test_per_stack_go_rate_regressions_helper():
    async def go():
        before = await run_bench([BenchCase("s1", "y", "swift")],
                                 lambda c: _async(_outcome(verdict="go", stack="swift")))
        after = await run_bench([BenchCase("s1", "y", "swift")],
                                lambda c: _async(_outcome(verdict="no_go", stack="swift")))
        return before, after

    before, after = asyncio.run(go())
    regs = per_stack_go_rate_regressions(before, after)
    assert regs and "swift" in regs[0]


def _async(value):
    async def _f():
        return value
    return _f()


def test_snapshot_and_restore_overrides(tmp_path):
    base = tmp_path / "cortex"
    base.mkdir()
    (base / "settings_overrides.json").write_text(json.dumps({"best_of_n": 1}))
    # prompt_overrides.json intentionally absent.

    snap = snapshot_overrides(tmp_path)
    # Mutate both files: change one, create the other.
    (base / "settings_overrides.json").write_text(json.dumps({"best_of_n": 5}))
    (base / "prompt_overrides.json").write_text(json.dumps({"reviewer": "be strict"}))

    restore_overrides(tmp_path, snap)
    # settings restored to the snapshot; prompt file deleted (it didn't exist then).
    assert json.loads((base / "settings_overrides.json").read_text()) == {"best_of_n": 1}
    assert not (base / "prompt_overrides.json").exists()
