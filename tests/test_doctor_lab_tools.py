from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from skyn3t.cli import main as cli
from skyn3t.core.stacks import WEB_STACKS
from skyn3t.observability import health
from skyn3t.studio.lab_tools import LabToolchainReport, ToolCheck


def _report(
    *,
    stack: str,
    docker_ready: bool = True,
    playwright_ready: bool = True,
    maestro_ready: bool = False,
) -> LabToolchainReport:
    is_web = not stack or stack in WEB_STACKS
    is_mobile = stack == "react_native"
    return LabToolchainReport(
        stack=stack,
        checks={
            "docker": ToolCheck(
                name="docker",
                installed=True,
                ready=docker_ready,
                required=is_web,
                detail="daemon ready" if docker_ready else "daemon unavailable",
            ),
            "playwright": ToolCheck(
                name="playwright",
                installed=True,
                ready=playwright_ready,
                required=not is_mobile,
                detail="Version 1.61.0" if playwright_ready else "playwright CLI not installed",
            ),
            "maestro": ToolCheck(
                name="maestro",
                installed=maestro_ready,
                ready=maestro_ready,
                required=is_mobile,
                detail="1.40.0" if maestro_ready else "maestro CLI not installed",
            ),
        },
    )


def test_health_doctor_reports_effective_codex_backend_without_provider_key(
    monkeypatch,
    tmp_path,
) -> None:
    from skyn3t.adapters import llm as llm_module
    from skyn3t.config.settings import Settings

    settings = Settings(
        llm_backend="auto",
        openrouter_api_key="",
        anthropic_api_key="",
        openai_api_key="",
        kimi_api_key="",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        projects_dir=tmp_path / "projects",
    )
    monkeypatch.setattr(
        llm_module.LLMClient,
        "_cli_available",
        classmethod(lambda _cls, provider: provider == "codex"),
    )
    monkeypatch.setattr(
        llm_module.LLMClient,
        "_cli_detail",
        classmethod(
            lambda _cls, provider: {
                "provider": provider,
                "available": provider == "codex",
                "account_source": "local_cli_session",
                "account_verified": False,
                "cost_source": "not_reported_by_cli",
                "cost_usd_known": False,
            }
        ),
    )
    monkeypatch.setattr(
        health,
        "inspect_lab_toolchain",
        lambda *, stack="": _report(stack=stack),
    )
    monkeypatch.setattr(health, "_writable", lambda _path: True)

    report = asyncio.run(health.doctor(settings, stack="react"))

    checks = {check["name"]: check for check in report["checks"]}
    assert settings.has_any_llm is False
    assert checks["llm"] == {
        "name": "llm",
        "status": "ok",
        "detail": "codex_cli (requested auto, ready)",
    }


def test_health_doctor_degrades_when_selected_backend_resolves_to_stub(
    monkeypatch,
    tmp_path,
) -> None:
    from skyn3t.adapters import llm as llm_module
    from skyn3t.config.settings import Settings

    settings = Settings(
        llm_backend="codex_cli",
        openrouter_api_key="",
        anthropic_api_key="",
        openai_api_key="",
        kimi_api_key="",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        projects_dir=tmp_path / "projects",
    )
    monkeypatch.setattr(
        llm_module.LLMClient,
        "_cli_available",
        classmethod(lambda _cls, _provider: False),
    )
    monkeypatch.setattr(
        llm_module.LLMClient,
        "_cli_detail",
        classmethod(
            lambda _cls, provider: {
                "provider": provider,
                "available": False,
                "account_source": "local_cli_session",
                "account_verified": False,
                "cost_source": "not_reported_by_cli",
                "cost_usd_known": False,
            }
        ),
    )
    monkeypatch.setattr(
        health,
        "inspect_lab_toolchain",
        lambda *, stack="": _report(stack=stack),
    )
    monkeypatch.setattr(health, "_writable", lambda _path: True)

    report = asyncio.run(health.doctor(settings, stack="react"))

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["llm"]["status"] == "degraded"
    assert "stub (requested codex_cli, cli_missing)" in checks["llm"]["detail"]
    assert "selected but is not available" in checks["llm"]["detail"]


