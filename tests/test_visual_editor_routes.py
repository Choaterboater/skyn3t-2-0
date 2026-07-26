from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from skyn3t.core.events import EventBus
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.visual_editor import EMPTY_SHA256, VisualEditor
from skyn3t.web.routes import (
    visual_editor_apply,
    visual_editor_inspect,
    visual_editor_style,
)
from skyn3t.worktree import source_tree_snapshot


def _state(tmp_path):
    projects = tmp_path / "Projects"
    project = projects / "demo"
    project.mkdir(parents=True)
    (project / "index.html").write_text(
        "<html><head></head><body><h1 class=\"hero\">Old title</h1></body></html>"
    )
    BuildManifest(
        slug="demo",
        brief="Demo",
        stack="static",
        status="completed",
        verdict="go",
        score=90.0,
        files=["index.html"],
    ).save(project)
    return (
        SimpleNamespace(
            settings=SimpleNamespace(
                projects_dir=projects,
                execution_backend="inline",
                run_generated_tests=False,
                generated_test_timeout=5,
                run_generated_build=False,
                generated_build_timeout=5,
                proof_ladder_required=False,
                degraded_proof_score_cap=74.0,
            ),
            event_bus=EventBus(),
            memory=None,
        ),
        project,
    )


async def test_visual_editor_inspect_and_text_edit_are_source_mapped(tmp_path):
    state, project = _state(tmp_path)
    signature = {"tag": "h1", "classes": ["hero"], "text": "Old title"}

    inspected = await visual_editor_inspect(state, "demo", signature)
    occurrence = inspected["occurrences"][0]
    edited = await visual_editor_apply(
        state,
        "demo",
        {
            "kind": "text",
            "relative_path": occurrence["relative_path"],
            "base_sha": occurrence["current_sha"],
            "signature": signature,
            "occurrence_id": occurrence["occurrence_id"],
            "line": occurrence["line"],
            "value": "New title",
        },
    )

    assert "New title" in (project / "index.html").read_text()
    assert edited["edit"]["operation"] == "text"
    assert edited["verification"]["proof"]["passed"] is True
    assert edited["manifest"]["status"] == "completed"


async def test_visual_editor_token_edit_loads_managed_stylesheet(tmp_path):
    state, project = _state(tmp_path)
    style = await visual_editor_style(state, "demo")

    edited = await visual_editor_apply(
        state,
        "demo",
        {
            "kind": "design_token",
            "base_sha": style["style"]["current_sha"],
            "css_property": "--accent",
            "value": "#ff5500",
        },
    )

    assert style["style"]["current_sha"] == EMPTY_SHA256
    assert edited["integration"]["integrated"] is True
    assert (project / "skyn3t-visual-editor.css").is_file()
    assert 'data-skyn3t-visual-editor="bridge"' in (
        project / "index.html"
    ).read_text()
    disk_manifest = BuildManifest.load(project)
    assert disk_manifest is not None
    assert disk_manifest.extra["visual_editor"]["last_edit"]["operation"].startswith(
        "design_token"
    )


async def test_visual_editor_marks_manifest_no_go_before_mutating_and_binds_proof(
    tmp_path,
    monkeypatch,
):
    state, project = _state(tmp_path)
    signature = {"tag": "h1", "classes": ["hero"], "text": "Old title"}
    occurrence = (await visual_editor_inspect(state, "demo", signature))["occurrences"][0]
    observed = {}
    original_apply = VisualEditor.apply_edit

    def inspect_pending_manifest(editor, request):
        pending = BuildManifest.load(project)
        assert pending is not None
        observed["status"] = pending.status
        observed["verdict"] = pending.verdict
        observed["pending"] = pending.extra["visual_editor"]["verification_pending"]
        return original_apply(editor, request)

    monkeypatch.setattr(VisualEditor, "apply_edit", inspect_pending_manifest)

    edited = await visual_editor_apply(
        state,
        "demo",
        {
            "kind": "text",
            "relative_path": occurrence["relative_path"],
            "base_sha": occurrence["current_sha"],
            "signature": signature,
            "occurrence_id": occurrence["occurrence_id"],
            "line": occurrence["line"],
            "value": "Bound title",
        },
    )

    disk_manifest = BuildManifest.load(project)
    assert disk_manifest is not None
    binding = edited["verification"]["source_tree"]
    snapshot = source_tree_snapshot(project)
    assert observed == {
        "status": "completed_no_go",
        "verdict": "no_go",
        "pending": True,
    }
    assert binding["matched"] is True
    assert binding["source_tree_sha256"] == snapshot["sha256"]
    assert disk_manifest.status == "completed"
    assert disk_manifest.verdict == "go"
    assert disk_manifest.extra["visual_editor"]["verification_pending"] is False


