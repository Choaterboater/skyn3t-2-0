# tests/test_bench.py
"""Benchmark/regression harness (Spec 2): run a fixed brief-set through the
factory, record a scored ledger, diff two runs, and gate a change on the
measured delta. The harness logic is exercised with an injected build_fn so no
real spine is needed."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.studio.bench import (
    DEFAULT_CASES,
    BenchCase,
    BenchResult,
    BenchRun,
    diff_runs,
    gate_change,
    load_run,
    run_bench,
    save_run,
    summarize,
    summarize_by_stack,
)


def _stacked(case_id, stack, verdict="go", score=90.0):
    return BenchResult(case_id=case_id, brief="b", slug="s", verdict=verdict,
                       score=score, intent_score=80.0, proof_passed=True,
                       status="completed", stack=stack, cost_usd=0.05)


def _outcome(verdict="go", score=90.0, intent=88.0, proof_passed=True,
             status="completed", stack="python", slug="demo", cost_usd=0.05):
    extra = {}
    if intent is not None:
        extra["intent"] = {"score": intent}
    extra["proof"] = {"passed": proof_passed}
    return SimpleNamespace(
        verdict=verdict, score=score, status=status, stack=stack, slug=slug,
        project_dir="/x", cost_usd=cost_usd, manifest={"extra": extra},
    )


def _result(case_id="c1", verdict="go", score=90.0, intent=88.0, cost_usd=0.05):
    return BenchResult(case_id=case_id, brief="b", slug="s", verdict=verdict,
                       score=score, intent_score=intent, proof_passed=True,
                       status="completed", stack="python", cost_usd=cost_usd)


# --------------------------------------------------------------------------
# summarize + run
# --------------------------------------------------------------------------

def test_summarize_computes_go_rate_and_means():
    results = [_result(verdict="go", score=80, intent=70),
               _result(verdict="no_go", score=40, intent=20)]
    s = summarize(results)
    assert s["n"] == 2
    assert s["go_rate"] == 0.5
    assert s["mean_score"] == 60.0
    assert s["mean_intent"] == 45.0
    # go-only mean is the shippable-quality signal (excludes the no_go's 40)
    assert s["mean_score_go"] == 80.0


def test_summarize_by_stack_isolates_per_app_type_go_rate():
    # phaser fully failing while python ships — the aggregate would hide it.
    results = [
        _stacked("cli1", "python", verdict="go", score=90),
        _stacked("cli2", "python", verdict="go", score=80),
        _stacked("game1", "phaser", verdict="no_go", score=30),
    ]
    # aggregate go-rate is a rosy 2/3, but the per-stack view exposes the games gap.
    assert summarize(results)["go_rate"] == round(2 / 3, 4)
    by_stack = summarize_by_stack(results)
    assert by_stack["python"]["go_rate"] == 1.0
    assert by_stack["phaser"]["go_rate"] == 0.0
    # ordered by descending case count → python (2) before phaser (1).
    assert list(by_stack) == ["python", "phaser"]


def test_summarize_by_stack_groups_empty_stack_as_unknown():
    by_stack = summarize_by_stack([_stacked("c", "", verdict="go")])
    assert "unknown" in by_stack and by_stack["unknown"]["n"] == 1


def test_run_bench_uses_injected_build_fn_and_orders_results():
    cases = [BenchCase(id="web", brief="a coloring website"),
             BenchCase(id="cli", brief="a python cli")]

    async def build_fn(case):
        return _outcome(verdict="go" if case.id == "web" else "no_go",
                        score=95.0 if case.id == "web" else 30.0,
                        slug=case.id)

    run = asyncio.run(run_bench(cases, build_fn, label="t1"))
    assert isinstance(run, BenchRun)
    assert [r.case_id for r in run.results] == ["web", "cli"]
    assert run.results[0].verdict == "go" and run.results[1].verdict == "no_go"
    assert run.summary["go_rate"] == 0.5
    assert run.label == "t1"


def test_run_bench_survives_a_failing_build():
    cases = [BenchCase(id="ok", brief="x"), BenchCase(id="boom", brief="y")]

    async def build_fn(case):
        if case.id == "boom":
            raise RuntimeError("build crashed")
        return _outcome(slug=case.id)

    run = asyncio.run(run_bench(cases, build_fn))
    by_id = {r.case_id: r for r in run.results}
    assert by_id["ok"].verdict == "go"
    assert by_id["boom"].status == "error" and by_id["boom"].verdict == "no_go"


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

def test_save_and_load_run_roundtrip(tmp_path):
    run = BenchRun(label="r1", results=[_result(), _result(case_id="c2", verdict="no_go", score=20, intent=10)])
    run.summary = summarize(run.results)
    path = save_run(run, tmp_path)
    assert path.exists()
    loaded = load_run(path)
    assert loaded.label == "r1"
    assert [r.case_id for r in loaded.results] == ["c1", "c2"]
    assert loaded.summary["n"] == 2


# --------------------------------------------------------------------------
# diff + gate
# --------------------------------------------------------------------------

def test_diff_flags_verdict_regression():
    before = BenchRun(label="b", results=[_result(case_id="c1", verdict="go", score=85)])
    after = BenchRun(label="a", results=[_result(case_id="c1", verdict="no_go", score=40)])
    d = diff_runs(before, after)
    assert any(r["case_id"] == "c1" for r in d["regressions"])
    assert d["mean_score_delta"] == -45.0
    assert d["go_rate_delta"] == -1.0


def test_diff_reports_improvement():
    before = BenchRun(label="b", results=[_result(case_id="c1", verdict="no_go", score=30)])
    after = BenchRun(label="a", results=[_result(case_id="c1", verdict="go", score=88)])
    d = diff_runs(before, after)
    assert any(r["case_id"] == "c1" for r in d["improvements"])
    assert d["regressions"] == []
    assert d["mean_score_delta"] == 58.0


def test_diff_flags_score_regression_beyond_threshold():
    before = BenchRun(label="b", results=[_result(case_id="c1", verdict="go", score=90)])
    after = BenchRun(label="a", results=[_result(case_id="c1", verdict="go", score=70)])
    d = diff_runs(before, after, score_regress_threshold=10.0)
    assert any(r["case_id"] == "c1" for r in d["regressions"])


def test_gate_rejects_verdict_regression():
    before = BenchRun(label="b", results=[_result(case_id="c1", verdict="go", score=85)])
    after = BenchRun(label="a", results=[_result(case_id="c1", verdict="no_go", score=40)])
    d = diff_runs(before, after)
    ok, reasons = gate_change(d)
    assert ok is False and reasons


def test_gate_accepts_improvement():
    before = BenchRun(label="b", results=[_result(case_id="c1", verdict="no_go", score=30)])
    after = BenchRun(label="a", results=[_result(case_id="c1", verdict="go", score=88)])
    d = diff_runs(before, after)
    ok, _ = gate_change(d)
    assert ok is True


def test_gate_rejects_flat_change_when_min_delta_positive():
    before = BenchRun(label="b", results=[_result(case_id="c1", score=80)])
    after = BenchRun(label="a", results=[_result(case_id="c1", score=80)])
    d = diff_runs(before, after)
    ok, _ = gate_change(d, min_mean_score_delta=1.0)
    assert ok is False


def test_diff_flags_dropped_go_case_as_regression():
    # a passing case removed from the suite is silent coverage loss — flag it
    before = BenchRun(label="b", results=[_result(case_id="keep", verdict="go", score=80),
                                          _result(case_id="dropped", verdict="go", score=95)])
    after = BenchRun(label="a", results=[_result(case_id="keep", verdict="go", score=80)])
    d = diff_runs(before, after)
    assert any(r["case_id"] == "dropped" and r.get("kind") == "dropped" for r in d["regressions"])


def test_diff_no_phantom_score_regression_on_error():
    # a go case that crashes (score None, status error) is a VERDICT regression,
    # not a fabricated -score; per-case score_delta is None, not a big negative.
    before = BenchRun(label="b", results=[_result(case_id="c1", verdict="go", score=85)])
    err = BenchResult(case_id="c1", brief="b", slug="", verdict="no_go", score=None,
                      intent_score=None, proof_passed=False, status="error", stack="python")
    after = BenchRun(label="a", results=[err])
    d = diff_runs(before, after)
    kinds = {r["case_id"]: r.get("kind") for r in d["regressions"]}
    assert kinds.get("c1") == "verdict"
    entry = next(e for e in d["per_case"] if e["case_id"] == "c1")
    assert entry["score_delta"] is None


def test_gate_rejects_go_rate_drop():
    before = BenchRun(label="b", results=[_result(case_id="c1", verdict="go", score=80),
                                          _result(case_id="c2", verdict="go", score=80)])
    after = BenchRun(label="a", results=[_result(case_id="c1", verdict="go", score=80),
                                         _result(case_id="c2", verdict="no_go", score=70)])
    d = diff_runs(before, after)
    ok, reasons = gate_change(d)
    assert ok is False and reasons


def test_gate_rejects_empty_run():
    before = BenchRun(label="b", results=[])
    after = BenchRun(label="a", results=[_result(case_id="c1", verdict="go", score=90)])
    d = diff_runs(before, after)
    ok, reasons = gate_change(d)
    assert ok is False  # a zero-case baseline must not rubber-stamp a pass


def test_summarize_reports_cost_efficiency():
    # two go (0.10 each) + one no_go (0.30). cost-per-go = total/go = 0.50/2.
    results = [_result(case_id="a", verdict="go", cost_usd=0.10),
               _result(case_id="b", verdict="go", cost_usd=0.10),
               _result(case_id="c", verdict="no_go", cost_usd=0.30)]
    s = summarize(results)
    assert s["total_cost_usd"] == 0.5
    assert s["cost_per_go_usd"] == 0.25  # 0.50 spent / 2 shipped


def test_cost_per_go_is_none_when_nothing_ships():
    s = summarize([_result(verdict="no_go", cost_usd=0.4)])
    assert s["cost_per_go_usd"] is None  # undefined, not zero


def test_from_outcome_captures_cost():
    run = BenchResult.from_outcome(BenchCase(id="x", brief="b"),
                                   _outcome(cost_usd=0.123))
    assert run.cost_usd == 0.123


def test_gate_rejects_cost_per_go_regression():
    # same quality, but each go now costs more -> cost-per-go regression
    before = BenchRun(label="b", results=[_result(case_id="c1", verdict="go", cost_usd=0.10)])
    after = BenchRun(label="a", results=[_result(case_id="c1", verdict="go", cost_usd=0.40)])
    d = diff_runs(before, after)
    ok, reasons = gate_change(d, max_cost_per_go_increase=0.10)
    assert ok is False and any("cost" in r for r in reasons)
    # without the cost bar it's accepted (quality flat, no regression)
    ok2, _ = gate_change(d)
    assert ok2 is True


def test_default_cases_use_valid_pin_keys():
    from skyn3t.studio.stack_selector import _validate_pin
    assert len(DEFAULT_CASES) >= 3
    assert all(c.brief for c in DEFAULT_CASES)
    assert len({c.id for c in DEFAULT_CASES}) == len(DEFAULT_CASES)  # unique ids
    for c in DEFAULT_CASES:
        if c.stack:  # a pinned stack must be one the selector actually accepts
            assert _validate_pin(c.stack), f"{c.id}: {c.stack!r} is not a valid pin"
