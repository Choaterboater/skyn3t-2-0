"""Exercise the real API, orchestrator, improver and Node proof with a scripted model."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.web import routes
from skyn3t.web.app import create_app
from skyn3t.web.deps import AppState


class ScriptedModel:
    """Only the model response is scripted; project execution is real and offline."""

    backend = "openrouter"

    def __init__(self, *, correct: bool):
        self.correct = correct
        self.prompts: list[str] = []

    async def agentic_build(self, prompt, workdir, **kwargs):
        self.prompts.append(prompt)
        root = Path(workdir)
        (root / "main.js").write_text("export { total } from './helpers.js';\n")
        operator = "+" if self.correct else "*"
        (root / "helpers.js").write_text(
            f"export function total(a, b) {{ return a {operator} b; }}\n",
        )
        return {"ok": True, "backend": "openrouter"}

    async def complete(self, *args, **kwargs):
        raise AssertionError("Agentic edit should not fall back to per-file generation")


@pytest.mark.parametrize("correct", [True, False])
async def test_import_api_to_real_improve_and_project_tests(tmp_path, monkeypatch, correct):
    node = shutil.which("node")
    if not node or not shutil.which("npm"):
        pytest.skip("requires the existing Node/npm toolchain")
    monkeypatch.setenv("npm_config_offline", "true")
    monkeypatch.setenv("npm_config_cache", str(tmp_path / "npm-cache"))
    source = tmp_path / "external-calculator"
    source.mkdir()
    (source / "package.json").write_text(json.dumps({
        "name": "external-calculator",
        "version": "1.0.0",
        "type": "module",
        "scripts": {"build": "node --check main.js", "test": "node --test"},
    }))
    (source / "main.js").write_text("export function total(a, b) { return a - b; }\n")
    (source / "main.test.js").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import { total } from './main.js';\n"
        "test('adds both operands', () => assert.equal(total(3, 2), 5));\n",
    )
    (source / "model.bin").write_bytes(b"\x00\xff\x80original-binary")
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    baseline = subprocess.run(
        [node, "--test"], cwd=source, capture_output=True, text=True, timeout=30,
    )
    assert baseline.returncode != 0
    assert "adds both operands" in baseline.stdout

    bus = EventBus()
    orchestrator = Orchestrator(bus)
    model = ScriptedModel(correct=correct)
    agent = CodeImproverAgent(event_bus=bus, llm=model)
    await orchestrator.register(agent)
    settings = Settings(
        projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
        llm_backend="stub", execution_backend="inline",
        run_generated_build=True, run_generated_tests=True,
        auth_token="", moa_enabled=False,
    )
    state = AppState(settings=settings, event_bus=bus, orchestrator=orchestrator)
    app = create_app(state=state)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        ) as client:
            imported = await client.post("/api/projects/import", json={"path": str(source)})
            assert imported.status_code == 200, imported.text
            slug = imported.json()["slug"]
            managed = Path(imported.json()["project_dir"])
            rows = (await client.get("/api/projects")).json()["projects"]
            assert rows[0]["status"] == "imported" and rows[0]["can_improve"]
            assert not rows[0]["is_complete"]

            response = await client.post("/api/studio/improve", json={
                "slug": slug, "goal": "Fix total to add both operands; extract a reusable helper.",
            })
            assert response.status_code == 200, response.text
            assert response.json()["accepted"]
            cid = response.json()["correlation_id"]
            await asyncio.gather(*list(routes._IMPROVE_TASKS))

            events = [event for event in bus.history() if event.correlation_id == cid]
            terminal_type = EventType.IMPROVE_COMPLETED if correct else EventType.IMPROVE_FAILED
            result = next(event.payload for event in events if event.type == terminal_type)
            assert result["proof_passed"] is correct
            assert model.prompts and "Fix total" in model.prompts[0]
            manifest = BuildManifest.load(managed)
            proof = (
                manifest.extra["proof"] if correct
                else result["detail"]["proof"]
            )
            assert proof["detail"]["build"] == "passed"
            assert proof["detail"]["node_tests"] == ("passed" if correct else "failed")
            assert manifest.status == ("completed" if correct else "imported")
            assert (managed / "model.bin").read_bytes() == original["model.bin"]
            assert not (managed / "package-lock.json").exists()
            assert (managed / "package.json").read_bytes() == original["package.json"]
            assert (managed / "helpers.js").exists() is correct
            if not correct:
                assert (managed / "main.js").read_bytes() == original["main.js"]
            rows = (await client.get("/api/projects")).json()["projects"]
            assert rows[0]["is_complete"] is correct
            assert {path.name: path.read_bytes() for path in source.iterdir()} == original
    finally:
        await orchestrator.unregister(agent.name)
