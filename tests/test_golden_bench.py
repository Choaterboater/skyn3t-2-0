"""Executable contracts for the strict golden benchmark core."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.studio.golden_bench import (
    GoldenBenchError,
    GoldenSuite,
    benchmark_settings_profile,
    build_run_metadata,
    compare_ledger_files,
    compare_ledgers,
    deterministic_slug,
    isolated_settings,
    load_ledger,
    load_suite,
    run_golden,
    suite_digest,
    wilson_interval,
)


def _suite_dict() -> dict:
    return {
        "schema_version": 1,
        "suite_id": "focused-v1",
        "name": "Focused golden suite",
        "description": "Small deterministic contracts for benchmark tests.",
        "cases": [
            {
                "id": "business-site",
                "brief": (
                    "Build a concrete static business website with navigation services contact "
                    "details metadata and accessible calls to action."
                ),
                "stack": "static",
                "tags": ["web", "static"],
                "expectations": {
                    "expected_stack": "static",
                    "min_score": 60,
                    "min_intent_score": 80,
                    "required_gates": ["proof", "security_check", "seo"],
                    "required_artifacts": ["index.html"],
                },
            },
            {
                "id": "report-cli",
                "brief": (
                    "Build a Python command line report generator with validated input explicit "
                    "output deterministic formatting and useful errors."
                ),
                "stack": "python",
                "tags": ["cli", "python"],
                "expectations": {
                    "expected_stack": "python",
                    "min_score": 60,
                    "min_intent_score": 80,
                    "required_gates": ["proof", "cli_check"],
                    "required_artifacts": ["main.py"],
                },
            },
        ],
    }


def _suite() -> GoldenSuite:
    return GoldenSuite.model_validate(_suite_dict(), strict=True)


def _metadata(suite: GoldenSuite, *, seed: int = 17, repeats: int = 2):
    return build_run_metadata(
        suite,
        seed=seed,
        repeats=repeats,
        llm_backend="stub",
        execution_backend="inline",
        git_commit="unknown",
        git_dirty=False,
        git_status_digest="0" * 64,
        platform_value="test-platform",
        system="TestOS",
        machine="test-machine",
        python_version="3.12.0",
        python_implementation="CPython",
    )


def _outcome(case, context, *, pass_gates: bool = True):
    project = context.workspace_dir / "delivered"
    project.mkdir()
    artifact = case.expectations.required_artifacts[0]
    target = project / artifact
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("substantive", encoding="utf-8")
    extra = {
        "intent": {"score": 90.0},
        "proof": {"passed": True},
    }
    if case.stack == "static":
        extra["security_check"] = {"ok": True, "skipped": False}
        extra["seo"] = {"ok": pass_gates, "skipped": not pass_gates}
    else:
        extra["cli_check"] = {"ok": True, "skipped": False}
    return SimpleNamespace(
        build_id=f"build-{case.id}-{context.repeat}",
        slug=context.slug,
        status="completed",
        verdict="go",
        score=90.0,
        stack=case.stack,
        project_dir=str(project),
        cost_usd=0.0,
        manifest={"extra": extra},
    )


def test_packaged_suite_loads_strictly_with_stable_digest() -> None:
    first = load_suite()
    second = load_suite()

    assert first.suite_id == "golden-v1"
    assert len(first.cases) == 31
    assert suite_digest(first) == suite_digest(second)
    assert suite_digest(first) == "96dd70d10360752da2aa4c121e8b62f54144bd1ca74f5f9302653acb66ece65e"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["cases"][0]["expectations"]["required_artifacts"].append("../escape.py"),
        lambda data: data["cases"][0]["expectations"].update(required_gates=["proof"]),
        lambda data: data["cases"][0]["expectations"].update(min_score=59),
        lambda data: data["cases"][0].update(unexpected=True),
    ],
)
def test_suite_rejects_unsafe_or_weakened_contracts(tmp_path: Path, mutation) -> None:
    data = _suite_dict()
    mutation(data)
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(GoldenBenchError):
        load_suite(path)


def test_suite_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")

    with pytest.raises(GoldenBenchError, match="duplicate JSON key"):
        load_suite(path)


def test_wilson_interval_has_expected_boundary_behavior() -> None:
    assert wilson_interval(0, 0) == (0.0, 0.0)
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.236593)
    assert high == pytest.approx(0.763407)
    with pytest.raises(GoldenBenchError):
        wilson_interval(2, 1)


def test_metadata_rejects_a_weakened_safety_profile() -> None:
    with pytest.raises(GoldenBenchError, match="required controls"):
        build_run_metadata(
            _suite(),
            seed=17,
            repeats=1,
            llm_backend="stub",
            execution_backend="inline",
            safety_profile={"allow_remote_deploy": True},
        )


@pytest.mark.asyncio
async def test_run_checkpoints_repeats_and_records_real_expectation_failure(tmp_path: Path) -> None:
    suite = _suite()
    out = tmp_path / "run.json"
    report = tmp_path / "run.md"
    observed_checkpoints: list[tuple[str, int]] = []

    async def build(case, context):
        checkpoint = json.loads(out.read_text(encoding="utf-8"))
        observed_checkpoints.append((checkpoint["status"], len(checkpoint["attempts"])))
        fail_static_repeat = case.id == "business-site" and context.repeat == 2
        return _outcome(case, context, pass_gates=not fail_static_repeat)

    ledger = await run_golden(
        suite,
        build,
        out_path=out,
        report_path=report,
        work_root=tmp_path / "work",
        repeats=2,
        seed=17,
        metadata=_metadata(suite),
    )

    assert observed_checkpoints == [("partial", 0), ("partial", 1), ("partial", 2), ("partial", 3)]
    assert ledger.status == "completed"
    assert ledger.summary.overall.attempts == 4
    assert ledger.summary.overall.passed == 3
    assert ledger.summary.by_stack["static"].pass_rate == 0.5
    assert ledger.summary.by_case["report-cli"].pass_rate == 1.0
    assert ledger.attempts[1].failed_expectations == ["gate:seo"]
    assert ledger.attempts[0].slug == deterministic_slug(
        ledger.attempts[0].case_id,
        ledger.attempts[0].repeat,
        ledger.attempts[0].seed,
    )
    assert len({attempt.slug for attempt in ledger.attempts}) == 4
    assert ledger.case_check_names["business-site"] == [
        "project_isolation",
        "build_slug",
        "build_status",
        "verdict",
        "stack",
        "score",
        "intent_score",
        "gate:proof",
        "gate:security_check",
        "gate:seo",
        "artifact:index.html",
    ]
    assert load_ledger(out).model_dump(mode="json") == ledger.model_dump(mode="json")
    markdown = report.read_text(encoding="utf-8")
    assert "Wilson 95% interval" in markdown
    assert "gate:seo" in markdown


@pytest.mark.asyncio
async def test_interruption_leaves_a_valid_partial_ledger(tmp_path: Path) -> None:
    suite = _suite()
    out = tmp_path / "partial.json"

    async def cancel(_case, _context):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_golden(
            suite,
            cancel,
            out_path=out,
            work_root=tmp_path / "work",
            repeats=1,
            seed=17,
            metadata=_metadata(suite, repeats=1),
        )

    ledger = load_ledger(out)
    assert ledger.status == "partial"
    assert ledger.completed_at is None
    assert len(ledger.attempts) == 1
    assert ledger.attempts[0].status == "error"
    assert ledger.summary.overall.errors == 1


@pytest.mark.asyncio
async def test_fatal_setup_failure_leaves_a_valid_error_ledger(tmp_path: Path) -> None:
    suite = _suite()
    out = tmp_path / "error.json"
    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("blocked", encoding="utf-8")

    async def should_not_build(_case, _context):  # pragma: no cover - setup fails first
        raise AssertionError("build should not start")

    with pytest.raises(OSError):
        await run_golden(
            suite,
            should_not_build,
            out_path=out,
            work_root=blocked_root,
            repeats=1,
            seed=17,
            metadata=_metadata(suite, repeats=1),
        )

    ledger = load_ledger(out)
    assert ledger.status == "error"
    assert ledger.attempts == []
    assert ledger.error


@pytest.mark.asyncio
async def test_loader_rejects_tampered_summary_and_fingerprint(tmp_path: Path) -> None:
    suite = _suite()

    async def build(case, context):
        return _outcome(case, context)

    original = tmp_path / "original.json"
    await run_golden(
        suite,
        build,
        out_path=original,
        work_root=tmp_path / "work",
        repeats=1,
        seed=17,
        metadata=_metadata(suite, repeats=1),
    )
    data = json.loads(original.read_text(encoding="utf-8"))

    summary_path = tmp_path / "bad-summary.json"
    data["summary"]["overall"]["pass_rate"] = 0.0
    summary_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GoldenBenchError, match="summary"):
        load_ledger(summary_path)

    data = json.loads(original.read_text(encoding="utf-8"))
    fingerprint_path = tmp_path / "bad-fingerprint.json"
    data["metadata"]["fingerprint"] = "0" * 64
    fingerprint_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GoldenBenchError, match="fingerprint"):
        load_ledger(fingerprint_path)

    data = json.loads(original.read_text(encoding="utf-8"))
    evidence_path = tmp_path / "bad-evidence.json"
    data["attempts"][0]["checks"][0]["passed"] = False
    evidence_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GoldenBenchError, match="passing attempt"):
        load_ledger(evidence_path)

    data = json.loads(original.read_text(encoding="utf-8"))
    missing_checks_path = tmp_path / "missing-checks.json"
    data["attempts"][0]["checks"] = []
    missing_checks_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GoldenBenchError, match="exactly match the contract"):
        load_ledger(missing_checks_path)

    data = json.loads(original.read_text(encoding="utf-8"))
    duplicate_checks_path = tmp_path / "duplicate-checks.json"
    data["attempts"][0]["checks"].append(data["attempts"][0]["checks"][-1])
    duplicate_checks_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GoldenBenchError, match="exactly match the contract"):
        load_ledger(duplicate_checks_path)


@pytest.mark.asyncio
async def test_comparison_enforces_compatibility_and_regression_thresholds(tmp_path: Path) -> None:
    suite = _suite()

    async def passing(case, context):
        return _outcome(case, context)

    async def regressed(case, context):
        return _outcome(case, context, pass_gates=case.id != "business-site")

    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline = await run_golden(
        suite,
        passing,
        out_path=baseline_path,
        work_root=tmp_path / "work",
        repeats=1,
        seed=17,
        metadata=_metadata(suite, repeats=1),
    )
    candidate = await run_golden(
        suite,
        regressed,
        out_path=candidate_path,
        work_root=tmp_path / "work",
        repeats=1,
        seed=17,
        metadata=_metadata(suite, repeats=1),
    )

    comparison = compare_ledgers(baseline, candidate)
    assert comparison.status == "failed"
    assert comparison.compatible is True
    assert comparison.suite_pass_rate_drop == 0.5
    assert any("business-site" in reason for reason in comparison.reasons)

    incompatible_path = tmp_path / "other-seed.json"
    other = await run_golden(
        suite,
        passing,
        out_path=incompatible_path,
        work_root=tmp_path / "work",
        repeats=1,
        seed=18,
        metadata=_metadata(suite, seed=18, repeats=1),
    )
    incompatible = compare_ledgers(baseline, other)
    assert incompatible.status == "incompatible"
    assert any("seeds differ" in reason for reason in incompatible.reasons)


def test_compare_malformed_ledger_still_writes_error_evidence(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    out = tmp_path / "comparison.json"
    report = tmp_path / "comparison.md"

    comparison = compare_ledger_files(
        bad,
        bad,
        out_path=out,
        report_path=report,
    )

    assert comparison.status == "error"
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "error"
    assert "Gate findings" in report.read_text(encoding="utf-8")


def test_isolated_settings_move_all_mutable_state_and_disable_side_effects(tmp_path: Path) -> None:
    base_root = tmp_path / "base"
    base = Settings(
        data_dir=base_root / "data",
        projects_dir=base_root / "projects",
        logs_dir=base_root / "logs",
        vector_db_path=base_root / "vectors",
        db_url=f"sqlite+aiosqlite:///{(base_root / 'base.db').as_posix()}",
        allow_remote_deploy=True,
        asset_gen=True,
        autonomous_learning=True,
        bench_capture_failures=True,
    )

    isolated = isolated_settings(
        base,
        tmp_path / "attempt",
        llm_backend="stub",
        execution_backend="inline",
    )

    assert isolated.projects_dir == (tmp_path / "attempt").resolve() / "projects"
    assert isolated.data_dir == (tmp_path / "attempt").resolve() / "state"
    assert isolated.db_url.endswith("/attempt/state/skyn3t.db")
    assert isolated.allow_remote_deploy is False
    assert isolated.asset_gen is False
    assert isolated.autonomous_learning is False
    assert isolated.bench_capture_failures is False
    assert isolated.best_of_n == 1
    assert isolated.parallel_code_slices is False
    assert isolated.game_art_source == "offline"
    assert isolated.game_visual_check_enabled is False
    assert isolated.qa_playtest_enabled is False
    assert isolated.security_check_enabled is True
    assert isolated.run_generated_tests is True
    assert isolated.run_generated_build is True
    # The bench must measure the blocking posture, never the lab default.
    assert isolated.build_posture == "release"
    assert isolated.blocking_gates == ""
    assert isolated.github_token == ""
    assert isolated.skills_hub_paths == ""
    assert isolated.openrouter_api_key == ""
    assert base.allow_remote_deploy is True

    profile = benchmark_settings_profile(base)
    assert profile["daily_usd_cap"] == base.daily_usd_cap
    assert profile["daily_token_cap"] == base.daily_token_cap
    assert profile["game_art_source"] == "offline"


def test_no_credential_is_ever_recorded_into_a_benchmark_profile(tmp_path):
    """No secret may reach artifacts/golden/run.json.

    The name filter matched `<vendor>_token` but not `<vendor>_api_token`, so
    fly / cloudflare / replicate tokens were written verbatim. Asserted over
    real Settings values rather than a hand-list so a NEW token field cannot
    quietly reappear in the profile.
    """
    secret = "SENTINEL-DO-NOT-RECORD"
    base = Settings(
        fly_api_token=secret, vercel_token=secret, cloudflare_api_token=secret,
        netlify_auth_token=secret, railway_token=secret, render_api_key=secret,
        replicate_api_token=secret, github_token=secret, openrouter_api_key=secret,
    )

    profile = benchmark_settings_profile(base, llm_backend="stub")

    flat = json.dumps(profile)
    assert secret not in flat
    # …and a genuine, non-secret control is still recorded.
    assert "daily_token_cap" in profile


def test_isolated_settings_blanks_every_deploy_token(tmp_path):
    """Bench subprocesses must not inherit live deploy credentials.

    Only 3 of the 6 were blanked, so Netlify/Railway/Render creds rode into
    every bench build with allow_remote_deploy=False as the sole defence.
    """
    secret = "SENTINEL-DO-NOT-LEAK"
    base = Settings(
        fly_api_token=secret, vercel_token=secret, cloudflare_api_token=secret,
        netlify_auth_token=secret, railway_token=secret, render_api_key=secret,
        replicate_api_token=secret,
    )

    isolated = isolated_settings(
        base, tmp_path, llm_backend="stub", execution_backend="inline"
    )

    assert isolated.deploy_tokens == {}
    assert isolated.replicate_api_token == ""


@pytest.mark.parametrize(
    ("setting", "first", "second"),
    [
        ("game_art_enabled", True, False),
        ("reward_hardening", True, False),
        ("sandbox_hardening", True, False),
        ("sandbox_drop_caps", True, False),
        ("llm_fallback_enabled", True, False),
        ("llm_max_retries", 3, 7),
        ("agentic_context_editing", True, False),
        ("agentic_context_budget_bytes", 200_000, 350_000),
        ("agentic_context_keep_last", 6, 9),
    ],
)
def test_settings_profile_fingerprints_material_host_controls(
    setting: str, first, second
) -> None:
    first_profile = benchmark_settings_profile(Settings(**{setting: first}), llm_backend="stub")
    second_profile = benchmark_settings_profile(Settings(**{setting: second}), llm_backend="stub")

    assert first_profile[setting] == first
    assert second_profile[setting] == second
    assert first_profile != second_profile
    assert all(not value for value in first_profile["provider_access"].values())
    common = {
        "seed": 17,
        "repeats": 1,
        "llm_backend": "stub",
        "execution_backend": "inline",
        "git_commit": "unknown",
        "git_dirty": False,
        "git_status_digest": "0" * 64,
        "platform_value": "test-platform",
        "system": "TestOS",
        "machine": "test-machine",
        "python_version": "3.12.0",
        "python_implementation": "CPython",
    }
    first_metadata = build_run_metadata(_suite(), safety_profile=first_profile, **common)
    second_metadata = build_run_metadata(_suite(), safety_profile=second_profile, **common)
    assert first_metadata.fingerprint != second_metadata.fingerprint


def test_git_provenance_is_recorded_but_excluded_from_comparison_fingerprint() -> None:
    suite = _suite()
    common = {
        "seed": 17,
        "repeats": 1,
        "llm_backend": "stub",
        "execution_backend": "inline",
        "platform_value": "test-platform",
        "system": "TestOS",
        "machine": "test-machine",
        "python_version": "3.12.0",
        "python_implementation": "CPython",
    }
    clean = build_run_metadata(
        suite,
        **common,
        git_commit="a" * 40,
        git_dirty=False,
        git_status_digest="1" * 64,
    )
    dirty = build_run_metadata(
        suite,
        **common,
        git_commit="b" * 40,
        git_dirty=True,
        git_status_digest="2" * 64,
    )

    assert clean.git_dirty is False
    assert dirty.git_dirty is True
    assert clean.git_status_digest != dirty.git_status_digest
    assert clean.fingerprint == dirty.fingerprint
    assert "git_commit" not in clean.fingerprint_inputs
    assert "git_status_digest" not in clean.fingerprint_inputs
