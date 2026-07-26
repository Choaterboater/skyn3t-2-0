from __future__ import annotations

import asyncio

from skyn3t.adapters.llm import LLMClient, enforce_explicit_routing_lock
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventType
from skyn3t.core.model_router import Tier
from skyn3t.web import routes
from skyn3t.web.deps import AppState, BuildRecord


class _CaptureStudio:
    def __init__(self) -> None:
        self.extra: dict = {}

    def start(self, brief: str, slug: str | None = None, extra: dict | None = None) -> None:
        self.extra = dict(extra or {})


def _state(
    tmp_path,
    *,
    settings: Settings,
    client: LLMClient,
    studio: object,
) -> AppState:
    return AppState(settings=settings, llm_client=client, studio=studio)


async def test_auto_build_uses_codex_and_failure_history_never_injects_model(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="auto",
    )
    client = LLMClient(settings)
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")
    studio = _CaptureStudio()
    state = _state(tmp_path, settings=settings, client=client, studio=studio)
    for index, status in enumerate(("failed", "completed_no_go"), start=1):
        state.builds[f"prior-{index}"] = BuildRecord(
            build_id=f"prior-{index}",
            brief="a tile platformer",
            stack="phaser",
            slug="tile-platformer",
            status=status,
        )

    result = await routes.submit_build(
        state,
        brief="a tile platformer",
        stack="phaser",
        slug="tile-platformer",
    )

    trace = state.builds[result["build_id"]].model_trace
    assert result["model_override"] == ""
    assert "model_override" not in studio.extra
    assert trace["failure_count"] == 2
    assert trace["requested_backend"] == "auto"
    assert trace["effective_backend"] == "codex_cli"
    assert trace["backend"] == "codex_cli"
    assert trace["requested_model"] == ""
    assert trace["effective_model"] == "codex-cli:default"


async def test_explicit_openrouter_is_locked_for_the_entire_background_build(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
    )
    client = LLMClient(settings)
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "codex")
    completed = asyncio.Event()
    observed: list[str] = []

    class _AsyncStudio:
        async def start(
            self,
            brief: str,
            slug: str | None = None,
            extra: dict | None = None,
        ) -> None:
            observed.append(client.backend)
            settings.llm_backend = "codex_cli"
            await asyncio.sleep(0)
            observed.append(client.backend)
            completed.set()

    state = _state(
        tmp_path,
        settings=settings,
        client=client,
        studio=_AsyncStudio(),
    )

    result = await routes.submit_build(
        state,
        brief="an inventory app",
        model_override="openai/gpt-4.1-mini",
    )
    await asyncio.wait_for(completed.wait(), timeout=1)

    trace = state.builds[result["build_id"]].model_trace
    assert observed == ["openrouter", "openrouter"]
    assert trace["requested_backend"] == "openrouter"
    assert trace["effective_backend"] == "openrouter"
    assert trace["requested_model"] == ""
    assert trace["effective_model"] == "router:auto"
    assert trace["submission"]["model_override"] == "openai/gpt-4.1-mini"
    assert trace["codegen"]["requested_model"] == "openai/gpt-4.1-mini"
    assert trace["codegen"]["effective_model"] == "openai/gpt-4.1-mini"


async def test_background_build_keeps_submission_model_policy_after_settings_mutate(
    tmp_path,
):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
        preferred_model="openai/submission-preferred",
        model_ui="openai/submission-ui",
    )
    client = LLMClient(settings)
    started = asyncio.Event()
    resume = asyncio.Event()
    completed = asyncio.Event()
    observed: list[str] = []

    class _AsyncStudio:
        async def start(
            self,
            brief: str,
            slug: str | None = None,
            extra: dict | None = None,
        ) -> None:
            started.set()
            await resume.wait()
            observed.append(
                client._resolve_pinned_model(
                    tier=Tier.UI,
                    setting_names=("preferred_model",),
                    allow_live_catalog=False,
                )
            )
            observed.append(
                client._resolve_pinned_model(
                    tier=Tier.UI,
                    allow_live_catalog=False,
                )
            )
            completed.set()

    state = _state(
        tmp_path,
        settings=settings,
        client=client,
        studio=_AsyncStudio(),
    )
    await routes.submit_build(state, brief="a settings-race regression")
    await asyncio.wait_for(started.wait(), timeout=1)

    settings.free_only = True
    settings.preferred_model = "openrouter/free"
    settings.model_ui = "openrouter/free"
    resume.set()
    await asyncio.wait_for(completed.wait(), timeout=1)

    assert observed == [
        "openai/submission-preferred",
        "openai/submission-ui",
    ]


