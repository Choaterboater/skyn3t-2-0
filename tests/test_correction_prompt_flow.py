"""Authenticated correction memory must reach real codegen after a restart."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.memory.store import MemoryStore
from skyn3t.studio.runner import StudioRunner
from skyn3t.web import routes
from skyn3t.web.deps import AppState


class _RecordingOfflineBackend:
    """Exercise completion prompt assembly, but answer with the free local stub."""

    backend = "offline_recording"
    supports_agentic = False
    last_model = None
    last_route = None

    def __init__(self, settings):
        self.settings = settings
        self.prompts: list[str] = []
        self.routes: list[dict] = []
        self._offline = LLMClient(settings)

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return await self._offline.complete(prompt, **kwargs)


async def test_authenticated_correction_reaches_next_code_prompt_and_retires(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        projects_dir=tmp_path / "Projects",
        logs_dir=tmp_path / "logs",
        auth_token="correction-test-token",
        llm_backend="stub",
        critic_enabled=False,
        approval_gates=False,
        best_of_n=1,
        liveness_check_enabled=False,
        run_generated_tests=False,
        run_generated_build=False,
    )
    bus = EventBus()
    memory = MemoryStore(settings)
    await memory.init_db()
    state = AppState(event_bus=bus, settings=settings, memory=memory)
    app = FastAPI()
    app.include_router(routes.build_router(state))
    headers = {"Authorization": "Bearer correction-test-token"}
    matching = "Keep inventory command names stable across releases."
    other_project = "Use accounting ledger terminology in every command."
    other_stage = "Explain the audit findings in chronological order."
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = {"text": matching, "project": "inventory", "stage": "code"}
            denied = await client.post("/api/learning/corrections", json=body)
            assert denied.status_code == 401
            saved = await client.post(
                "/api/learning/corrections", json=body, headers=headers
            )
            assert saved.status_code == 200
            correction_id = saved.json()["correction"]["id"]
            for text, project, stage in (
                (other_project, "accounting", "code"),
                (other_stage, "inventory", "review"),
            ):
                response = await client.post(
                    "/api/learning/corrections",
                    json={"text": text, "project": project, "stage": stage},
                    headers=headers,
                )
                assert response.status_code == 200

            # No captured row or hand-built extra is handed to codegen: only the
            # reopened database can supply the runner's real per-stage context.
            await memory.close()
            memory = MemoryStore(settings)
            await memory.init_db()
            state.memory = memory
            backend = _RecordingOfflineBackend(settings)
            orch = Orchestrator(bus)
            await orch.register(CodeAgent(event_bus=bus, llm=backend))
            runner = StudioRunner(bus, orch, settings=settings, memory=memory)

            async def code_prompts(project, attempt):
                # Fresh delivery roots keep project identity stable across runs;
                # the runner otherwise correctly reserves inventory-2 on rebuild.
                settings.projects_dir = tmp_path / attempt
                backend.prompts.clear()
                outcome = await runner.start(
                    "Build a small Python inventory command line tool.",
                    slug=project,
                    extra={"stack": "python_cli"},
                )
                assert outcome.slug == project
                stages = {stage["name"]: stage for stage in outcome.manifest["stages"]}
                assert stages["code"]["status"] == "completed"
                assert backend.prompts, "The actual CodeAgent must call its completion backend"
                return tuple(backend.prompts)

            first = await code_prompts("inventory", "first-build")
            assert all(matching in prompt for prompt in first)
            assert all(other_project not in prompt for prompt in first)
            assert all(other_stage not in prompt for prompt in first)

            unrelated = await code_prompts("accounting", "other-project-build")
            assert all(other_project in prompt for prompt in unrelated)
            assert all(matching not in prompt for prompt in unrelated)
            assert all(other_stage not in prompt for prompt in unrelated)

            retired = await client.post(
                f"/api/learning/corrections/{correction_id}/retire", headers=headers
            )
            assert retired.status_code == 200
            after_retirement = await code_prompts("inventory", "retired-build")
            assert all(matching not in prompt for prompt in after_retirement)
            assert all(other_project not in prompt for prompt in after_retirement)
            assert all(other_stage not in prompt for prompt in after_retirement)
    finally:
        await memory.close()
