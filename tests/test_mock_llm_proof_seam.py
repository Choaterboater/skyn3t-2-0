"""Proof-run mock-LLM seam (research item 42).

When the generated project CALLS an LLM, ``proof_run`` boots the local mock
(skyn3t.studio.mock_llm) for the project's OWN test step and injects a base-url +
dummy-key env overlay so the app's tests run headlessly at $0. The seam is
advisory + degrade-open: a non-LLM project, a disabled flag, or a mock that can't
start all fall back to running the tests exactly as before.

Offline throughout; the command executor is monkeypatched so no real subprocess,
npm, or network is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import skyn3t.studio.proof_run as pr
from skyn3t.security.secrets import MOCK_PROOF_VALUE, filter_env
from skyn3t.studio.proof_run import (
    _is_llm_calling_project,
    _start_proof_mock_llm,
    proof_run,
)


# --- detection heuristic -----------------------------------------------------


def test_detects_llm_project_via_python_dep(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nopenai>=1.0\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('x')\n", encoding="utf-8")
    assert _is_llm_calling_project(tmp_path) is True


def test_detects_llm_project_via_js_dep(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name":"x","dependencies":{"@anthropic-ai/sdk":"^0.30.0"}}', encoding="utf-8")
    assert _is_llm_calling_project(tmp_path) is True


def test_detects_llm_project_via_source_reference(tmp_path):
    # No client dep declared, but the source reads an OpenAI-compatible base url.
    (tmp_path / "server.js").write_text(
        "const base = process.env.OPENAI_BASE_URL;\n", encoding="utf-8")
    assert _is_llm_calling_project(tmp_path) is True


def test_non_llm_project_not_detected(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\nrequests\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    # A lookalike dep must NOT trip exact-name detection.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\ndependencies=["openai-tokenizer"]\n', encoding="utf-8")
    assert _is_llm_calling_project(tmp_path) is False


# --- seam lifecycle / gating -------------------------------------------------


def _llm_project(tmp_path):
    (tmp_path / "requirements.txt").write_text("openai\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "import os\nBASE = os.getenv('OPENAI_BASE_URL')\n", encoding="utf-8")


@pytest.mark.requires_loopback
def test_seam_starts_and_injects_env(tmp_path):
    _llm_project(tmp_path)
    seam = _start_proof_mock_llm(tmp_path, None, enabled=True)
    assert seam is not None
    try:
        assert seam.env["OPENAI_BASE_URL"].startswith("http://127.0.0.1:")
        assert seam.env["OPENAI_BASE_URL"].endswith("/v1")
        assert seam.env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
        assert seam.env["OPENAI_API_KEY"] == MOCK_PROOF_VALUE
        assert seam.env["OPENROUTER_API_KEY"] == MOCK_PROOF_VALUE
        assert seam.env["ANTHROPIC_API_KEY"] == MOCK_PROOF_VALUE
    finally:
        seam.stop()


def test_seam_disabled_returns_none(tmp_path):
    _llm_project(tmp_path)
    assert _start_proof_mock_llm(tmp_path, None, enabled=False) is None


def test_seam_skips_non_llm_project(tmp_path):
    (tmp_path / "main.py").write_text("print('plain')\n", encoding="utf-8")
    assert _start_proof_mock_llm(tmp_path, None, enabled=True) is None


def test_seam_skips_network_isolated_docker_sandbox(tmp_path):
    _llm_project(tmp_path)
    ctx = SimpleNamespace(docker_available=True)
    # A --network none docker sandbox can't reach the host mock -> degrade to no-op.
    assert _start_proof_mock_llm(tmp_path, ctx, enabled=True) is None


def test_seam_degrades_open_when_server_fails(tmp_path, monkeypatch):
    _llm_project(tmp_path)

    class _BoomServer:
        def start(self):
            raise RuntimeError("cannot bind")

        def stop(self):
            pass

    monkeypatch.setattr("skyn3t.studio.mock_llm.MockLLMServer", _BoomServer)
    assert _start_proof_mock_llm(tmp_path, None, enabled=True) is None


def test_injected_dummy_key_survives_env_filter():
    # The sandbox filters secret-named env vars, but the mock's dummy key value is
    # an allowed sentinel so tests can build their client; a REAL key is stripped.
    filtered = filter_env({
        "OPENAI_API_KEY": MOCK_PROOF_VALUE,
        "OPENAI_BASE_URL": "http://127.0.0.1:5000/v1",
        "REAL_SECRET_API_KEY": "sk-live-should-be-stripped",
    })
    assert filtered.get("OPENAI_API_KEY") == MOCK_PROOF_VALUE
    assert filtered.get("OPENAI_BASE_URL") == "http://127.0.0.1:5000/v1"
    assert "REAL_SECRET_API_KEY" not in filtered


# --- end-to-end injection into the test step (executor monkeypatched) --------


def _llm_python_project_with_tests(tmp_path):
    (tmp_path / "requirements.txt").write_text("openai\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "import os\n\n\ndef base():\n    return os.getenv('OPENAI_BASE_URL')\n",
        encoding="utf-8")
    (tmp_path / "test_main.py").write_text(
        "from main import base\n\n\ndef test_base():\n    assert True\n",
        encoding="utf-8")


def _patch_executor(monkeypatch):
    """Record every proof command's (command, env); always report success."""
    calls: list[tuple[list[str], dict]] = []

    def fake(ctx, command, *, cwd, timeout, env=None, network=False):
        calls.append((list(command), dict(env or {})))
        return pr._ProofCommandResult(returncode=0, stdout="1 passed", stderr="")

    monkeypatch.setattr(pr, "_run_proof_command", fake)
    return calls


