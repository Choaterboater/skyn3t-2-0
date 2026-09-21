import json
from unittest.mock import MagicMock

import pytest

from skyn3t.studio import stack_selection_bench as bench
from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS


def test_suite_covers_registry_and_has_frozen_acceptable_sets():
    assert len({c.id for c in bench.CASES}) == len(bench.CASES)
    expected = {stack for c in bench.CASES for stack in c.acceptable}
    assert expected == set(REAL_BUILDER_STACKS)
    assert {"registry", "task_only", "contrast", "ambiguous"} == {c.group for c in bench.CASES}
    assert all(c.acceptable or c.group == "ambiguous" for c in bench.CASES)


def test_keyword_baseline_is_offline_and_persistent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bench, "TypeSafeAdapter", MagicMock(side_effect=AssertionError("must remain offline"))
    )
    output = tmp_path / "baseline.json"
    report = bench.run_benchmark("keyword", output, repeats=2)
    stored = json.loads(output.read_text())
    assert report["status"] == stored["status"] == "completed"
    assert len(stored["rows"]) == len(bench.CASES) * 2
    assert stored["summary"]["errors"] == 0
    assert stored["summary"]["confident_wrong"] == 0
    assert stored["summary"]["ambiguous_requests"] == 8
    assert len(stored["suite_sha256"]) == 64
    assert stored["summary"]["latency_p95_s"] >= stored["summary"]["latency_median_s"]


def test_live_metrics_separate_accuracy_coverage_errors_and_ambiguity(tmp_path, monkeypatch):
    cases = tuple(
        bench.SelectionCase(name, "test", name, () if name == "ambiguous" else ("react",))
        for name in ("correct", "wrong", "defer", "ambiguous", "error", "invalid")
    )
    monkeypatch.setattr(bench, "CASES", cases)
    adapter = MagicMock()
    answers = {
        "correct": {"choice": "react", "confidence": 0.9},
        "wrong": {"choice": "python", "confidence": 0.9},
        "defer": {"choice": "react", "confidence": 0.52},
        "ambiguous": {"choice": "python", "confidence": 0.9},
        "invalid": {"choice": "react", "confidence": float("nan")},
    }

    def evaluate(*, state, **kwargs):
        assert kwargs["raise_on_error"] is True
        assert kwargs["questions"]["stack"]["criteria"] == REAL_BUILDER_STACKS
        if state == "error":
            raise TimeoutError("do not persist provider error text")
        return {"stack": answers[state]}

    adapter.evaluate_system_one.side_effect = evaluate
    monkeypatch.setattr(bench, "TypeSafeAdapter", lambda _: adapter)
    output = tmp_path / "live.json"
    report = bench.run_benchmark("typesafe", output, repeats=1)
    summary = report["summary"]
    assert report["status"] == "completed_with_errors"
    assert summary["errors"] == 2
    assert summary["raw_accuracy"] == 2 / 5
    assert summary["coverage"] == 2 / 5
    assert summary["accepted_accuracy"] == 0.5
    assert summary["confident_wrong"] == 1
    assert summary["fallback_requests"] == 3
    assert summary["ambiguous_accepted"] == 1
    stored = output.read_text()
    assert "NaN" not in stored
    assert "do not persist" not in stored
    assert json.loads(stored)["summary"] == summary


def test_unavailable_live_provider_is_blocked_not_a_keyword_result(tmp_path, monkeypatch):
    adapter = MagicMock(available=False)
    monkeypatch.setattr(bench, "TypeSafeAdapter", lambda _: adapter)
    output = tmp_path / "blocked.json"
    report = bench.run_benchmark("typesafe", output)
    assert report["status"] == "blocked"
    assert report["rows"] == []
    assert report["summary"]["raw_accuracy"] is None
    assert report["summary"]["accepted_accuracy"] is None
    assert json.loads(output.read_text())["status"] == "blocked"
    adapter.evaluate_system_one.assert_not_called()


