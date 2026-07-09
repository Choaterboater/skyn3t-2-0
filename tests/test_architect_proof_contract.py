"""Architect-owned files remain mandatory through debug and final proof."""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventType
from skyn3t.studio.manifest import BuildManifest, StageRecord
from skyn3t.studio.planner import BuildPlan
from skyn3t.studio.proof_run import proof_run
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.stage_debug import debug_stage
from skyn3t.studio.stages import StageSpec


def test_architect_contract_is_persisted_and_drives_debug_and_final_proof(
    tmp_path: Path,
) -> None:
    project = tmp_path / "app"
    (project / "src" / "pages").mkdir(parents=True)
    (project / "src" / "pages" / "index.astro").write_text("<h1>Golf</h1>")
    (project / "package.json").write_text('{"scripts": {}}')

    code = StageSpec(name="code", agent_type="code", capability="codegen")
    plan = BuildPlan(
        slug="golf",
        brief="golf lessons",
        stack="astro",
        stages=[code],
        checklist=["package.json", "src/pages/index.astro"],
    )
    prior = {"architect": {"plan": {
        "stack": "astro",
        "summary": "Multiple tutorial pages",
        "files": [
            {"path": "package.json", "purpose": "manifest"},
            {"path": "src/pages/index.astro", "purpose": "home"},
            {"path": "src/pages/lessons.astro", "purpose": "lesson paths"},
            {"path": "../escape.txt", "purpose": "unsafe"},
        ],
    }}}
    manifest = BuildManifest(slug="golf", brief="golf lessons", stack="astro")
    record = StageRecord(name="architect", agent_type="architecture", capability="architecture")

    contract = StudioRunner._promote_architect_contract(plan, prior, manifest, record)

    assert contract is not None
    assert plan.checklist == [
        "package.json",
        "src/pages/index.astro",
        "src/pages/lessons.astro",
    ]
    assert code.extra["checklist"] == plan.checklist
    assert manifest.extra["architect_plan"] == contract
    assert record.output_summary["plan"] == contract

    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        run_generated_tests=False,
        run_generated_build=False,
    )
    emitted: list[tuple[EventType, dict]] = []

    async def emit(event_type: EventType, payload: dict) -> None:
        emitted.append((event_type, payload))

    debug = asyncio.run(
        debug_stage(
            build_id="build-1",
            spec=code,
            record=StageRecord(
                name="code",
                agent_type="code",
                capability="codegen",
                status="completed",
            ),
            worktree_dir=str(project),
            plan=plan,
            settings=settings,
            emit=emit,
            improve=None,
        )
    )
    assert debug.passed is False
    assert "src/pages/lessons.astro" in debug.detail["gaps"]
    assert emitted[-1][0] is EventType.STAGE_DEBUG_RESOLVED
    assert emitted[-1][1]["status"] == "degraded"

    final = proof_run(
        project,
        checklist=plan.checklist,
        stack="astro",
        run_tests=False,
        run_build=False,
    )
    assert final.passed is False
    assert "src/pages/lessons.astro" in final.missing