def test_health_doctor_probes_lab_toolchain_once_and_blocks_required_docker(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def inspect(*, stack: str = "") -> LabToolchainReport:
        calls.append(stack)
        return _report(stack=stack, docker_ready=False)

    monkeypatch.setattr(health, "inspect_lab_toolchain", inspect, raising=False)
    settings = SimpleNamespace(
        proof_ladder_required=True,
        lab_autonomy=False,
        has_any_llm=True,
        data_dir=SimpleNamespace(exists=lambda: True),
        logs_dir=SimpleNamespace(exists=lambda: True),
    )
    monkeypatch.setattr(health, "_writable", lambda _path: True)

    report = asyncio.run(health.doctor(settings, stack="react"))

    checks = {check["name"]: check for check in report["checks"]}
    assert calls == ["react"]
    assert checks["docker"] == {
        "name": "docker",
        "status": "fail",
        "detail": "required for proof ladder; daemon unavailable",
    }
    assert checks["playwright"]["status"] == "ok"
    assert checks["maestro"] == {
        "name": "maestro",
        "status": "ok",
        "detail": "not required for stack react; maestro CLI not installed",
    }


def test_health_doctor_uses_ladder_setting_even_when_lab_autonomy_is_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        health,
        "inspect_lab_toolchain",
        lambda *, stack="": _report(stack=stack, playwright_ready=False),
        raising=False,
    )
    settings = SimpleNamespace(
        proof_ladder_required=False,
        lab_autonomy=True,
        has_any_llm=True,
        data_dir=SimpleNamespace(exists=lambda: True),
        logs_dir=SimpleNamespace(exists=lambda: True),
    )
    monkeypatch.setattr(health, "_writable", lambda _path: True)

    report = asyncio.run(health.doctor(settings, stack="react"))

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["playwright"] == {
        "name": "playwright",
        "status": "degraded",
        "detail": ("not required because proof ladder is disabled; playwright CLI not installed"),
    }
    assert checks["maestro"]["status"] == "ok"


def test_reused_health_registry_refreshes_live_tool_readiness(monkeypatch) -> None:
    reports = [
        _report(stack="react", docker_ready=False),
        _report(stack="react", docker_ready=True),
    ]
    calls: list[str] = []

    def inspect(*, stack: str = "") -> LabToolchainReport:
        calls.append(stack)
        return reports.pop(0)

    monkeypatch.setattr(health, "inspect_lab_toolchain", inspect)
    monkeypatch.setattr(health, "_writable", lambda _path: True)
    settings = SimpleNamespace(
        proof_ladder_required=True,
        lab_autonomy=False,
        has_any_llm=True,
        data_dir=SimpleNamespace(exists=lambda: True),
        logs_dir=SimpleNamespace(exists=lambda: True),
    )
    registry = health.doctor_registry(settings, stack="react")

    first = asyncio.run(registry.run())
    second = asyncio.run(registry.run())

    first_checks = {check["name"]: check for check in first["checks"]}
    second_checks = {check["name"]: check for check in second["checks"]}
    assert calls == ["react", "react"]
    assert first_checks["docker"]["status"] == "fail"
    assert second_checks["docker"]["status"] == "ok"


def test_health_doctor_classifies_probe_failure_by_stack_and_policy(
    monkeypatch,
) -> None:
    def inspect(*, stack: str = "") -> LabToolchainReport:
        raise RuntimeError(f"{stack} probe exploded")

    monkeypatch.setattr(health, "inspect_lab_toolchain", inspect)
    monkeypatch.setattr(health, "_writable", lambda _path: True)
    settings = SimpleNamespace(
        proof_ladder_required=True,
        lab_autonomy=False,
        has_any_llm=True,
        data_dir=SimpleNamespace(exists=lambda: True),
        logs_dir=SimpleNamespace(exists=lambda: True),
    )

    report = asyncio.run(health.doctor(settings, stack="react_native"))

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["docker"]["status"] == "ok"
    assert checks["playwright"]["status"] == "ok"
    assert checks["maestro"]["status"] == "fail"
    assert "not required for stack react_native" in checks["docker"]["detail"]
    assert "readiness unknown" in checks["docker"]["detail"]
    assert "react_native probe exploded" in checks["maestro"]["detail"]


