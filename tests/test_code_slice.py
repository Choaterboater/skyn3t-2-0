"""CodeAgent slice mode — Hermes orchestrator-worker sub-agent.

A slice writes ONLY its own files and must NOT trigger the whole-app scaffold
floor / under-delivery revert (a slice is small by design). The offline stub
path writes exactly the slice's files; the agentic path keeps a small delivery.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


def _backend_property():
    return property(lambda self: getattr(self, "_backend", "stub"))


async def test_slice_stub_writes_only_its_files(tmp_path):
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()  # stub backend
    task = TaskRequest(
        type="codegen",
        payload={
            "brief": "a react dashboard", "slug": "dash", "stack": "react",
            "worktree_dir": str(tmp_path),
            "plan": {"stack": "react", "files": [
                {"path": "src/components/Card.jsx"}, {"path": "src/components/Chart.jsx"}]},
            "slice_scope": {
                "name": "frontend",
                "files": ["src/components/Card.jsx", "src/components/Chart.jsx"],
                "manifest": "  api/main.py — fastapi backend",
            },
        },
        capabilities_required=("codegen",),
    )
    result = await agent.run(task)
    assert result.success
    assert result.output["slice"] == "frontend"
    written = set(result.output["files"])
    # Exactly the slice's files — nothing else (no whole-app scaffold).
    assert written == {"src/components/Card.jsx", "src/components/Chart.jsx"}
    assert (tmp_path / "src/components/Card.jsx").exists()


async def test_slice_agentic_keeps_small_delivery_no_scaffold_floor(tmp_path):
    # The whole-app path would revert a sub-threshold delivery to the full
    # scaffold; slice mode must KEEP the small slice and NOT lay a scaffold.
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()

    async def fake_agentic_build(prompt, workdir, timeout=None, model=None, **kwargs):
        comp = pathlib.Path(workdir, "src/components")
        comp.mkdir(parents=True, exist_ok=True)
        (comp / "Card.jsx").write_text(
            "export default function Card(){ return null }\n", encoding="utf-8")
        assert kwargs["allowed_paths"] == ["src/components/Card.jsx"]
        assert kwargs["enforce_antistub"] is False
        assert kwargs["verify_on_stop"] is False
        return {"ok": True, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=_backend_property):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        task = TaskRequest(
            type="codegen",
            payload={
                "brief": "x", "slug": "x", "stack": "react",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "react", "files": [{"path": "src/components/Card.jsx"}]},
                "slice_scope": {
                    "name": "frontend",
                    "files": ["src/components/Card.jsx"],
                    "manifest": "  api/main.py — backend",
                },
            },
            capabilities_required=("codegen",),
        )
        result = await agent.run(task)

    written = set(result.output["files"])
    assert "src/components/Card.jsx" in written
    # No scaffold floor: the full react scaffold was NOT laid down.
    assert "package.json" not in written
    assert "src/main.jsx" not in written


async def test_slice_agentic_passes_model_override(tmp_path):
    # The slice's tier model must reach agentic_build (mixed-by-slice routing).
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()
    seen: dict = {}

    async def fake_agentic_build(prompt, workdir, timeout=None, model=None, **kwargs):
        seen["model"] = model
        pathlib.Path(workdir, "api").mkdir(parents=True, exist_ok=True)
        pathlib.Path(workdir, "api/main.py").write_text("# api\n", encoding="utf-8")
        return {"ok": True, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=_backend_property):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        task = TaskRequest(
            type="codegen",
            payload={
                "brief": "x", "slug": "x", "stack": "fastapi",
                "worktree_dir": str(tmp_path),
                "model_override": "anthropic/strong-model",
                "plan": {"stack": "fastapi", "files": [{"path": "api/main.py"}]},
                "slice_scope": {"name": "backend", "files": ["api/main.py"], "manifest": ""},
            },
            capabilities_required=("codegen",),
        )
        await agent.run(task)
    assert seen["model"] == "anthropic/strong-model"


async def test_slice_partial_delivery_retries_and_remains_degraded(tmp_path):
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()
    calls = {"n": 0}
    verify_overrides = []
    original = "export const preserved = 'substantial partial source';\n"

    async def fake_agentic_build(prompt, workdir, **kwargs):
        calls["n"] += 1
        verify_overrides.append(kwargs["verify_on_stop"])
        target = pathlib.Path(workdir, "src/components/Card.jsx")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original, encoding="utf-8")
        return {
            "ok": False,
            "completed": False,
            "backend": "claude_cli",
            "error": "session ended before finish",
        }

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=_backend_property):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={
                "brief": "x", "slug": "x", "stack": "react",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "react", "files": [
                    {"path": "src/components/Card.jsx"},
                    {"path": "src/components/Chart.jsx"},
                ]},
                "slice_scope": {
                    "name": "frontend",
                    "files": ["src/components/Card.jsx", "src/components/Chart.jsx"],
                    "manifest": "",
                },
            },
            capabilities_required=("codegen",),
        ))

    assert calls["n"] == 2
    assert verify_overrides == [False, False]
    assert result.output["degraded"] is True
    assert result.output["agentic"]["complete"] is False
    assert result.output["agentic"]["missing_files"] == ["src/components/Chart.jsx"]
    assert (tmp_path / "src/components/Card.jsx").read_text() == original
    assert (tmp_path / "src/components/Chart.jsx").exists(), "missing path gets a floor"
    assert "RESUME THIS SLICE IN PLACE" in result.output["prompts"][1]["text"]


async def test_slice_resume_can_fill_every_owned_file(tmp_path):
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()
    calls = {"n": 0}

    async def fake_agentic_build(prompt, workdir, **kwargs):
        calls["n"] += 1
        name = "Card.jsx" if calls["n"] == 1 else "Chart.jsx"
        target = pathlib.Path(workdir, "src/components", name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"export const name = '{name}';\n", encoding="utf-8")
        return {
            "ok": calls["n"] == 2,
            "completed": calls["n"] == 2,
            "backend": "claude_cli",
        }

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=_backend_property):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={
                "brief": "x", "slug": "x", "stack": "react",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "react", "files": [
                    {"path": "src/components/Card.jsx"},
                    {"path": "src/components/Chart.jsx"},
                ]},
                "slice_scope": {
                    "name": "frontend",
                    "files": ["src/components/Card.jsx", "src/components/Chart.jsx"],
                    "manifest": "",
                },
            },
            capabilities_required=("codegen",),
        ))

    assert calls["n"] == 2
    assert "degraded" not in result.output
    assert result.output["agentic"]["complete"] is True
    assert result.output["agentic"]["missing_files"] == []


async def test_slice_removes_and_reports_out_of_scope_writes(tmp_path):
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()

    async def fake_agentic_build(prompt, workdir, **kwargs):
        owned = pathlib.Path(workdir, "src/components/Card.jsx")
        owned.parent.mkdir(parents=True, exist_ok=True)
        owned.write_text("export default function Card(){ return null }\n", encoding="utf-8")
        outside = pathlib.Path(workdir, "api/main.py")
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("overwritten = True\n", encoding="utf-8")
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=_backend_property):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={
                "brief": "x", "slug": "x", "stack": "react",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "react", "files": [
                    {"path": "src/components/Card.jsx"},
                ]},
                "slice_scope": {
                    "name": "frontend",
                    "files": ["src/components/Card.jsx"],
                    "manifest": "  api/main.py - backend",
                },
            },
            capabilities_required=("codegen",),
        ))

    assert "degraded" not in result.output
    assert result.output["agentic"]["out_of_scope_files"] == ["api/main.py"]
    assert not (tmp_path / "api/main.py").exists()
    assert set(result.output["files"]) == {"src/components/Card.jsx"}


async def test_owned_symlink_cannot_satisfy_slice_completeness(tmp_path):
    outside = tmp_path.parent / "outside-slice.jsx"
    outside.write_text("export const escaped = true;\n", encoding="utf-8")
    probe = tmp_path / "probe-link"
    try:
        probe.symlink_to(outside)
        probe.unlink()
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    agent = CodeAgent(event_bus=EventBus())
    await agent.start()

    async def fake_agentic_build(prompt, workdir, **kwargs):
        link = pathlib.Path(workdir, "src/Owned.jsx")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=_backend_property):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={
                "brief": "x", "slug": "x", "stack": "react",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "react", "files": [{"path": "src/Owned.jsx"}]},
                "slice_scope": {
                    "name": "frontend", "files": ["src/Owned.jsx"], "manifest": "",
                },
            },
            capabilities_required=("codegen",),
        ))

    assert result.output["degraded"] is True
    assert result.output["agentic"]["missing_files"] == ["src/Owned.jsx"]
    assert result.output["agentic"]["out_of_scope_files"] == ["src/Owned.jsx"]
    assert not (tmp_path / "src/Owned.jsx").is_symlink()
    assert outside.read_text(encoding="utf-8").startswith("export const escaped")


async def test_owned_svg_and_png_survive_slice_without_text_roundtrip(tmp_path):
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><circle cx='4' cy='4' r='3'/></svg>"
    png = b"\x89PNG\r\n\x1a\n\x00\x01slice-binary-payload"
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()

    async def fake_agentic_build(prompt, workdir, **kwargs):
        public = pathlib.Path(workdir, "public")
        public.mkdir(parents=True, exist_ok=True)
        (public / "logo.svg").write_bytes(svg)
        (public / "texture.png").write_bytes(png)
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=_backend_property):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={
                "brief": "x", "slug": "x", "stack": "react",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "react", "files": [
                    {"path": "public/logo.svg"}, {"path": "public/texture.png"},
                ]},
                "slice_scope": {
                    "name": "assets",
                    "files": ["public/logo.svg", "public/texture.png"],
                    "manifest": "",
                },
            },
            capabilities_required=("codegen",),
        ))

    assert "degraded" not in result.output
    assert result.output["agentic"]["missing_files"] == []
    assert set(result.output["files"]) == {"public/logo.svg", "public/texture.png"}
    assert (tmp_path / "public/logo.svg").read_bytes() == svg
    assert (tmp_path / "public/texture.png").read_bytes() == png


async def test_unconfirmed_slice_does_not_overwrite_present_binary_fallback(tmp_path):
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='9' height='9'/></svg>"
    agent = CodeAgent(event_bus=EventBus())
    await agent.start()

    async def fake_agentic_build(prompt, workdir, **kwargs):
        target = pathlib.Path(workdir, "public/logo.svg")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(svg)
        return {
            "ok": False,
            "completed": False,
            "backend": "claude_cli",
            "error": "terminal result was not received",
        }

    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    with patch.object(type(agent.llm), "backend", new_callable=_backend_property):
        agent.llm.agentic_build = fake_agentic_build  # type: ignore[method-assign]
        result = await agent.run(TaskRequest(
            type="codegen",
            payload={
                "brief": "x", "slug": "x", "stack": "react",
                "worktree_dir": str(tmp_path),
                "plan": {"stack": "react", "files": [{"path": "public/logo.svg"}]},
                "slice_scope": {
                    "name": "assets", "files": ["public/logo.svg"], "manifest": "",
                },
            },
            capabilities_required=("codegen",),
        ))

    assert result.output["degraded"] is True
    assert result.output["agentic"]["missing_files"] == []
    assert (tmp_path / "public/logo.svg").read_bytes() == svg