async def test_visual_editor_rejects_proof_when_source_tree_changes_during_proof(
    tmp_path,
    monkeypatch,
):
    state, project = _state(tmp_path)
    signature = {"tag": "h1", "classes": ["hero"], "text": "Old title"}
    occurrence = (await visual_editor_inspect(state, "demo", signature))["occurrences"][0]

    class PassingProof:
        passed = True

        @staticmethod
        def to_dict():
            return {"passed": True}

    def mutating_proof(project_dir, *_args, **_kwargs):
        source = project_dir / "index.html"
        source.write_text(
            source.read_text(encoding="utf-8") + "\n<!-- changed during proof -->",
            encoding="utf-8",
        )
        return PassingProof()

    monkeypatch.setattr("skyn3t.studio.proof_run.proof_run", mutating_proof)

    edited = await visual_editor_apply(
        state,
        "demo",
        {
            "kind": "text",
            "relative_path": occurrence["relative_path"],
            "base_sha": occurrence["current_sha"],
            "signature": signature,
            "occurrence_id": occurrence["occurrence_id"],
            "line": occurrence["line"],
            "value": "Changed title",
        },
    )

    disk_manifest = BuildManifest.load(project)
    assert disk_manifest is not None
    assert edited["verification"]["proof"]["passed"] is True
    assert edited["verification"]["source_tree"]["matched"] is False
    assert edited["verification"]["passed"] is False
    assert disk_manifest.status == "completed_no_go"
    assert disk_manifest.verdict == "no_go"


async def test_visual_editor_serializes_proof_per_project(tmp_path, monkeypatch):
    state, project = _state(tmp_path)
    second = project / "about.html"
    second.write_text(
        '<html><body><h2 id="about">Old about</h2></body></html>',
        encoding="utf-8",
    )
    first_signature = {"tag": "h1", "classes": ["hero"], "text": "Old title"}
    second_signature = {"tag": "h2", "element_id": "about", "text": "Old about"}
    first = (await visual_editor_inspect(state, "demo", first_signature))["occurrences"][0]
    second_occurrence = (
        await visual_editor_inspect(state, "demo", second_signature)
    )["occurrences"][0]
    counter_lock = threading.Lock()
    active = 0
    maximum = 0

    class PassingProof:
        passed = True

        @staticmethod
        def to_dict():
            return {"passed": True}

    def slow_proof(*_args, **_kwargs):
        nonlocal active, maximum
        with counter_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with counter_lock:
            active -= 1
        return PassingProof()

    monkeypatch.setattr("skyn3t.studio.proof_run.proof_run", slow_proof)

    first_result, second_result = await asyncio.gather(
        visual_editor_apply(
            state,
            "demo",
            {
                "kind": "text",
                "relative_path": first["relative_path"],
                "base_sha": first["current_sha"],
                "signature": first_signature,
                "occurrence_id": first["occurrence_id"],
                "line": first["line"],
                "value": "New title",
            },
        ),
        visual_editor_apply(
            state,
            "demo",
            {
                "kind": "text",
                "relative_path": second_occurrence["relative_path"],
                "base_sha": second_occurrence["current_sha"],
                "signature": second_signature,
                "occurrence_id": second_occurrence["occurrence_id"],
                "line": second_occurrence["line"],
                "value": "New about",
            },
        ),
    )

    assert maximum == 1
    assert first_result["verification"]["passed"] is True
    assert second_result["verification"]["passed"] is True