@pytest.mark.parametrize("provider,repeats", [("unknown", 1), ("keyword", 0), ("typesafe", 11)])
def test_bad_arguments_rejected(tmp_path, provider, repeats):
    with pytest.raises(ValueError):
        bench.run_benchmark(provider, tmp_path / "invalid.json", repeats=repeats)
    assert not (tmp_path / "invalid.json").exists()


def test_all_deferred_has_no_accepted_accuracy(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "CASES", (bench.SelectionCase("c", "test", "c", ("react",)),))
    adapter = MagicMock()
    adapter.evaluate_system_one.return_value = {"stack": {"choice": "react", "confidence": 0.59}}
    monkeypatch.setattr(bench, "TypeSafeAdapter", lambda _: adapter)
    report = bench.run_benchmark("typesafe", tmp_path / "deferred.json", repeats=1)
    assert report["summary"]["raw_accuracy"] == 1
    assert report["summary"]["coverage"] == 0
    assert report["summary"]["accepted_accuracy"] is None


def test_benchmark_records_distribution_and_response_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "CASES", (bench.SelectionCase("c", "test", "c", ("react",)),))
    adapter = MagicMock()
    distribution = dict.fromkeys(REAL_BUILDER_STACKS, 0.0)
    distribution.update(react=0.7, python=0.3)
    metadata = {"model": "jev-test-version", "request_id": "req_test",
                "usage": {"input_tokens": 120, "output_tokens": 15}}
    adapter.evaluate_system_one.return_value = {"stack": {
        "choice": "react", "confidence": 0.65, "probabilities": distribution,
        "response_metadata": metadata,
    }}
    monkeypatch.setattr(bench, "TypeSafeAdapter", lambda _: adapter)
    report = bench.run_benchmark("typesafe", tmp_path / "evidence.json", repeats=1)
    row = report["rows"][0]
    assert row["probabilities"] == distribution
    assert row["distribution_status"] == "valid"
    assert row["selected_probability"] == 0.7
    assert row["top_two_margin"] == pytest.approx(0.4)
    assert row["response_metadata"] == metadata


@pytest.mark.parametrize("probabilities", [
    {"react": float("nan")}, {"react": -0.1}, {"react": True},
    {"react": 0.5}, {"not_a_stack": 1.0}, ["react"], {},
])
def test_bad_distribution_cannot_become_clean_evidence(tmp_path, monkeypatch, probabilities):
    monkeypatch.setattr(bench, "CASES", (bench.SelectionCase("c", "test", "c", ("react",)),))
    adapter = MagicMock()
    adapter.evaluate_system_one.return_value = {"stack": {
        "choice": "react", "confidence": 0.9, "probabilities": probabilities,
    }}
    monkeypatch.setattr(bench, "TypeSafeAdapter", lambda _: adapter)
    output = tmp_path / "evidence.json"
    report = bench.run_benchmark("typesafe", output, repeats=1)
    row = report["rows"][0]
    assert row["distribution_status"] == "invalid"
    assert row["top_two_margin"] is None
    assert row["probabilities"] is None
    assert report["summary"]["invalid_distribution_requests"] == 1
    json.loads(output.read_text())


@pytest.mark.parametrize("values", [
    {"react": float("nan")}, {"react": float("inf")}, {"react": -0.1},
    {"react": True}, {"react": 0.6, "python": 0.6},
    {"react": 0.1, "python": 0.9},
])
def test_complete_but_invalid_distributions_are_flagged(values):
    probabilities = dict.fromkeys(REAL_BUILDER_STACKS, 0.0)
    probabilities.update(values)
    evidence = bench.distribution_evidence({"choice": "react", "probabilities": probabilities})
    assert evidence["distribution_status"] == "invalid"
    assert evidence["selected_probability"] is None


def test_tied_distribution_records_zero_margin_without_overriding_policy():
    probabilities = dict.fromkeys(REAL_BUILDER_STACKS, 0.0)
    probabilities.update(react=0.5, python=0.5)
    evidence = bench.distribution_evidence({"choice": "react", "probabilities": probabilities})
    assert evidence["distribution_status"] == "valid"
    assert evidence["top_two_margin"] == 0