def test_cli_doctor_reports_stack_aware_lab_readiness_and_stays_exit_zero(
    monkeypatch,
    tmp_path,
) -> None:
    from skyn3t.config import settings as settings_mod
    from skyn3t.config.settings import Settings
    from skyn3t.studio import lab_tools

    settings = Settings(
        proof_ladder_required=True,
        execution_backend="auto",
        projects_dir=tmp_path / "projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )

    def get_settings():
        return settings

    get_settings.cache_clear = lambda: None
    monkeypatch.setattr(settings_mod, "get_settings", get_settings)
    monkeypatch.setattr(cli, "_check_db", lambda _settings: ("memory", True))
    monkeypatch.setattr(cli, "_check_llm", lambda _settings: ("stub", False))
    monkeypatch.setattr(cli, "_check_writable", lambda _path: (True, "writable"))
    monkeypatch.setattr(cli, "_has_module", lambda _name: True)
    monkeypatch.setattr(cli, "_console", lambda: Console(width=240))
    inspected_stacks: list[str] = []

    def inspect(*, stack=""):
        inspected_stacks.append(stack)
        return _report(
            stack=stack,
            docker_ready=False,
            maestro_ready=False,
        )

    monkeypatch.setattr(
        lab_tools,
        "inspect_lab_toolchain",
        inspect,
    )

    result = CliRunner().invoke(cli.app, ["doctor", "--stack", "react_native"])

    assert result.exit_code == 0
    output = " ".join(result.output.replace("│", " ").split())
    assert "lab:docker" in output
    assert "lab:playwright" in output
    assert "lab:maestro" in output
    assert "not required for stack react_native; daemon unavailable" in output
    assert "required for proof ladder; maestro CLI not installed" in output
    assert "provider_key_configured=False" in output
    assert inspected_stacks == ["react_native"]

    monkeypatch.setattr(
        cli,
        "_console",
        lambda: SimpleNamespace(print=lambda *_args, **_kwargs: None),
    )
    cli.doctor()
    assert inspected_stacks == ["react_native", ""]

def test_check_sandbox_reports_cli_ready_docker_not_optional_sdk(monkeypatch) -> None:
    from skyn3t.security.sandbox import SandboxRunner

    monkeypatch.setattr(SandboxRunner, "docker_available", lambda _self: True)

    assert cli._check_sandbox(SimpleNamespace(execution_backend="auto")) == (
        "auto (docker daemon ready)"
    )
    assert cli._check_sandbox(SimpleNamespace(execution_backend="docker")) == (
        "docker (daemon ready)"
    )


@pytest.mark.parametrize(
    ("inspector", "expected_reason"),
    [
        (
            lambda *, stack="": (_ for _ in ()).throw(RuntimeError("probe exploded")),
            "toolchain inspection failed: probe exploded",
        ),
        (
            lambda *, stack="": SimpleNamespace(checks=None),
            "toolchain report checks must be a mapping",
        ),
    ],
)
def test_cli_doctor_reports_unknown_probe_state_without_crashing(
    monkeypatch,
    tmp_path,
    inspector,
    expected_reason,
) -> None:
    from skyn3t.config import settings as settings_mod
    from skyn3t.config.settings import Settings
    from skyn3t.studio import lab_tools

    settings = Settings(
        proof_ladder_required=True,
        projects_dir=tmp_path / "projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )

    def get_settings():
        return settings

    get_settings.cache_clear = lambda: None
    monkeypatch.setattr(settings_mod, "get_settings", get_settings)
    monkeypatch.setattr(cli, "_check_db", lambda _settings: ("memory", True))
    monkeypatch.setattr(cli, "_check_llm", lambda _settings: ("stub", False))
    monkeypatch.setattr(cli, "_check_writable", lambda _path: (True, "writable"))
    monkeypatch.setattr(cli, "_has_module", lambda _name: True)
    monkeypatch.setattr(cli, "_console", lambda: Console(width=240))
    monkeypatch.setattr(lab_tools, "inspect_lab_toolchain", inspector)

    result = CliRunner().invoke(cli.app, ["doctor", "--stack", "react_native"])

    assert result.exit_code == 0
    output = " ".join(result.output.replace("│", " ").split())
    assert "lab:docker N/A" in output
    assert "lab:playwright N/A" in output
    assert "lab:maestro ERROR" in output
    assert expected_reason in output
    assert "lab:docker MISSING" not in output
    assert "lab:maestro MISSING" not in output
