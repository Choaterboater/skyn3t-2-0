"""Offline tests for the generative agents.

No network, no heavy deps. Exercises the stub LLM backend and verifies the
CodeAgent emits a genuinely runnable scaffold (design rule #1).
"""

from __future__ import annotations

from skyn3t.agents._common import KNOWN_STACKS, detect_stack, parse_json, slugify
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.agents.architect import ArchitectAgent
from skyn3t.agents.brainstorm import BrainstormAgent
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.agents.designer import DesignerAgent
from skyn3t.agents.research_agent import ResearchAgent
from skyn3t.agents.writer import WriterAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


def test_detect_stack_defaults_and_signals():
    assert detect_stack(brief="a react counter app") == "react_vite"
    assert detect_stack(brief="a python cli tool") == "python_cli"
    assert detect_stack(brief="a fastapi rest api") == "fastapi"
    assert detect_stack(brief="") == "react_vite"  # default
    assert detect_stack(plan={"stack": "node_express"}) == "node_express"


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('prefix {"b": 2} suffix') == {"b": 2}
    assert parse_json("not json") is None


def test_scaffold_react_vite_is_runnable_shape():
    files = scaffold_for("react_vite", "demo", "A counter")
    for required in ("package.json", "index.html", "src/main.jsx", "src/App.jsx", "vite.config.js"):
        assert required in files
    assert "export default" in files["src/App.jsx"]
    assert "/src/main.jsx" in files["index.html"]
    assert '"react"' in files["package.json"]


def test_all_known_stacks_scaffold():
    for stack in KNOWN_STACKS:
        files = scaffold_for(stack, "demo", "brief")
        assert files, f"{stack} produced no files"


async def test_code_agent_writes_runnable_offline(tmp_path):
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()
    assert agent.llm.backend == "stub"

    task = TaskRequest(
        type="codegen",
        payload={"brief": "a react counter app", "slug": "counter",
                 "worktree_dir": str(tmp_path)},
        capabilities_required=("codegen",),
    )
    result = await agent.run(task)
    assert result.success
    assert result.output["stack"] == "react_vite"
    assert result.output["files_written"] >= 5

    # The actual files exist on disk and form a runnable Vite+React app.
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / "src" / "App.jsx").is_file()
    app = (tmp_path / "src" / "App.jsx").read_text()
    assert "export default" in app
    main = (tmp_path / "src" / "main.jsx").read_text()
    assert "import App" in main


async def test_degraded_flag_does_not_leak_across_builds(tmp_path):
    """The singleton CodeAgent must not carry a prior build's `degraded` flag
    into a fresh, clean build (it would force a false no_go + halved score)."""
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()
    # Simulate a previous build having marked this singleton degraded.
    agent.metadata["degraded"] = True
    agent.metadata["degraded_reason"] = "prior build under-delivered"
    task = TaskRequest(
        type="codegen",
        payload={"brief": "a react counter app", "slug": "counter2",
                 "worktree_dir": str(tmp_path)},
    )
    result = await agent.run(task)
    assert result.success
    assert "degraded" not in result.output, result.output.get("degraded_reason")


async def test_code_agent_python_cli(tmp_path):
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()
    task = TaskRequest(
        type="codegen",
        payload={"brief": "a command line greeting tool", "slug": "greet",
                 "worktree_dir": str(tmp_path)},
    )
    result = await agent.run(task)
    assert result.success
    assert result.output["stack"] == "python_cli"
    assert (tmp_path / "main.py").is_file()


async def test_code_agent_confines_writes(tmp_path):
    """Files with traversal paths must not escape the worktree."""
    bus = EventBus()
    agent = CodeAgent(event_bus=bus)
    await agent.start()
    wt = tmp_path / "wt"
    result = await agent.run(TaskRequest(
        type="codegen",
        payload={"brief": "react app", "worktree_dir": str(wt)},
    ))
    assert result.success
    for f in result.output["files"]:
        assert not f.startswith("..")


async def test_research_agent_offline_non_empty():
    bus = EventBus()
    agent = ResearchAgent(event_bus=bus)
    await agent.start()
    result = await agent.run(TaskRequest(
        type="research", payload={"brief": "a fastapi todo api"}))
    assert result.success
    out = result.output
    assert out["stack"] == "fastapi"
    assert out["recommended_libraries"]
    assert out["approach"]  # non-empty plan of attack


async def test_brainstorm_architect_designer_writer_offline(tmp_path):
    bus = EventBus()
    for agent in (
        BrainstormAgent(event_bus=bus),
        ArchitectAgent(event_bus=bus),
        DesignerAgent(event_bus=bus),
    ):
        await agent.start()
        result = await agent.run(TaskRequest(
            type=agent.agent_type,
            payload={"brief": "a react counter", "slug": "counter"}))
        assert result.success, agent.name

    arch = ArchitectAgent(event_bus=bus)
    await arch.start()
    plan_res = await arch.run(TaskRequest(
        type="architecture", payload={"brief": "a react counter", "slug": "counter"}))
    plan = plan_res.output["plan"]
    assert plan["files"]
    assert plan["stack"] == "react_vite"

    # Writer writes a README into the worktree.
    writer = WriterAgent(event_bus=bus)
    await writer.start()
    wres = await writer.run(TaskRequest(
        type="documentation",
        payload={"brief": "a react counter", "slug": "counter",
                 "worktree_dir": str(tmp_path)}))
    assert wres.success
    assert (tmp_path / "README.md").is_file()
    # Offline README must be comprehensive, not a one-line stub (design rule #1).
    readme = (tmp_path / "README.md").read_text()
    assert len(readme.encode()) >= 400
    for section in ("## Features", "## Installation", "## Usage", "## Project structure"):
        assert section in readme, f"writer README missing {section}"


async def test_code_improver_addresses_gaps(tmp_path):
    # Write an App.jsx missing its default export, then improve it.
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text("function App() { return null }\n")
    bus = EventBus()
    improver = CodeImproverAgent(event_bus=bus)
    await improver.start()
    result = await improver.run(TaskRequest(
        type="code_improve",
        payload={"brief": "react app", "worktree_dir": str(tmp_path),
                 "gaps": ["src/App.jsx is missing a default export"]}))
    assert result.success
    assert "export default" in (src / "App.jsx").read_text()


def test_slugify():
    assert slugify("My Cool App!") == "my-cool-app"
    assert slugify("") == "app"
