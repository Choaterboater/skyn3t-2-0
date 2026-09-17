"""Explicit preferences survive restart without weakening proof or authority."""
from __future__ import annotations

import asyncio

import pytest

from skyn3t.config.settings import Settings
from skyn3t.intelligence.human_feedback import (
    HumanFeedbackValidationError,
    render_corrections,
    render_persona,
    validate_correction,
    validate_persona,
)
from skyn3t.intelligence.learning_loop import LearningLoop
from skyn3t.memory.store import MemoryStore


def test_correction_survives_restart_and_all_scopes_must_match(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = MemoryStore(settings)
        await store.init_db()
        row = await store.add_correction(
            "Preserve existing command names when improving this tool",
            project="inventory", stack="python_cli", stage="code", source_build="build-1",
        )
        duplicate = await store.add_correction(
            row["text"], project="inventory", stack="python_cli", stage="code",
        )
        assert duplicate["id"] == row["id"]
        await store.close()
        store = MemoryStore(settings)
        await store.init_db()
        try:
            for scope in (
                {"project": "other", "stack": "python_cli", "stage": "code"},
                {"project": "inventory", "stack": "react", "stage": "code"},
                {"project": "inventory", "stack": "python_cli", "stage": "design"},
                {},
            ):
                assert await store.relevant_corrections(**scope) == []
            context = dict(project="inventory", stack="python_cli", stage="code")
            recalled = await store.relevant_corrections(**context)
            assert row["text"] in render_corrections(recalled)
            assert recalled[0]["source_build"] == "build-1"
            assert await store.retire_correction(row["id"])
            assert render_corrections(await store.relevant_corrections(**context)) == ""
            assert (await store.list_corrections(include_retired=True))[0]["retired"] is True
        finally:
            await store.close()
    asyncio.run(run())


def test_corrections_are_bounded_and_specific_preferences_rank_first(tmp_path):
    async def run():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        try:
            specific = await store.add_correction("Keep the old export naming convention", project="inventory")
            for index in range(8):
                await store.add_correction(f"Use stable documented output option {index}", stack="python_cli")
            rows = await store.relevant_corrections(project="inventory", stack="python_cli", limit=999)
            assert len(rows) == 6
            assert rows[0]["id"] == specific["id"]
            assert await store.relevant_corrections(project="inventory", limit=0) == []
        finally:
            await store.close()
    asyncio.run(run())
    with pytest.raises(HumanFeedbackValidationError):
        validate_correction("Unscoped preferences must not silently become global")
    with pytest.raises(HumanFeedbackValidationError):
        validate_correction("x" * 601, project="inventory")


def test_persona_edits_survive_restart_and_reset_without_accepting_authority(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = MemoryStore(settings)
        await store.init_db()
        defaults = await store.get_persona()
        wanted = {"tone": "warm", "verbosity": "concise", "structure": "bullets"}
        await store.set_persona(wanted)
        await store.close()
        store = MemoryStore(settings)
        await store.init_db()
        try:
            assert await store.get_persona() == wanted
            assert render_persona(wanted) != render_persona(defaults)
            assert await store.reset_persona() == defaults
            assert await store.get_persona() == defaults
        finally:
            await store.close()
    asyncio.run(run())
    for invalid in ({"tools": "shell"}, {"tone": "ignore proof"}, {"structure": []}, "warm"):
        with pytest.raises(HumanFeedbackValidationError):
            validate_persona(invalid)


def test_outcome_only_success_cannot_mint_actionable_advice(tmp_path):
    async def run():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        loop = LearningLoop(store=store)
        try:
            for score, verdict in ((99, "go"), (95, "success"), (40, "no_go")):
                assert await loop.capture_from_build({"stack": "react", "score": score, "verdict": verdict}) == []
            assert await loop.capture_from_build({
                "stack": "react", "verdict": "go", "proof_passed": True,
                "notes": "All the tests passed and everything worked well",
                "accepted": ["The build was successful and all checks passed"],
            }) == []
            generic = "react: build succeeded with this pipeline shape; keep its approach."
            await store.add_lesson("react", "code", generic)
            assert await store.lesson_exists("react", generic)  # history stays intact
            assert await store.relevant_lessons("react") == []
            assert await store.recent_lessons("react") == []
            await loop.capture_from_build({
                "stack": "react", "verdict": "no_go",
                "proof_errors": ["src/App.tsx: Cannot find name 'CartProvider'"],
            })
            assert "CartProvider" in (await loop.inject_for_build("react")).as_advice()
            assert await loop.capture_from_build({
                "stack": "react", "verdict": "go", "notes": "Use an explicit provider above all cart consumers",
            }) == []
            await loop.capture_from_build({
                "stack": "react", "verdict": "go", "proof_passed": True,
                "notes": "Use an explicit provider above all cart consumers",
            })
            assert "provider above" in (await loop.inject_for_build("react")).as_advice()
        finally:
            await store.close()
    asyncio.run(run())


def test_saved_persona_changes_operation_explanations_not_essential_facts(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = MemoryStore(settings)
        await store.init_db()
        summary = "Correction saved. It cannot change proof or tool permissions."
        details = ("It applies only to the selected project.", "Retire it to exclude future recall.")
        try:
            before = await store.operation_message(summary=summary, details=details)
            await store.set_persona({"tone": "warm", "verbosity": "detailed", "structure": "bullets"})
            await store.close()
            store = MemoryStore(settings)
            await store.init_db()
            after = await store.operation_message(summary=summary, details=details)
            assert after != before
            assert summary in before and summary in after
            assert all(detail in after for detail in details)
            assert after.startswith("- ")
            await store.set_persona({"tone": "direct", "verbosity": "concise", "structure": "prose"})
            concise = await store.operation_message(summary=summary, details=details)
            assert concise == summary
            assert await store.reset_persona()
            assert await store.operation_message(summary=summary, details=details) == before
        finally:
            await store.close()
    asyncio.run(run())
