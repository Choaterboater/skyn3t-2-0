"""Interactive CLI contract gate: live process and failure-mode coverage."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import skyn3t.studio.cli_playtest as cli_playtest_module
from skyn3t.studio.cli_playtest import (
    CONTRACT_FILENAME,
    check_cli_playtest,
    load_cli_playtest_contract,
)


def _project(tmp_path: Path, code: str, *, contract: dict | None = None) -> Path:
    (tmp_path / "main.py").write_text(code, encoding="utf-8")
    payload = contract or {
        "version": 1,
        "command": ["{python}", "-B", "main.py"],
        "scenarios": [
            {
                "name": "greeter",
                "steps": [
                    {"expect": "Name: ", "send": "Ada"},
                    {"expect": "Hello, Ada"},
                ],
                "exit_code": 0,
            }
        ],
    }
    (tmp_path / CONTRACT_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def _run(project: Path, **kwargs):
    return check_cli_playtest(
        project,
        stack="python_cli",
        scenario_timeout=kwargs.pop("scenario_timeout", 1.5),
        step_timeout=kwargs.pop("step_timeout", 0.5),
        **kwargs,
    )


def test_successfully_drives_prompt_response_and_records_bounded_transcript(tmp_path: Path):
    project = _project(
        tmp_path,
        "name = input('Name: ')\nprint(f'Hello, {name}', flush=True)\n",
    )

    verdict = _run(project)

    assert verdict.ok, verdict.to_dict()
    scenario = verdict.checked["scenarios"][0]
    assert scenario["status"] == "passed"
    assert scenario["exit_code"] == 0
    assert scenario["steps_completed"] == 2
    assert "Name: " in scenario["transcript"]
    assert "Hello, Ada" in scenario["transcript"]
    assert scenario["truncated"] is False


@pytest.mark.parametrize(
    ("code", "expected_issue"),
    [
        ("input('Different prompt: ')\n", "timed out waiting"),
        ("input('Name: ')\nprint('Wrong person', flush=True)\n", "exited before expected output"),
    ],
)
def test_wrong_prompt_or_output_is_a_real_defect(
    tmp_path: Path, code: str, expected_issue: str
):
    verdict = _run(_project(tmp_path, code), scenario_timeout=0.35, step_timeout=0.15)

    assert not verdict.skipped
    assert not verdict.ok
    assert any(expected_issue in issue for issue in verdict.issues), verdict.to_dict()


def test_process_that_does_not_exit_hits_whole_scenario_deadline(tmp_path: Path):
    code = "import time\ninput('Name: ')\nprint('Hello, Ada', flush=True)\ntime.sleep(30)\n"

    verdict = _run(_project(tmp_path, code), scenario_timeout=0.3, step_timeout=0.15)

    assert not verdict.skipped
    assert any("timed out waiting for process exit" in issue for issue in verdict.issues)
    assert verdict.checked["scenarios"][0]["status"] == "failed"


def test_process_cannot_hang_gate_by_closing_output_before_exit(tmp_path: Path):
    code = "import os, time\nos.close(1)\nos.close(2)\ntime.sleep(30)\n"
    contract = {
        "version": 1,
        "command": ["{python}", "-B", "main.py"],
        "scenarios": [{"name": "closed-output", "steps": [], "exit_code": 0}],
    }

    verdict = _run(
        _project(tmp_path, code, contract=contract),
        scenario_timeout=0.3,
        step_timeout=0.15,
    )

    assert not verdict.skipped
    assert any("timed out waiting for process exit" in issue for issue in verdict.issues)


def test_early_exit_before_prompt_is_a_real_defect(tmp_path: Path):
    verdict = _run(_project(tmp_path, "raise SystemExit(0)\n"))

    assert not verdict.skipped
    assert any("exited before expected output 'Name: '" in issue for issue in verdict.issues)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"version": 2, "command": ["{python}"], "scenarios": []}),
        json.dumps({"version": 1, "command": "python", "scenarios": [{}]}),
    ],
)
def test_malformed_declared_contract_is_an_issue(tmp_path: Path, payload: str):
    (tmp_path / CONTRACT_FILENAME).write_text(payload, encoding="utf-8")

    verdict = _run(tmp_path)

    assert not verdict.skipped
    assert verdict.issues
    assert "invalid" in verdict.issues[0]


def test_contract_path_and_executable_may_not_escape_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes"):
        load_cli_playtest_contract(project, "../outside.json")

    contract = {
        "version": 1,
        "command": ["../outside-program"],
        "scenarios": [{"name": "escape", "steps": [], "exit_code": 0}],
    }
    verdict = _run(_project(project, "", contract=contract))
    assert not verdict.skipped
    assert any("escapes the project" in issue for issue in verdict.issues)


def test_contract_file_may_not_be_a_symlink(tmp_path: Path):
    target = tmp_path / "real-contract.json"
    target.write_text(
        json.dumps({"version": 1, "command": ["{python}"], "scenarios": []}),
        encoding="utf-8",
    )
    (tmp_path / CONTRACT_FILENAME).symlink_to(target)

    verdict = _run(tmp_path)

    assert not verdict.skipped
    assert any("non-symlink" in issue for issue in verdict.issues)


def test_host_secrets_are_removed_and_never_appear_in_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "host-super-secret-value-928374"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    code = (
        "import os\n"
        "print('secret=' + os.environ.get('OPENAI_API_KEY', 'missing'), flush=True)\n"
    )
    contract = {
        "version": 1,
        "command": ["{python}", "-B", "main.py"],
        "env": {"SERVICE_TOKEN": "contract-should-also-be-filtered"},
        "scenarios": [
            {
                "name": "environment",
                "steps": [{"expect": "secret=missing"}],
                "exit_code": 0,
            }
        ],
    }

    verdict = _run(_project(tmp_path, code, contract=contract))

    assert verdict.ok, verdict.to_dict()
    serialized = json.dumps(verdict.to_dict())
    assert secret not in serialized
    assert "contract-should-also-be-filtered" not in serialized
    assert verdict.checked["filtered_env"] == ["SERVICE_TOKEN"]


def test_dangerous_host_environment_is_removed_from_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PYTHONPATH", "host-import-injection")
    monkeypatch.setenv("LD_PRELOAD", "host-loader-injection")
    monkeypatch.setenv("NODE_OPTIONS", "--require=host-module")
    code = (
        "import os\n"
        "names = ('PYTHONPATH', 'LD_PRELOAD', 'NODE_OPTIONS')\n"
        "print('env=' + ','.join(os.environ.get(name, 'missing') for name in names), "
        "flush=True)\n"
    )
    contract = {
        "version": 1,
        "command": ["{python}", "-B", "main.py"],
        "scenarios": [
            {
                "name": "host-env",
                "steps": [{"expect": "env=missing,missing,missing"}],
                "exit_code": 0,
            }
        ],
    }

    verdict = _run(_project(tmp_path, code, contract=contract))

    assert verdict.ok, verdict.to_dict()
    serialized = json.dumps(verdict.to_dict())
    assert "host-import-injection" not in serialized
    assert "host-loader-injection" not in serialized


def test_output_limit_stops_chatty_process_and_bounds_transcript(tmp_path: Path):
    code = "import sys, time\nwhile True:\n print('x' * 200, flush=True)\n time.sleep(0.001)\n"
    contract = {
        "version": 1,
        "command": ["{python}", "-B", "main.py"],
        "scenarios": [
            {"name": "chatty", "steps": [{"expect": "never-arrives"}], "exit_code": 0}
        ],
    }

    verdict = _run(
        _project(tmp_path, code, contract=contract),
        scenario_timeout=1.0,
        step_timeout=0.8,
        max_transcript_chars=512,
    )

    scenario = verdict.checked["scenarios"][0]
    assert any("output exceeded" in issue for issue in verdict.issues)
    assert scenario["truncated"] is True
    assert len(scenario["transcript"]) <= 512


def test_missing_contract_and_missing_pexpect_are_soft_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    absent = _run(tmp_path)
    assert absent.skipped and absent.gaps() == []

    project = _project(tmp_path, "print('unused')\n")
    monkeypatch.setattr(cli_playtest_module, "PopenSpawn", None)
    unavailable = _run(project)
    assert unavailable.skipped and unavailable.gaps() == []
    assert "pexpect" in unavailable.reason


@pytest.mark.parametrize("executable", ["powershell", "cmd", "sh"])
def test_contract_cannot_invoke_bare_host_executables(tmp_path: Path, executable: str):
    contract = {
        "version": 1,
        "command": [executable],
        "scenarios": [{"name": "host-tool", "steps": [], "exit_code": 0}],
    }

    verdict = _run(_project(tmp_path, "", contract=contract))

    assert not verdict.skipped
    assert any("relative executable" in issue for issue in verdict.issues)


def test_contract_rejects_absolute_executable_and_placeholder_injection(tmp_path: Path):
    absolute = {
        "version": 1,
        "command": [sys.executable],
        "scenarios": [{"name": "absolute", "steps": [], "exit_code": 0}],
    }
    absolute_verdict = _run(_project(tmp_path, "", contract=absolute))
    assert any("relative executable" in issue for issue in absolute_verdict.issues)

    injected = {
        "version": 1,
        "command": ["{python}", "--root={project}"],
        "scenarios": [{"name": "placeholder", "steps": [], "exit_code": 0}],
    }
    injected_verdict = _run(_project(tmp_path, "", contract=injected))
    assert any("complete command token" in issue for issue in injected_verdict.issues)


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "PATHEXT",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "COMSPEC",
        "SHELL",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "NODE_OPTIONS",
        "SystemRoot",
        "windir",
    ],
)
def test_contract_rejects_process_loader_environment_overrides(tmp_path: Path, name: str):
    contract = {
        "version": 1,
        "command": ["{python}", "main.py"],
        "env": {name: "untrusted"},
        "scenarios": [{"name": "env", "steps": [], "exit_code": 0}],
    }

    verdict = _run(_project(tmp_path, "", contract=contract))

    assert not verdict.skipped
    assert any("process-loader" in issue for issue in verdict.issues)


def test_missing_in_project_compiled_executable_is_a_defect(tmp_path: Path):
    contract = {
        "version": 1,
        "command": [".build/release/MissingCli"],
        "scenarios": [{"name": "compiled", "steps": [], "exit_code": 0}],
    }

    verdict = _run(_project(tmp_path, "", contract=contract))

    assert not verdict.skipped
    assert any("does not exist" in issue for issue in verdict.issues)


def test_python_placeholder_uses_current_interpreter(tmp_path: Path):
    contract = load_cli_playtest_contract(
        _project(tmp_path, "print('ok')\n", contract={
            "version": 1,
            "command": ["{python}", "main.py"],
            "scenarios": [{"name": "python", "steps": [{"expect": "ok"}]}],
        })
    )
    assert contract is not None
    assert contract.command[0] == "{python}"
    assert sys.executable
    assert os.path.isfile(sys.executable)
