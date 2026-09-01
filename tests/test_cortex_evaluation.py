"""Focused contracts for evidence-only Cortex learning evaluations."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from skyn3t.cortex.evaluation import (
    EvaluationManifest,
    EvaluationPersistenceError,
    EvaluationValidationError,
    EvidenceDigest,
    evaluate_candidate,
    list_manifests,
    load_manifest,
    validate_candidate,
)


def _ledger(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _passing_comparison(baseline, candidate, **kwargs):
    assert baseline.suffix == ".json"
    assert candidate.suffix == ".json"
    assert kwargs == {"max_suite_pass_rate_drop": 0.0, "min_case_pass_rate": 1.0}
    return {"status": "passed", "compatible": True, "reasons": []}


def _failed_comparison(_baseline, _candidate, **_kwargs):
    return {
        "status": "failed",
        "compatible": True,
        "reasons": ["case dashboard pass rate is below the required floor"],
        "suite_pass_rate_drop": 0.25,
    }


def _ledger_pair(tmp_path):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    _ledger(baseline, {"fixture": "baseline", "attempts": 1})
    _ledger(candidate, {"fixture": "candidate", "attempts": 1})
    return baseline, candidate


def test_candidate_schemas_reject_unknown_secret_and_executable_content():
    assert validate_candidate(
        "router_policy",
        {"default_backend": "codex_cli", "fallback_order": ["codex_cli", "kimi_cli"]},
    ) == {"default_backend": "codex_cli", "fallback_order": ["codex_cli", "kimi_cli"]}

    with pytest.raises(EvaluationValidationError, match="candidate_kind"):
        validate_candidate("code_patch", {"template": "be helpful"})
    with pytest.raises(EvaluationValidationError, match="secret"):
        validate_candidate("prompt", {"template": "be helpful", "api_key": "not-a-secret"})
    with pytest.raises(EvaluationValidationError, match="command"):
        validate_candidate("skill_policy", {"command": "python -m pytest"})
    with pytest.raises(EvaluationValidationError, match="code, a command, a filesystem path, or a URL"):
        validate_candidate("prompt", {"template": "Run powershell to change the policy"})


def test_manifest_hash_is_deterministic_and_tamper_evident():
    evidence = (
        EvidenceDigest("baseline_ledger", "baseline.json", "a" * 64, 10),
        EvidenceDigest("candidate_ledger", "candidate.json", "b" * 64, 11),
    )
    created_at = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)
    kwargs = {
        "candidate_kind": "prompt",
        "candidate": {
            "max_output_tokens": 900,
            "temperature": 0.2,
            "template": "Prefer precise, evidence-backed answers.",
        },
        "base_revision": "a" * 40,
        "input_evidence": evidence,
        "comparison": {"status": "passed", "compatible": True, "reasons": []},
        "created_at": created_at,
    }
    first = EvaluationManifest.create(**kwargs)
    second = EvaluationManifest.create(**kwargs)

    assert first.evaluation_id == second.evaluation_id
    assert first.candidate_sha256 == second.candidate_sha256
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.status == "review_required"
    assert first.applied is False
    assert first.promoted is False

    tampered = first.to_dict()
    tampered["candidate"]["template"] = "A different prompt"
    with pytest.raises(EvaluationValidationError, match="candidate_sha256"):
        EvaluationManifest.from_dict(tampered)


def test_persist_load_list_and_path_traversal_protection(tmp_path):
    baseline, candidate = _ledger_pair(tmp_path)
    result = evaluate_candidate(
        data_dir=tmp_path / "data",
        candidate_kind="skill_policy",
        candidate={"max_injected_skills": 4, "prefer_verified": True},
        baseline_ledger_path=baseline,
        candidate_ledger_path=candidate,
        base_revision="b" * 40,
        comparison_fn=_passing_comparison,
        created_at=datetime(2026, 8, 2, 12, 30, tzinfo=UTC),
    )

    assert result.manifest_path == (
        tmp_path / "data" / "cortex" / "evaluations" / f"{result.manifest.evaluation_id}.json"
    )
    assert result.manifest_path.is_file()
    loaded = load_manifest(tmp_path / "data", result.manifest.evaluation_id)
    assert loaded.to_dict() == result.manifest.to_dict()
    assert [item.evaluation_id for item in list_manifests(tmp_path / "data")] == [
        result.manifest.evaluation_id
    ]

    repeated = evaluate_candidate(
        data_dir=tmp_path / "data",
        candidate_kind="skill_policy",
        candidate={"max_injected_skills": 4, "prefer_verified": True},
        baseline_ledger_path=baseline,
        candidate_ledger_path=candidate,
        base_revision="b" * 40,
        comparison_fn=_passing_comparison,
        created_at=datetime(2026, 8, 3, 12, 30, tzinfo=UTC),
    )
    assert repeated.manifest_path == result.manifest_path
    assert repeated.manifest.created_at == result.manifest.created_at

    with pytest.raises(EvaluationValidationError, match="evaluation_id"):
        load_manifest(tmp_path / "data", "../outside")

    stored = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    stored["created_at"] = "2026-08-03T12:30:00+00:00"
    result.manifest_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(EvaluationPersistenceError, match="integrity"):
        load_manifest(tmp_path / "data", result.manifest.evaluation_id)

    result.manifest_path.write_text(json.dumps(result.manifest.to_dict()), encoding="utf-8")
    stored = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    stored["comparison"]["status"] = "failed"
    result.manifest_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(EvaluationPersistenceError, match="integrity"):
        load_manifest(tmp_path / "data", result.manifest.evaluation_id)


def test_passed_evidence_requires_review_and_failed_evidence_is_rejected(tmp_path):
    baseline, candidate = _ledger_pair(tmp_path)
    common = {
        "data_dir": tmp_path / "data",
        "candidate_kind": "router_policy",
        "candidate": {"default_backend": "codex_cli", "prefer_local": True},
        "baseline_ledger_path": baseline,
        "candidate_ledger_path": candidate,
        "base_revision": "unknown",
    }
    passed = evaluate_candidate(comparison_fn=_passing_comparison, **common)
    failed = evaluate_candidate(comparison_fn=_failed_comparison, **common)

    assert passed.status == "review_required"
    assert passed.manifest.comparison["status"] == "passed"
    assert passed.applied is False and passed.promoted is False
    assert failed.status == "rejected"
    assert failed.manifest.comparison["status"] == "failed"
    assert failed.manifest.reasons == ("case dashboard pass rate is below the required floor",)
    assert failed.applied is False and failed.promoted is False
