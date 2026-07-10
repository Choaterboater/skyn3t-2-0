"""Runner-level provider locks fail before stack selection or model spend."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyn3t.adapters.llm import (
    LLMClient,
    RoutingLockError,
    explicit_routing_lock_error,
)
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.runner import BuildOutcome, StudioRunner


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path / "data",
        "projects_dir": tmp_path / "Projects",
        "logs_dir": tmp_path / "logs",
        "vector_db_path": tmp_path / "vectors",
        "llm_backend": "stub",
    }
    values.update(overrides)
    return Settings(**values)


def _runner(settings: Settings, *, cost_tracker=None) -> StudioRunner:
    bus = EventBus()
    return StudioRunner(
        bus,
        Orchestrator(bus),
        settings=settings,
        memory=None,
        cost_tracker=cost_tracker,
    )


def test_auto_without_explicit_cli_does_not_construct_llm_client(monkeypatch):
    def forbidden_init(self, *_args, **_kwargs):
        raise AssertionError("auto/stub validation constructed an LLM client")

    monkeypatch.setattr(LLMClient, "__init__", forbidden_init)
    settings = SimpleNamespace(
        llm_backend="auto",
        openrouter_api_key="",
        codegen_cli_provider="",
    )
    assert explicit_routing_lock_error(settings) == ""


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        ("openrouter", "API_KEY is not configured"),
        ("made_up_backend", "Unsupported LLM backend"),
    ],
)
async def test_runner_rejects_invalid_explicit_backend_before_build_ledger(
    tmp_path, monkeypatch, backend, message
):
    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    class Tracker:
        starts = 0

        def start_build(self, _build_id):
            self.starts += 1

    tracker = Tracker()
    runner = _runner(
        _settings(tmp_path, llm_backend=backend, openrouter_api_key=""),
        cost_tracker=tracker,
    )

    called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("_start_build reached")

    monkeypatch.setattr(runner, "_start_build", forbidden)
    with pytest.raises(RoutingLockError, match=message):
        await runner.start("build a dashboard")

    assert called is False
    assert tracker.starts == 0


async def test_runner_rejects_unavailable_explicit_codegen_cli_before_selection(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        LLMClient,
        "_cli_available",
        classmethod(lambda _cls, _provider: False),
    )
    runner = _runner(
        _settings(
            tmp_path,
            llm_backend="openrouter",
            openrouter_api_key="test-key-not-used",
            codegen_cli_provider="codex",
        )
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("stack selection/build work reached")

    monkeypatch.setattr(runner, "_start_build", forbidden)
    with pytest.raises(RoutingLockError, match="codegen_cli_provider='codex'.*unavailable"):
        await runner.start("build a dashboard")


async def test_runner_rejects_unavailable_explicit_global_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(
        LLMClient,
        "_cli_available",
        classmethod(lambda _cls, _provider: False),
    )
    runner = _runner(_settings(tmp_path, llm_backend="codex_cli"))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("stack selection/build work reached")

    monkeypatch.setattr(runner, "_start_build", forbidden)
    with pytest.raises(RoutingLockError, match="codex CLI.*not available"):
        await runner.start("build a dashboard")


async def test_runner_preserves_auto_to_offline_stub_behavior(tmp_path, monkeypatch):
    monkeypatch.delenv("SKYN3T_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        LLMClient,
        "_cli_available",
        classmethod(lambda _cls, _provider: False),
    )
    runner = _runner(_settings(tmp_path, llm_backend="auto", openrouter_api_key=""))
    expected = BuildOutcome(
        build_id="build-1",
        slug="demo",
        status="completed",
        verdict="go",
        score=100.0,
        stack="python_cli",
        project_dir=str(tmp_path / "Projects" / "demo"),
    )

    async def offline_build(*_args, **_kwargs):
        return expected

    monkeypatch.setattr(runner, "_start_build", offline_build)
    assert await runner.start("build an offline tool") is expected
