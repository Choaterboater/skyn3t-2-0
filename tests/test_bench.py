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
    publish_go_rate,
    run_bench,
    save_run,
    scorecard,
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


def test_to_dict_carries_per_stack_breakdown():
    run = BenchRun(label="r", results=[
        _stacked("cli1", "python", verdict="go"),
        _stacked("game1", "phaser", verdict="no_go", score=20),
    ])
    d = run.to_dict()
    assert d["by_stack"]["python"]["go_rate"] == 1.0
    assert d["by_stack"]["phaser"]["go_rate"] == 0.0


def test_publish_go_rate_writes_markdown_and_json(tmp_path):
    run = BenchRun(label="r", results=[
        _stacked("cli1", "python", verdict="go"),
        _stacked("game1", "phaser", verdict="no_go", score=20),
    ])

    paths = publish_go_rate(run, tmp_path)

    md = paths["markdown"]
    js = paths["json"]
    assert md.exists() and js.exists()
    text = md.read_text(encoding="utf-8")
    assert "Go-Rate" in text
    assert "python" in text and "phaser" in text
    assert '"go_rate": 0.5' in js.read_text(encoding="utf-8")


def test_scorecard_surfaces_weak_stacks_errors_and_costs():
    results = [
        _stacked("cli1", "python", verdict="go", score=92),
        _stacked("game1", "phaser", verdict="no_go", score=30),
        BenchResult(
            case_id="api1", brief="b", slug="", verdict="no_go", score=None,
            intent_score=None, proof_passed=False, status="error", stack="fastapi",
            cost_usd=0.25,
        ),
    ]
    run = BenchRun(label="factory-exam", results=results)

    card = scorecard(run)

    assert card["label"] == "factory-exam"
    assert card["headline"]["go_rate"] == round(1 / 3, 4)
    assert card["headline"]["errors"] == 1
    assert card["headline"]["cost_per_go_usd"] == 0.35
    assert card["weak_stacks"][0]["stack"] == "fastapi"
    assert {row["stack"] for row in card["weak_stacks"]} == {"fastapi", "phaser"}
    assert card["case_failures"] == [
        {"case_id": "api1", "stack": "fastapi", "status": "error", "score": None},
        {"case_id": "game1", "stack": "phaser", "status": "completed", "score": 30},
    ]


def test_publish_go_rate_writes_factory_scorecard(tmp_path):
    run = BenchRun(label="r", results=[
        _stacked("cli1", "python", verdict="go", score=92),
        _stacked("game1", "phaser", verdict="no_go", score=30),
    ])

    paths = publish_go_rate(run, tmp_path)

    md_text = paths["markdown"].read_text(encoding="utf-8")
    json_text = paths["json"].read_text(encoding="utf-8")
    assert "Factory Scorecard" in md_text
    assert "Weak Stacks" in md_text
    assert "phaser" in md_text
    assert '"scorecard"' in json_text


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


def test_cli_load_bench_cases_accepts_inline_json():
    from skyn3t.cli.main import _load_bench_cases

    cases = _load_bench_cases(
        '[{"id":"swift-only","brief":"a Swift menu-bar timer","stack":"swift"}]'
    )

    assert cases is not None
    assert len(cases) == 1
    assert cases[0].id == "swift-only"
    assert cases[0].brief == "a Swift menu-bar timer"
    assert cases[0].stack == "swift"


# --------------------------------------------------------------------------
# coverage + regression cases (the flywheel's memory)
# --------------------------------------------------------------------------

def test_bench_covers_every_builder_stack():
    # Every real builder stack must have at least one exam case, so a per-app-type
    # GO-rate can never go blind on a stack.
    from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS, _validate_pin

    covered = {_validate_pin(c.stack) for c in DEFAULT_CASES if c.stack}
    missing = set(REAL_BUILDER_STACKS) - covered
    assert not missing, f"bench has no case for: {sorted(missing)}"


def test_capture_regression_case_appends_dedupes_and_loads(tmp_path):
    from skyn3t.studio.bench import (
        all_cases,
        capture_regression_case,
        load_regression_cases,
    )

    assert load_regression_cases(tmp_path) == []
    assert capture_regression_case(tmp_path, "broken build #1", "a thing that failed", "react")
    cases = load_regression_cases(tmp_path)
    assert len(cases) == 1
    assert cases[0].id == "broken-build-1"  # slugified + dash-collapsed
    assert cases[0].brief == "a thing that failed" and cases[0].stack == "react"
    # Deduped by id — a second capture of the same id is a no-op.
    assert capture_regression_case(tmp_path, "broken build #1", "again", "react") is False
    assert len(load_regression_cases(tmp_path)) == 1
    # all_cases merges the defaults + the captured case.
    assert len(all_cases(tmp_path)) == len(DEFAULT_CASES) + 1


def test_bench_capture_failures_defaults_on():
    from skyn3t.config.settings import Settings

    assert Settings().bench_capture_failures is True


def test_capture_regression_case_ignores_empties_and_default_ids(tmp_path):
    from skyn3t.studio.bench import capture_regression_case, load_regression_cases

    assert capture_regression_case(tmp_path, "", "a brief") is False   # no id
    assert capture_regression_case(tmp_path, "x", "") is False         # no brief
    # An id colliding with a built-in case must not be re-added.
    assert capture_regression_case(tmp_path, "notes-api", "dup of a default") is False
    assert load_regression_cases(tmp_path) == []


def test_capture_regression_case_is_capped(tmp_path):
    from skyn3t.studio.bench import capture_regression_case, load_regression_cases

    for i in range(5):
        capture_regression_case(tmp_path, f"c{i}", f"brief {i}", "python", cap=3)
    cases = load_regression_cases(tmp_path)
    assert [c.id for c in cases] == ["c2", "c3", "c4"]  # only the most recent `cap`


def test_finalize_captures_only_failed_builds_when_enabled(tmp_path):
    # The flywheel wiring: with bench_capture_failures on, a no_go build becomes a
    # regression case; a go build does not.
    import asyncio

    from skyn3t.config.settings import Settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.bench import load_regression_cases
    from skyn3t.studio.manifest import BuildManifest
    from skyn3t.studio.planner import Planner
    from skyn3t.studio.runner import StudioRunner

    class _FakeMemory:
        async def save_build(self, **fields):
            return None

    async def run():
        settings = Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
                            logs_dir=tmp_path / "logs", bench_capture_failures=True)
        bus = EventBus()
        runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=_FakeMemory())
        plan = Planner(settings).plan("a python thing", "s")

        bad = BuildManifest(slug="flywheel-fail", brief="a thing that failed review", stack="python")
        bad.verdict = "no_go"
        bad.artifact_dir = str(tmp_path / "bad")
        await runner._finalize(bad, plan, "cid", 20.0)

        good = BuildManifest(slug="flywheel-pass", brief="a good thing", stack="python")
        good.verdict = "go"
        good.artifact_dir = str(tmp_path / "good")
        await runner._finalize(good, plan, "cid", 90.0)

        ids = {c.id for c in load_regression_cases(settings.data_dir)}
        assert "flywheel-fail" in ids     # the failure was captured
        assert "flywheel-pass" not in ids  # the passing build was not

    asyncio.run(run())
