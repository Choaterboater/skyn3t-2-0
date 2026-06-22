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
)


def _outcome(verdict="go", score=90.0, intent=88.0, proof_passed=True,
             status="completed", stack="python", slug="demo"):
    extra = {}
    if intent is not None:
        extra["intent"] = {"score": intent}
    extra["proof"] = {"passed": proof_passed}
    return SimpleNamespace(
        verdict=verdict, score=score, status=status, stack=stack, slug=slug,
        project_dir="/x", manifest={"extra": extra},
    )


def _result(case_id="c1", verdict="go", score=90.0, intent=88.0):
    return BenchResult(case_id=case_id, brief="b", slug="s", verdict=verdict,
                       score=score, intent_score=intent, proof_passed=True,
                       status="completed", stack="python")


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


def test_default_cases_cover_multiple_stacks():
    assert len(DEFAULT_CASES) >= 3
    assert all(c.brief for c in DEFAULT_CASES)
    assert len({c.id for c in DEFAULT_CASES}) == len(DEFAULT_CASES)  # unique ids
