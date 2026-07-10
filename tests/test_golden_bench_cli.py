"""CLI and workflow contracts for repeatable golden app benchmarks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml
from typer.testing import CliRunner

import skyn3t.studio.golden_bench as golden_bench
from skyn3t.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "golden-bench.yml"
GUIDE_PATH = REPO_ROOT / "docs" / "bench" / "RUNNING_GOLDEN.md"
cli_runner = CliRunner()


def _workflow() -> dict:
    # BaseLoader preserves GitHub's `on` key instead of treating it as YAML 1.1 bool.
    parsed = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def _step_script(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def test_golden_workflow_parses_and_has_all_entry_points() -> None:
    workflow = _workflow()

    assert set(workflow["on"]) == {"pull_request", "schedule", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"validate", "run"}


def test_pull_request_job_only_validates_and_never_builds() -> None:
    workflow = _workflow()
    validate_script = _step_script(workflow["jobs"]["validate"])
    run_job = workflow["jobs"]["run"]

    assert "bench golden validate" in validate_script
    assert "bench golden run" not in validate_script
    assert run_job["if"] == "github.event_name != 'pull_request'"
    assert run_job["needs"] == "validate"


def test_scheduled_job_is_keyless_deterministic_and_always_uploads() -> None:
    workflow = _workflow()
    run_job = workflow["jobs"]["run"]
    env = run_job["env"]
    script = _step_script(run_job)

    assert run_job["runs-on"] == "macos-latest"
    assert env["SKYN3T_LLM_BACKEND"] == "stub"
    assert env["SKYN3T_EXECUTION_BACKEND"] == "inline"
    assert env["SKYN3T_ALLOW_REMOTE_DEPLOY"] == "false"
    assert workflow["env"]["GOLDEN_SUITE"] == "skyn3t/benchmarks/golden-v1.json"
    assert "bench golden run" in script
    assert "bench golden compare" in script
    assert "swift --version" in script and "xcodebuild -version" in script
    assert "secrets." not in WORKFLOW_PATH.read_text(encoding="utf-8")

    run_step = next(step for step in run_job["steps"] if step.get("id") == "golden_run")
    compare_step = next(
        step for step in run_job["steps"] if step.get("id") == "golden_compare"
    )
    assert run_step["continue-on-error"] == "true"
    assert compare_step["continue-on-error"] == "true"
    assert compare_step["if"] == "always()"

    upload = next(
        step
        for step in run_job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert upload["if"] == "always()"
    assert upload["uses"] == "actions/upload-artifact@v7.0.1"
    assert upload["with"]["path"] == "artifacts/golden/"

    enforce = next(step for step in run_job["steps"] if step.get("name") == "Enforce golden result")
    assert enforce["if"] == "always()"
    assert "GOLDEN_RUN_OUTCOME" in enforce["run"]
    assert "GOLDEN_COMPARE_OUTCOME" in enforce["run"]


def test_operator_guide_pins_safety_and_baseline_rules() -> None:
    guide = GUIDE_PATH.read_text(encoding="utf-8")

    assert "skyn3t bench golden validate" in guide
    assert "skyn3t bench golden run" in guide
    assert "skyn3t bench golden compare" in guide
    assert "stub" in guide and "inline" in guide
    assert "Do not hand-author" in guide
    assert "partial/error ledger" in guide
    assert "never replace" in guide


def test_golden_cli_exposes_all_documented_commands() -> None:
    result = cli_runner.invoke(app, ["bench", "golden", "--help"])

    assert result.exit_code == 0, result.output
    assert "validate" in result.output
    assert "run" in result.output
    assert "compare" in result.output


def test_golden_validate_cli_reports_digest_and_rejects_bad_input(tmp_path: Path) -> None:
    valid = cli_runner.invoke(app, ["bench", "golden", "validate"])
    assert valid.exit_code == 0, valid.output
    assert "golden-v1" in valid.output
    assert "SHA-256" in valid.output

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    invalid = cli_runner.invoke(
        app,
        ["bench", "golden", "validate", "--suite", str(bad)],
    )
    assert invalid.exit_code == 2
    assert "Invalid golden suite" in invalid.output


def test_golden_run_cli_passes_isolated_options_and_exits_one_on_contract_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    async def fake_run(suite, **kwargs):
        calls.append({"suite": suite, **kwargs})
        return SimpleNamespace(
            status="completed",
            summary=SimpleNamespace(
                overall=SimpleNamespace(
                    passed=1,
                    attempts=1,
                    failed=0,
                    wilson=SimpleNamespace(low=0.2065, high=1.0),
                )
            ),
        )

    monkeypatch.setattr("skyn3t.cli.main._golden_run_async", fake_run)
    result = cli_runner.invoke(
        app,
        [
            "bench",
            "golden",
            "run",
            "--out",
            str(tmp_path / "run.json"),
            "--report",
            str(tmp_path / "run.md"),
            "--work-root",
            str(tmp_path / "work"),
            "--repeats",
            "1",
            "--seed",
            "41",
            "--execution-backend",
            "INLINE",
            "--llm-backend",
            "STUB",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["repeats"] == 1
    assert calls[0]["seed"] == 41
    assert calls[0]["execution_backend"] == "inline"
    assert calls[0]["llm_backend"] == "stub"
    assert calls[0]["work_root"] == tmp_path / "work"

    async def failed_run(suite, **kwargs):
        return SimpleNamespace(
            status="completed",
            summary=SimpleNamespace(
                overall=SimpleNamespace(
                    passed=0,
                    attempts=1,
                    failed=1,
                    wilson=SimpleNamespace(low=0.0, high=0.7935),
                )
            ),
        )

    monkeypatch.setattr("skyn3t.cli.main._golden_run_async", failed_run)
    failed = cli_runner.invoke(
        app,
        ["bench", "golden", "run", "--repeats", "1"],
    )
    assert failed.exit_code == 1
    assert "0/1 passed" in failed.output


def test_golden_run_cli_bounds_repetitions_before_building(monkeypatch) -> None:
    async def should_not_run(*args, **kwargs):  # pragma: no cover - assertion sentinel
        raise AssertionError("invalid CLI arguments reached the build loop")

    monkeypatch.setattr("skyn3t.cli.main._golden_run_async", should_not_run)
    result = cli_runner.invoke(
        app,
        ["bench", "golden", "run", "--repeats", "11"],
    )

    assert result.exit_code == 2


def test_golden_compare_cli_maps_gate_and_input_statuses_to_exit_codes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def result_for(status: str):
        return SimpleNamespace(status=status, reasons=[f"{status} evidence"])

    out = tmp_path / "comparison.json"
    report = tmp_path / "comparison.md"
    args = [
        "bench",
        "golden",
        "compare",
        "--baseline",
        str(tmp_path / "baseline.json"),
        "--candidate",
        str(tmp_path / "candidate.json"),
        "--out",
        str(out),
        "--report",
        str(report),
    ]

    monkeypatch.setattr(
        golden_bench, "compare_ledger_files", lambda *args, **kwargs: result_for("passed")
    )
    assert cli_runner.invoke(app, args).exit_code == 0

    monkeypatch.setattr(
        golden_bench, "compare_ledger_files", lambda *args, **kwargs: result_for("failed")
    )
    assert cli_runner.invoke(app, args).exit_code == 1

    monkeypatch.setattr(
        golden_bench, "compare_ledger_files", lambda *args, **kwargs: result_for("incompatible")
    )
    assert cli_runner.invoke(app, args).exit_code == 1

    monkeypatch.setattr(
        golden_bench, "compare_ledger_files", lambda *args, **kwargs: result_for("error")
    )
    assert cli_runner.invoke(app, args).exit_code == 2
