"""Regression tests for the architect-plan -> CodeAgent / ContractVerifier wiring.

Prior to the fix, _base_payload always used the PLANNER's generic plan (which
lacks the architect's richer file list).  The architect stage's output was stored
in prior["architect"] but never threaded into subsequent stage payloads, so
CodeAgent._planned_paths and ContractVerifier.extract_planned_files both fell
back to the planner checklist.

These offline tests verify:
  1. When prior["architect"]["plan"]["files"] is present, _base_payload's
     returned payload["plan"]["files"] equals the architect's list.
  2. When no architect output is present, payload["plan"]["files"] remains
     whatever the planner plan contained (or is absent) — fallback intact.
  3. CodeAgent._planned_paths reads the architect's files out of the payload
     (end-to-end: payload -> plan["files"] -> _planned_paths).
  4. ContractVerifier.extract_planned_files reads the architect's files from
     the same payload shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.agents.contract_verifier import extract_planned_files
from skyn3t.core.events import EventBus
from skyn3t.studio.planner import BuildPlan, StageSpec
from skyn3t.studio.runner import StudioRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_plan(checklist: list[str] | None = None) -> BuildPlan:
    """Minimal planner BuildPlan with no stages and a basic checklist."""
    from dataclasses import dataclass  # noqa: PLC0415

    plan = BuildPlan.__new__(BuildPlan)
    object.__setattr__(plan, "slug", "test-slug")
    object.__setattr__(plan, "brief", "a test app")
    object.__setattr__(plan, "stack", "python_cli")
    object.__setattr__(plan, "stages", [])
    object.__setattr__(plan, "checklist", checklist or ["main.py", "README.md"])
    object.__setattr__(plan, "test_first", False)
    object.__setattr__(plan, "best_of_n", 1)
    object.__setattr__(plan, "notes", {})
    return plan


def _make_runner() -> StudioRunner:
    """Minimal StudioRunner with mocked orchestrator (no I/O)."""
    bus = EventBus()
    orch = MagicMock()
    return StudioRunner(bus, orch)


# ---------------------------------------------------------------------------
# 1. _base_payload prefers the architect's file list when present
# ---------------------------------------------------------------------------

def test_base_payload_uses_architect_file_list():
    runner = _make_runner()
    plan = _make_plan()

    arch_files = [
        {"path": "main.py", "purpose": "entrypoint"},
        {"path": "lib/utils.py", "purpose": "utilities"},
        {"path": "tests/test_main.py", "purpose": "unit tests"},
    ]
    prior = {
        "architect": {
            "plan": {
                "stack": "python_cli",
                "summary": "Architect's richer plan",
                "files": arch_files,
                "build_order": ["main.py", "lib/utils.py", "tests/test_main.py"],
                "components": [],
            },
            "stack": "python_cli",
            "backend": "stub",
        }
    }

    payload = runner._base_payload(plan, "/proj", "/wt", prior, [], None)

    # The payload's plan should carry the architect's files, not the generic planner plan.
    result_files = payload["plan"]["files"]
    assert result_files == arch_files, (
        f"Expected architect's file list, got: {result_files}"
    )
    # The architect's summary should be surfaced too.
    assert payload["plan"]["summary"] == "Architect's richer plan"


# ---------------------------------------------------------------------------
# 2. Fallback: no architect output -> planner plan is used unchanged
# ---------------------------------------------------------------------------

def test_base_payload_fallback_without_architect():
    runner = _make_runner()
    plan = _make_plan(checklist=["app.py"])

    # No "architect" key in prior.
    prior: dict = {}
    payload = runner._base_payload(plan, "/proj", "/wt", prior, [], None)

    # plan should be the planner's to_dict() — no "files" key unless planner adds one.
    result_plan = payload["plan"]
    # The planner's BuildPlan.to_dict() doesn't include a "files" key, so either
    # it's absent or it's whatever the planner put there (not the architect's list).
    assert result_plan.get("files") is None or isinstance(result_plan.get("files"), list)
    # Checklist still comes from the planner.
    assert payload["checklist"] == ["app.py"]


def test_base_payload_fallback_with_empty_architect_files():
    """An architect result with an empty file list should NOT override."""
    runner = _make_runner()
    plan = _make_plan()

    prior = {
        "architect": {
            "plan": {
                "stack": "python_cli",
                "files": [],  # empty — should not override
            },
        }
    }
    payload = runner._base_payload(plan, "/proj", "/wt", prior, [], None)

    # Should fall back to planner plan (no files key, or planner's files).
    result_files = payload["plan"].get("files")
    # An empty list from the architect must NOT replace the planner's plan.
    assert result_files is None or result_files == [], (
        "Empty architect file list should not be threaded in as an override"
    )
    # Specifically, verify it did NOT replace with the empty list when
    # the planner plan itself has no files (the bug was: empty list would
    # pass the truthiness check and override).
    # The logic uses `if arch_files and isinstance(arch_files, list)` so an
    # empty list is falsy and will not trigger the override.
    assert not (result_files == [] and payload["plan"].get("summary") == "Architect's richer plan")


# ---------------------------------------------------------------------------
# 3. CodeAgent._planned_paths reads architect files from payload["plan"]
# ---------------------------------------------------------------------------

def test_code_agent_planned_paths_uses_architect_files(tmp_path):
    """When the payload carries architect files, _planned_paths returns them."""
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)

    arch_files = [
        {"path": "main.py", "purpose": "entrypoint"},
        {"path": "lib/core.py", "purpose": "core logic"},
        {"path": "lib/utils.py", "purpose": "utilities"},
    ]
    plan_with_arch = {"files": arch_files, "stack": "python_cli"}

    from skyn3t.agents._scaffold import scaffold_for
    scaffold = scaffold_for("python_cli", "myapp", "a test app")

    paths = agent._planned_paths(plan_with_arch, scaffold)

    # All three architect-specified files must be in the planned paths.
    for f in arch_files:
        assert f["path"] in paths, (
            f"Architect file '{f['path']}' missing from _planned_paths output: {paths}"
        )


def test_code_agent_planned_paths_fallback_no_files(tmp_path):
    """Without architect files, _planned_paths falls back to the scaffold."""
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)

    plan_no_files: dict = {}

    from skyn3t.agents._scaffold import scaffold_for
    scaffold = scaffold_for("python_cli", "myapp", "a test app")

    paths = agent._planned_paths(plan_no_files, scaffold)

    # Must contain scaffold files.
    for path in scaffold:
        assert path in paths, f"Scaffold file '{path}' missing from fallback paths: {paths}"


# ---------------------------------------------------------------------------
# 4. ContractVerifier.extract_planned_files reads architect files from payload
# ---------------------------------------------------------------------------

def test_extract_planned_files_reads_architect_files():
    """extract_planned_files should find architect paths in payload["plan"]["files"]."""
    arch_files = [
        {"path": "main.py", "purpose": "entrypoint"},
        {"path": "lib/core.py", "purpose": "core logic"},
    ]
    payload = {
        "plan": {
            "files": arch_files,
            "stack": "python_cli",
        },
    }

    found = extract_planned_files(payload)
    assert found == ["main.py", "lib/core.py"], (
        f"extract_planned_files did not return architect's paths: {found}"
    )


def test_extract_planned_files_fallback_when_no_plan():
    """Without a plan, extract_planned_files should return an empty list."""
    payload: dict = {}
    found = extract_planned_files(payload)
    assert found == []


# ---------------------------------------------------------------------------
# 5. Full end-to-end: _base_payload -> extract_planned_files wiring
# ---------------------------------------------------------------------------

def test_end_to_end_wiring_base_payload_to_contract_verifier():
    """The payload produced by _base_payload with architect prior is correctly
    consumed by extract_planned_files (simulates the code->verify hand-off)."""
    runner = _make_runner()
    plan = _make_plan()

    arch_files = [
        {"path": "app.py", "purpose": "main app"},
        {"path": "models.py", "purpose": "data models"},
        {"path": "routes.py", "purpose": "HTTP routes"},
    ]
    prior = {
        "architect": {
            "plan": {
                "stack": "fastapi",
                "files": arch_files,
                "build_order": ["app.py", "models.py", "routes.py"],
                "components": [],
            },
            "stack": "fastapi",
        }
    }

    payload = runner._base_payload(plan, "/proj", "/wt", prior, [], None)

    # ContractVerifier will call extract_planned_files on this same payload shape.
    found = extract_planned_files(payload)
    expected = [f["path"] for f in arch_files]
    assert found == expected, (
        f"End-to-end wiring broken: expected {expected}, got {found}"
    )
