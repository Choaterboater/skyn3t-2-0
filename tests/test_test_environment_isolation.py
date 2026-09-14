"""Exercise the real test fixtures under a parent factory's environment."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from skyn3t.adapters.llm import openrouter_key
from skyn3t.config.settings import Settings, get_settings

_CHILD_TESTS = '''\
import os

from skyn3t.adapters.llm import openrouter_key
from skyn3t.config.settings import Settings, get_settings


def test_defaults_are_isolated():
    settings = get_settings()
    assert settings.openrouter_api_key == ""
    assert openrouter_key(settings) == ""
    assert settings.openai_api_key == ""
    assert settings.anthropic_api_key == ""
    assert settings.llm_backend == "stub"
    assert settings.proof_install_python_deps is True
    assert settings.codegen_cli_provider == ""
    assert settings.codegen_cli_model == ""
    assert settings.repair_model_slot == ""
    assert settings.preferred_model == ""
    assert settings.generated_test_timeout == 90
    assert settings.improve_agentic_timeout == 900
    assert settings.logs_dir == settings.data_dir.parent / "logs"
    assert settings.projects_dir == settings.data_dir.parent / "Projects"
    assert settings.vector_db_path == settings.data_dir / "vector_db"
    for name in (
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL",
        "OPENROUTER_BASE_URL", "MOCK_LLM_BASE_URL", "GITHUB_TOKEN", "GH_TOKEN",
    ):
        assert name not in os.environ, name


def test_explicit_overrides_still_work(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sentinel-per-test")
    monkeypatch.setenv("SKYN3T_PROOF_INSTALL_PYTHON_DEPS", "false")
    get_settings.cache_clear()
    settings = get_settings()
    assert openrouter_key(settings) == "sentinel-per-test"
    assert settings.proof_install_python_deps is False
    explicit = Settings(openrouter_api_key="sentinel-kwarg", proof_install_python_deps=True)
    assert explicit.openrouter_api_key == "sentinel-kwarg"
    assert explicit.proof_install_python_deps is True
'''


def _run_child_pytest(tmp_path: Path, overrides: dict[str, str]) -> None:
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / "tests" / "conftest.py", tmp_path / "conftest.py")
    probe = tmp_path / "test_environment_probe.py"
    probe.write_text(_CHILD_TESTS, encoding="utf-8")
    env = {**os.environ, **overrides, "PYTHONPATH": str(root)}
    # An outer -k selection must not deselect this independent regression probe.
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--confcutdir", str(tmp_path), str(probe),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_isolate_data_dir_clears_sentinel_credentials_and_endpoints(tmp_path: Path):
    _run_child_pytest(tmp_path, {
        "SKYN3T_OPENROUTER_API_KEY": "sentinel-prefixed",
        "OPENROUTER_API_KEY": "sentinel-openrouter",
        "OPENAI_API_KEY": "sentinel-openai",
        "ANTHROPIC_API_KEY": "sentinel-anthropic",
        "GITHUB_TOKEN": "sentinel-github",
        "GH_TOKEN": "sentinel-gh",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "OPENAI_API_BASE": "http://127.0.0.1:9/v1",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "OPENROUTER_BASE_URL": "http://127.0.0.1:9/v1",
        "MOCK_LLM_BASE_URL": "http://127.0.0.1:9",
    })


def test_child_pytest_process_isolates_parent_environment_leaks(tmp_path: Path):
    _run_child_pytest(tmp_path, {
        "SKYN3T_LLM_BACKEND": "copilot_cli",
        "SKYN3T_CODEGEN_CLI_PROVIDER": "copilot",
        "SKYN3T_CODEGEN_CLI_MODEL": "sentinel-model",
        "SKYN3T_REPAIR_MODEL_SLOT": "copilot_cli",
        "SKYN3T_PREFERRED_MODEL": "sentinel/model",
        "SKYN3T_PROOF_INSTALL_PYTHON_DEPS": "false",
        "SKYN3T_GENERATED_TEST_TIMEOUT": "1200",
        "SKYN3T_IMPROVE_AGENTIC_TIMEOUT": "1200",
        "SKYN3T_LOGS_DIR": str(tmp_path / "parent-logs"),
        "SKYN3T_PROJECTS_DIR": str(tmp_path / "parent-projects"),
        "SKYN3T_VECTOR_DB_PATH": str(tmp_path / "parent-vectors"),
        "OPENROUTER_API_KEY": "sentinel-parent",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    })


def test_explicit_per_test_overrides_win(monkeypatch):
    settings = Settings(openrouter_api_key="sentinel-kwarg", proof_install_python_deps=False)
    assert settings.openrouter_api_key == "sentinel-kwarg"
    assert settings.proof_install_python_deps is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "sentinel-monkeypatch")
    get_settings.cache_clear()
    assert openrouter_key(get_settings()) == "sentinel-monkeypatch"