def test_runner_lock_reuses_submission_route_not_changed_live_settings(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
    )
    client = LLMClient(settings)
    snapshot = client.build_routing_snapshot()
    settings.llm_backend = "codex_cli"
    settings.codegen_cli_provider = "claude"
    monkeypatch.setattr(client, "_cli_available", lambda provider: False)

    with client.build_routing_scope(snapshot):
        enforce_explicit_routing_lock(
            settings,
            cli_available=client._cli_available,
            require_codex_for_auto=True,
        )


async def test_explicit_codegen_cli_and_model_pin_own_codegen_trace(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
        preferred_model="openai/gpt-4.1-mini",
        openrouter_codegen_model="openai/gpt-4.1",
        codegen_cli_provider="claude",
        codegen_cli_model="sonnet",
    )
    client = LLMClient(settings)
    monkeypatch.setattr(client, "_cli_available", lambda provider: provider == "claude")
    studio = _CaptureStudio()
    state = _state(tmp_path, settings=settings, client=client, studio=studio)

    result = await routes.submit_build(
        state,
        brief="a polished storefront",
        model_override="google/gemini-2.5-flash",
    )

    trace = state.builds[result["build_id"]].model_trace
    assert trace["requested_backend"] == "openrouter"
    assert trace["effective_backend"] == "openrouter"
    assert trace["requested_model"] == "openai/gpt-4.1-mini"
    assert trace["effective_model"] == "openai/gpt-4.1-mini"
    assert trace["submission"]["model_override"] == "google/gemini-2.5-flash"
    assert trace["codegen"]["requested_backend"] == "claude_cli"
    assert trace["codegen"]["effective_backend"] == "claude_cli"
    assert trace["codegen"]["requested_model"] == "sonnet"
    assert trace["codegen"]["effective_model"] == "sonnet"


def test_build_scope_freezes_preferred_free_tier_fallback_and_vision_policy(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
        preferred_model="openai/submission-preferred",
        model_ui="openai/submission-ui",
        llm_fallback_enabled=True,
        llm_fallback_models="openai/submission-fallback",
        vision_model="openai/submission-vision",
    )
    client = LLMClient(settings)
    monkeypatch.setattr(client.router, "fallback_candidates", lambda *args, **kwargs: [])
    snapshot = client.build_routing_snapshot()

    settings.free_only = True
    settings.preferred_model = "openrouter/free"
    settings.model_ui = "openrouter/free"
    settings.llm_fallback_enabled = False
    settings.llm_fallback_models = "openrouter/free"
    settings.vision_model = "openai/live-mutated-vision"

    with client.build_routing_scope(snapshot):
        assert client._resolve_pinned_model(
            tier=Tier.UI,
            setting_names=("preferred_model",),
            allow_live_catalog=False,
        ) == "openai/submission-preferred"
        assert client._resolve_pinned_model(
            tier=Tier.UI,
            allow_live_catalog=False,
        ) == "openai/submission-ui"
        assert client._fallback_models(
            "openai/primary",
            Tier.UI,
        ) == ["openai/submission-fallback"]
        assert client._resolve_vision(
            "openai/text-only",
            None,
        ) == ("openai/submission-vision", True)


def test_gui_model_override_is_scoped_to_codegen_not_top_level_stages(tmp_path):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
    )
    client = LLMClient(settings)

    snapshot = client.build_routing_snapshot("openai/codegen-only")

    assert snapshot["requested_model"] == ""
    assert snapshot["effective_model"] == "router:auto"
    assert snapshot["submission"]["model_override"] == "openai/codegen-only"
    assert snapshot["codegen"]["requested_model"] == "openai/codegen-only"
    assert snapshot["codegen"]["effective_model"] == "openai/codegen-only"


def test_restore_submission_trace_does_not_replace_terminal_execution_truth(tmp_path):
    state = AppState(
        settings=Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            llm_backend="openrouter",
            openrouter_api_key="sk-or-test",
            free_only=False,
        )
    )
    state.builds["build-1"] = BuildRecord(
        build_id="build-1",
        brief="test",
        model_trace={
            "backend": "codex_cli",
            "effective_backend": "openrouter",
            "effective_model": "router:auto",
            "codegen": {
                "effective_backend": "codex_cli",
                "effective_model": "openai/fallback-that-ran",
            },
            "effective_codegen_backend": "codex_cli",
            "effective_codegen_model": "openai/fallback-that-ran",
        },
    )
    submission = {
        "requested_backend": "openrouter",
        "effective_backend": "openrouter",
        "requested_model": "",
        "effective_model": "router:auto",
        "submission": {
            "requested_backend": "openrouter",
            "model_override": "",
        },
        "codegen": {
            "source": "global_backend",
            "requested_backend": "openrouter",
            "effective_backend": "openrouter",
            "requested_model": "",
            "effective_model": "router:auto",
        },
    }

    routes._restore_submission_routing_trace(state, "build-1", submission, None)

    trace = state.builds["build-1"].model_trace
    assert trace["submission"]["requested_backend"] == "openrouter"
    assert trace["submission"]["model_override"] == ""
    assert trace["submission"]["codegen"]["effective_model"] == "router:auto"
    assert trace["requested_backend"] == "openrouter"
    assert trace["codegen"]["requested_backend"] == "openrouter"
    assert trace["codegen"]["effective_backend"] == "codex_cli"
    assert trace["codegen"]["effective_model"] == "openai/fallback-that-ran"
    assert trace["backend"] == "codex_cli"
    assert trace["effective_codegen_model"] == "openai/fallback-that-ran"