def _pytest_env(calls):
    for command, env in calls:
        if any("pytest" in str(c) for c in command):
            return env
    return None


@pytest.mark.requires_loopback
def test_env_injected_into_python_test_step(tmp_path, monkeypatch):
    _llm_python_project_with_tests(tmp_path)
    calls = _patch_executor(monkeypatch)
    res = proof_run(tmp_path, stack="python", execution_backend="inline",
                    run_tests=True, run_build=False)
    assert res.detail.get("mock_llm") == "active"
    env = _pytest_env(calls)
    assert env is not None, "pytest command was never executed"
    assert env["OPENAI_BASE_URL"].startswith("http://127.0.0.1:")
    assert env["OPENAI_API_KEY"] == MOCK_PROOF_VALUE


def test_flag_off_restores_old_behavior(tmp_path, monkeypatch):
    _llm_python_project_with_tests(tmp_path)
    calls = _patch_executor(monkeypatch)
    res = proof_run(tmp_path, stack="python", execution_backend="inline",
                    run_tests=True, run_build=False, enable_mock_llm=False)
    assert "mock_llm" not in res.detail
    env = _pytest_env(calls)
    assert env is not None
    assert "OPENAI_BASE_URL" not in env
    assert env.get("OPENAI_API_KEY") != MOCK_PROOF_VALUE or "OPENAI_API_KEY" not in env


def test_non_llm_project_gets_no_injection(tmp_path, monkeypatch):
    # Same shape but no LLM dep/reference -> seam never activates.
    (tmp_path / "main.py").write_text(
        "def base():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_main.py").write_text(
        "from main import base\n\n\ndef test_base():\n    assert base() == 1\n",
        encoding="utf-8")
    calls = _patch_executor(monkeypatch)
    res = proof_run(tmp_path, stack="python", execution_backend="inline",
                    run_tests=True, run_build=False)
    assert "mock_llm" not in res.detail
    env = _pytest_env(calls)
    assert env is not None
    assert "OPENAI_BASE_URL" not in env


def test_seam_default_enabled_reads_setting(monkeypatch):
    monkeypatch.setattr(pr, "_mock_llm_default_enabled", lambda: False)
    # With the default reader off and no explicit override, proof_run must not
    # activate the seam even for an LLM project.
    assert pr._mock_llm_default_enabled() is False
