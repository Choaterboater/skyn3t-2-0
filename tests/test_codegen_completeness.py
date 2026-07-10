"""Agentic codegen must prove the approved architecture is complete."""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


def _substantial_source(label: str) -> str:
    return "\n".join(
        f"export const {label}_{index} = {index};" for index in range(1400)
    )


def _plan(count: int = 16) -> dict:
    return {
        "stack": "react_vite",
        "summary": "A complete multi-page operations application.",
        "files": [
            {"path": f"src/features/feature_{index}.js", "purpose": f"feature {index}"}
            for index in range(count)
        ],
    }


class _ScriptedAgenticLLM:
    backend = "claude_cli"
    supports_agentic = True
    last_model = None
    last_route = None
    routes: list = []

    def __init__(self, callback):
        self.callback = callback
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []

    async def agentic_build(self, prompt, workdir, **kwargs):
        self.prompts.append(prompt)
        self.kwargs.append(dict(kwargs))
        return await self.callback(len(self.prompts), Path(workdir))


async def _run(tmp_path, llm, plan):
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    result = await agent.run(TaskRequest(
        type="codegen",
        payload={
            "brief": "a full operations application",
            "slug": "ops",
            "stack": plan.get("stack", "react_vite"),
            "plan": plan,
            "worktree_dir": str(tmp_path),
        },
        capabilities_required=("codegen",),
    ))
    return agent, result


async def test_large_seven_of_sixteen_delivery_is_preserved_but_no_go(tmp_path):
    plan = _plan()

    async def write_only_seven(_attempt, root):
        for index in range(7):
            target = root / plan["files"][index]["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_substantial_source(f"feature_{index}"), encoding="utf-8")
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    llm = _ScriptedAgenticLLM(write_only_seven)
    agent, result = await _run(tmp_path, llm, plan)

    assert len(llm.prompts) == 2, "missing architecture files require a resume"
    assert result.output["degraded"] is True
    assert result.output["agentic"]["complete"] is False
    assert result.output["agentic"]["planned_files"] == 16
    assert len(result.output["agentic"]["missing_files"]) == 9
    assert (tmp_path / plan["files"][0]["path"]).exists(), "substantial partial work is kept"
    assert "degraded" not in agent.metadata, "task state must not leak onto the singleton"


async def test_resume_prompt_is_compact_and_can_complete_missing_architecture(tmp_path):
    plan = _plan(4)

    async def recover(attempt, root):
        indexes = [0] if attempt == 1 else [1, 2, 3]
        for index in indexes:
            target = root / plan["files"][index]["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_substantial_source(f"feature_{index}"), encoding="utf-8")
        if attempt == 1:
            return {
                "ok": False,
                "completed": False,
                "backend": "claude_cli",
                "error": "provider session ended before finish",
            }
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    llm = _ScriptedAgenticLLM(recover)
    _agent, result = await _run(tmp_path, llm, plan)

    assert len(llm.prompts) == 2
    assert llm.kwargs[0]["planned_paths"] == sorted(
        item["path"] for item in plan["files"]
    )
    assert len(llm.prompts[1]) < len(llm.prompts[0])
    assert "RESUME IN PLACE" in llm.prompts[1]
    assert plan["files"][1]["path"] in llm.prompts[1]
    assert "degraded" not in result.output
    assert result.output["agentic"]["complete"] is True
    assert result.output["agentic"]["missing_files"] == []


async def test_planned_binary_assets_count_as_present_without_text_roundtrip(tmp_path):
    plan = {
        "stack": "react_vite",
        "summary": "Application code with approved visual assets.",
        "files": [
            {"path": "src/App.jsx", "purpose": "application"},
            {"path": "public/logo.svg", "purpose": "brand mark"},
            {"path": "public/texture.png", "purpose": "visual texture"},
        ],
    }
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0h8v8z'/></svg>"
    png = b"\x89PNG\r\n\x1a\n\x00\x00binary-test-payload"

    async def write_app_and_assets(_attempt, root):
        source = root / "src/App.jsx"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(_substantial_source("app"), encoding="utf-8")
        public = root / "public"
        public.mkdir(parents=True, exist_ok=True)
        (public / "logo.svg").write_bytes(svg)
        (public / "texture.png").write_bytes(png)
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    llm = _ScriptedAgenticLLM(write_app_and_assets)
    _agent, result = await _run(tmp_path, llm, plan)

    assert "degraded" not in result.output
    assert result.output["agentic"]["missing_files"] == []
    assert {"public/logo.svg", "public/texture.png"} <= set(result.output["files"])
    assert (tmp_path / "public/logo.svg").read_bytes() == svg
    assert (tmp_path / "public/texture.png").read_bytes() == png


async def test_substantial_astro_source_is_not_replaced_as_under_delivery(tmp_path):
    plan = {
        "stack": "astro",
        "summary": "A content-rich Astro application.",
        "files": [{"path": "src/pages/index.astro", "purpose": "complete homepage"}],
    }
    body = "---\nconst title = 'Complete';\n---\n" + "\n".join(
        f"<section id='s{i}'><h2>Section {i}</h2><p>Detailed content.</p></section>"
        for i in range(700)
    )

    async def write_astro(_attempt, root):
        target = root / "src/pages/index.astro"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    _agent, result = await _run(tmp_path, _ScriptedAgenticLLM(write_astro), plan)

    assert "degraded" not in result.output
    assert (tmp_path / "src/pages/index.astro").read_text(encoding="utf-8") == body


async def test_substantial_static_html_and_css_are_not_replaced_as_under_delivery(tmp_path):
    plan = {
        "stack": "static",
        "summary": "A complete static product site.",
        "files": [
            {"path": "index.html", "purpose": "complete page"},
            {"path": "styles.css", "purpose": "responsive visual system"},
        ],
    }
    html = "<!doctype html><main>" + "".join(
        f"<section class='section-{i}'><h2>Capability {i}</h2></section>"
        for i in range(700)
    ) + "</main>"
    css = "\n".join(
        f".section-{i} {{ display: grid; padding: {i % 20 + 1}px; }}"
        for i in range(700)
    )

    async def write_static(_attempt, root):
        (root / "index.html").write_text(html, encoding="utf-8")
        (root / "styles.css").write_text(css, encoding="utf-8")
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    _agent, result = await _run(tmp_path, _ScriptedAgenticLLM(write_static), plan)

    assert "degraded" not in result.output
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == html
    assert (tmp_path / "styles.css").read_text(encoding="utf-8") == css
