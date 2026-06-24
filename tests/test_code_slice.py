"""CodeAgent slice mode — Hermes orchestrator-worker sub-agent.

A slice writes ONLY its own files and must NOT trigger the whole-app scaffold
floor / under-delivery revert (a slice is small by design). The offline stub
path writes exactly the slice's files; the agentic path keeps a small delivery.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

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

    async def fake_agentic_build(prompt, workdir, timeout=None, model=None):
        comp = pathlib.Path(workdir, "src/components")
        comp.mkdir(parents=True, exist_ok=True)
        (comp / "Card.jsx").write_text(
            "export default function Card(){ return null }\n", encoding="utf-8")
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

    async def fake_agentic_build(prompt, workdir, timeout=None, model=None):
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
