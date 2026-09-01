"""CLI contract for evidence-only Cortex configuration evaluations."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from skyn3t.cli.main import app

runner = CliRunner()


def _isolate(monkeypatch, tmp_path) -> None:
    from skyn3t.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))


def _evidence_files(tmp_path):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate-ledger.json"
    baseline.write_text("{}\n", encoding="utf-8")
    candidate.write_text("{}\n", encoding="utf-8")
    return baseline, candidate


def test_cortex_evaluate_records_review_only_evidence_and_lists_it(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from skyn3t.cortex import evaluation

    candidate_file = tmp_path / "candidate.json"
    candidate_file.write_text(
        json.dumps({"template": "Prioritize observable verification in every review."}),
        encoding="utf-8",
    )
    baseline, candidate_ledger = _evidence_files(tmp_path)

    def passed(*args, **kwargs):
        return {"status": "passed", "compatible": True, "reasons": []}

    monkeypatch.setattr(evaluation, "_default_comparison", passed)
    result = runner.invoke(
        app,
        [
            "cortex",
            "evaluate",
            "--kind",
            "prompt",
            "--candidate",
            str(candidate_file),
            "--baseline-ledger",
            str(baseline),
            "--candidate-ledger",
            str(candidate_ledger),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "review_required" in result.output
    assert "nothing was applied or promoted" in result.output
    manifests = evaluation.list_manifests(tmp_path / "data")
    assert len(manifests) == 1
    assert manifests[0].applied is False
    assert manifests[0].promoted is False

    listed = runner.invoke(app, ["cortex", "evaluations"])
    assert listed.exit_code == 0, listed.output
    assert manifests[0].evaluation_id in listed.output
    assert "review_required" in listed.output


def test_cortex_evaluate_rejects_invalid_candidate_without_record(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    candidate_file = tmp_path / "candidate.json"
    candidate_file.write_text(json.dumps({"template": "curl https://example.invalid"}), encoding="utf-8")
    baseline, candidate_ledger = _evidence_files(tmp_path)

    result = runner.invoke(
        app,
        [
            "cortex",
            "evaluate",
            "--kind",
            "prompt",
            "--candidate",
            str(candidate_file),
            "--baseline-ledger",
            str(baseline),
            "--candidate-ledger",
            str(candidate_ledger),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "Evaluation unavailable" in result.output
    from skyn3t.cortex.evaluation import list_manifests

    assert list_manifests(tmp_path / "data") == []