async def test_codegen_cli_and_model_pin_stay_locked_after_live_settings_change(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
        codegen_cli_provider="copilot",
        codegen_cli_model="gpt-5-mini",
    )
    client = LLMClient(settings)
    monkeypatch.setattr(client, "_cli_available", lambda provider: True)
    snapshot = client.build_routing_snapshot()
    settings.codegen_cli_provider = "claude"
    settings.codegen_cli_model = "opus"
    captured_argv: list[str] = []

    class _Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_subprocess(*argv, **kwargs):
        captured_argv.extend(str(value) for value in argv)
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess)

    with client.build_routing_scope(snapshot):
        result = await client.agentic_build(
            "build a complete app",
            str(tmp_path),
            provider="claude",
            model="opus",
        )

    assert result["backend"] == "copilot_cli"
    assert result["model"] == "gpt-5-mini"
    assert captured_argv[0] == "copilot"
    assert captured_argv[captured_argv.index("--model") + 1] == "gpt-5-mini"


async def test_automatic_codegen_keeps_internal_per_slice_model_choice(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="sk-or-test",
        free_only=False,
        codegen_cli_provider="",
        openrouter_codegen_model="",
        preferred_model="",
    )
    client = LLMClient(settings)
    snapshot = client.build_routing_snapshot()
    observed: dict[str, str] = {}

    async def _fake_openrouter_agentic(prompt, workdir, model, **kwargs):
        observed["model"] = model
        return {"ok": True, "backend": "openrouter", "model": model}

    monkeypatch.setattr(client, "_openrouter_agentic", _fake_openrouter_agentic)

    with client.build_routing_scope(snapshot):
        result = await client.agentic_build(
            "build one frontend slice",
            str(tmp_path),
            model="openai/internal-slice-model",
        )

    assert result["backend"] == "openrouter"
    assert observed["model"] == "openai/internal-slice-model"


async def test_explicit_stub_remains_visible_as_offline_execution(tmp_path):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="stub",
    )
    client = LLMClient(settings)
    studio = _CaptureStudio()
    state = _state(tmp_path, settings=settings, client=client, studio=studio)

    result = await routes.submit_build(state, brief="an offline prototype")

    trace = state.builds[result["build_id"]].model_trace
    assert trace["requested_backend"] == "stub"
    assert trace["effective_backend"] == "stub"
    assert trace["requested_model"] == ""
    assert trace["effective_model"] == "offline-stub"
    assert trace["codegen"]["requested_backend"] == "stub"
    assert trace["codegen"]["effective_backend"] == "stub"
    assert trace["codegen"]["effective_model"] == "offline-stub"


async def test_terminal_event_keeps_submission_route_truth_in_build_record(tmp_path):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="stub",
    )
    client = LLMClient(settings)
    completed = asyncio.Event()
    state: AppState

    class _TerminalStudio:
        async def start(
            self,
            brief: str,
            slug: str | None = None,
            extra: dict | None = None,
        ) -> None:
            await state.event_bus.emit(
                EventType.BUILD_COMPLETED,
                source="test.studio",
                payload={
                    "build_id": str((extra or {}).get("build_id") or ""),
                    "status": "completed",
                    "model_trace": {
                        "backend": "stale-setting-label",
                        "prompt_count": 3,
                    },
                },
            )
            completed.set()

    state = _state(
        tmp_path,
        settings=settings,
        client=client,
        studio=_TerminalStudio(),
    )
    result = await routes.submit_build(state, brief="an offline prototype")
    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.sleep(0)

    trace = state.builds[result["build_id"]].model_trace
    assert trace["prompt_count"] == 3
    assert trace["requested_backend"] == "stub"
    assert trace["effective_backend"] == "stub"
    assert trace["backend"] == "stub"
