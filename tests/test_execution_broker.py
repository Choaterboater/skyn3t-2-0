"""Exercise the broker against the sandbox's real argv-only host backend."""

from __future__ import annotations

import json
import shlex
import sys
from unittest.mock import AsyncMock

import pytest

from skyn3t.config.settings import Settings
from skyn3t.security.execution_broker import Disposition, ExecutionBroker


def _broker(tmp_path, **overrides):
    return ExecutionBroker(settings=Settings(
        execution_backend="inline",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        **overrides,
    ))


def test_host_proof_executes_argv_without_shell_interpretation(tmp_path):
    arguments = ["a quoted value", "semi;literal", r"C:\Program Files\SkyN3t"]
    command = shlex.join([
        sys.executable, "-B", "-c",
        "import json, sys; print(json.dumps(sys.argv[1:]))",
        *arguments,
    ])
    with pytest.warns(RuntimeWarning, match="SANDBOX FALLBACK"):
        receipt = _broker(tmp_path).run_generated_code(
            command, str(tmp_path), stack="python", timeout=20,
        )
    assert receipt.ok, receipt.text or receipt.warning
    assert receipt.exit_code == 0
    assert receipt.backend == "subprocess"
    assert json.loads(receipt.stdout) == arguments


@pytest.mark.parametrize(
    ("command", "message"),
    [("python '", "quotation"), ("  ", "command must not be empty")],
)
def test_malformed_command_reports_error_without_execution(tmp_path, monkeypatch, command, message):
    broker = _broker(tmp_path)
    run = AsyncMock()
    monkeypatch.setattr(broker.sandbox, "run", run)
    receipt = broker.run_generated_code(command, str(tmp_path))
    assert receipt.disposition == Disposition.ERROR
    assert receipt.exit_code is None
    assert message in receipt.text.lower()
    run.assert_not_called()


def test_broker_errors_are_visible_and_secret_scrubbed(tmp_path, monkeypatch):
    credential = "sentinel-broker-credential"
    broker = _broker(tmp_path, openrouter_api_key=credential)
    monkeypatch.setattr(
        broker.sandbox, "run",
        AsyncMock(side_effect=OSError(f"cannot execute with {credential}")),
    )
    receipt = broker.run_generated_code("python -V", str(tmp_path))
    assert receipt.disposition == Disposition.ERROR
    assert "cannot execute" in receipt.text
    assert credential not in receipt.text
    assert credential not in receipt.warning
