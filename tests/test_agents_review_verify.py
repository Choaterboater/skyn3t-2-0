"""Offline tests for the review + verification agents.

No network, no heavy deps. Exercises the heuristic/static paths and the
reward-hardening guard against a real temp project on disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from skyn3t.agents.boot_verifier import BootVerifierAgent
from skyn3t.agents.build_verifier import BuildVerifierAgent, detect_reward_hacking
from skyn3t.agents.consistency_reviewer import ConsistencyReviewerAgent
from skyn3t.agents.contract_verifier import ContractVerifierAgent, extract_planned_files
from skyn3t.agents.critic import CriticAgent, static_scan
from skyn3t.agents.integration_verifier import IntegrationVerifierAgent
from skyn3t.agents.reviewer import ReviewerAgent, heuristic_score
from skyn3t.agents.test_author import TestAuthorAgent, derive_acceptance, render_test_file
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


def _good_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"name": "x", "scripts": {}}))
    (root / "index.js").write_text("function main() { return 42; }\nmmain();\n")
    (root / "util.js").write_text("export function add(a, b) { return a + b; }\n")
    (root / "lib.js").write_text("export const K = 1;\nexport const J = 2;\n")
    (root / "more.js").write_text("export const A = 3;\nexport const B = 4;\n")
    (root / "extra.js").write_text("export const C = 5;\nexport const D = 6;\n")
    return root


def _run(coro):
    return asyncio.run(coro)


def test_heuristic_score_good_project(tmp_path):
    root = _good_project(tmp_path)
    score, gaps = heuristic_score(root, {})
    assert score >= 60.0
    assert isinstance(gaps, list)


def test_heuristic_score_empty_project(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    score, gaps = heuristic_score(root, {})
    assert score < 60.0
    assert gaps


def test_reviewer_agent_go(tmp_path):
    root = _good_project(tmp_path)
    bus = EventBus()
    agent = ReviewerAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="review", payload={"worktree_dir": str(root)})))
    assert res.success
    assert res.output["verdict"] == "go"
    assert res.output["score"] >= 60.0


def test_critic_blocks_on_eval(tmp_path):
    root = tmp_path / "danger"
    root.mkdir()
    (root / "bad.py").write_text("def run(x):\n    return eval(x)\n")
    issues = static_scan(root)
    assert any(i["severity"] == "block" for i in issues)


def test_critic_agent_pass_clean(tmp_path):
    root = _good_project(tmp_path)
    bus = EventBus()
    agent = CriticAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="critique", payload={"worktree_dir": str(root)})))
    assert res.success
    assert res.output["verdict"] == "pass"


def test_critic_agent_blocks_secret(tmp_path):
    root = tmp_path / "secret"
    root.mkdir()
    (root / "cfg.py").write_text('API_KEY = "sk-abcdef1234567890"\n')
    bus = EventBus()
    agent = CriticAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="critique", payload={"project_dir": str(root)})))
    assert res.output["verdict"] == "block"


def test_contract_verifier_file_plan(tmp_path):
    root = _good_project(tmp_path)
    payload = {"worktree_dir": str(root), "plan": {"files": ["index.js", "util.js", "missing.js"]}}
    assert extract_planned_files(payload) == ["index.js", "util.js", "missing.js"]
    bus = EventBus()
    agent = ContractVerifierAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="verify_contract", payload=payload)))
    assert res.output["verdict"] == "fail"
    assert "missing.js" in res.output["missing"]
    assert "index.js" in res.output["satisfied"]


def test_detect_reward_hacking_empty_claiming_success(tmp_path):
    root = tmp_path / "fake"
    root.mkdir()
    (root / "README.md").write_text("All tests passed! Build succeeded.\n")
    result = detect_reward_hacking(str(root), {"success": True, "score": 99})
    assert result["suspicious"]
    assert result["flags"]


def test_detect_reward_hacking_trivial_tests(tmp_path):
    root = tmp_path / "trivial"
    root.mkdir()
    (root / "app.py").write_text("def f():\n    return 1\n")
    (root / "test_app.py").write_text("def test_f():\n    pass\n")
    result = detect_reward_hacking(str(root), {"tests_passed": 1})
    assert result["suspicious"]


def test_detect_reward_hacking_clean(tmp_path):
    root = _good_project(tmp_path)
    (root / "test_main.js").write_text("test('add', () => { expect(add(1,2)).toBe(3); });\n")
    result = detect_reward_hacking(str(root), {"success": True})
    assert not result["suspicious"]


def test_build_verifier_degraded(tmp_path):
    root = _good_project(tmp_path)
    bus = EventBus()
    agent = BuildVerifierAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="verify_build", payload={"worktree_dir": str(root)})))
    assert res.success
    assert res.output["verdict"] in ("pass", "fail")
    assert "reward_hacking" in res.output


def test_build_verifier_runs_real_build_for_nextjs(tmp_path, monkeypatch):
    """A nextjs/react project must trigger the REAL npm install/build, not the
    degraded dry check. Otherwise install-breaking codegen (hallucinated
    versions, route conflicts, malformed deps) sails through as a false 'pass'
    and the fix-loop never gets a failure to repair."""
    import skyn3t.agents.build_verifier as bv

    root = tmp_path / "web"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({
        "name": "web", "scripts": {"build": "next build"},
        "dependencies": {"next": "14.2.3"},
    }))
    (root / "index.jsx").write_text("export default function H(){return null}\n")

    # npm is "available" but every command fails (simulate a real install error).
    monkeypatch.setattr(bv.shutil, "which",
                        lambda x: "/usr/bin/npm" if x == "npm" else None)
    calls = []

    async def fake_run(self, cmd, cwd, timeout):
        calls.append(list(cmd))
        return False, "npm error code EINVALIDPACKAGENAME"

    monkeypatch.setattr(bv.BuildVerifierAgent, "_run", fake_run)

    agent = bv.BuildVerifierAgent(event_bus=EventBus())
    res = _run(agent.run(TaskRequest(
        type="verify_build",
        payload={"worktree_dir": str(root), "stack": "nextjs"})))

    assert res.success
    assert any(c[:2] == ["npm", "install"] for c in calls), f"never ran npm install: {calls}"
    assert res.output["ran_real_build"] is True
    assert res.output["verdict"] == "fail"  # install failed -> honest fail, feeds fix-loop


def test_boot_verifier_python(tmp_path):
    root = tmp_path / "py"
    root.mkdir()
    (root / "main.py").write_text("VALUE = 1\n\ndef go():\n    return VALUE\n")
    bus = EventBus()
    agent = BootVerifierAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="verify_boot", payload={"project_dir": str(root)})))
    assert res.success
    assert res.output["verdict"] == "pass"


def test_consistency_reviewer_broken_js_import(tmp_path):
    root = tmp_path / "js"
    root.mkdir()
    (root / "index.js").write_text("import { x } from './nope';\nconsole.log(x);\n")
    bus = EventBus()
    agent = ConsistencyReviewerAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="verify_consistency",
                                     payload={"worktree_dir": str(root), "stack": "node"})))
    assert res.output["verdict"] == "fail"
    assert res.output["broken_imports"]


def test_consistency_reviewer_ok(tmp_path):
    root = tmp_path / "js2"
    root.mkdir()
    (root / "util.js").write_text("export const x = 1;\n")
    (root / "index.js").write_text("import { x } from './util';\nconsole.log(x);\n")
    bus = EventBus()
    agent = ConsistencyReviewerAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="verify_consistency",
                                     payload={"worktree_dir": str(root), "stack": "node"})))
    assert res.output["verdict"] == "pass"


def test_integration_verifier_single_tier(tmp_path):
    root = tmp_path / "py3"
    root.mkdir()
    (root / "main.py").write_text("print('hello')\n")
    bus = EventBus()
    agent = IntegrationVerifierAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="verify_integration", payload={"project_dir": str(root)})))
    assert res.output["verdict"] == "pass"


def test_test_author_derives_and_writes(tmp_path):
    root = tmp_path / "ta"
    root.mkdir()
    (root / "main.py").write_text("print('x')\n")
    brief = "The app should let users add tasks and must display the task list."
    crit = derive_acceptance(brief, {})
    assert crit
    src = render_test_file(crit, brief, "app")
    assert "def test_project_has_source_content" in src
    bus = EventBus()
    agent = TestAuthorAgent(event_bus=bus)
    res = _run(agent.run(TaskRequest(type="test_author",
                                     payload={"worktree_dir": str(root), "brief": brief, "slug": "app"})))
    assert res.output["tests_written"] == 1
    written = root / res.output["test_files"][0]
    assert written.exists()
    assert written.read_text().strip()


def test_modules_import_without_side_effects():
    # importing the package modules must not require any heavy dep or network
    import skyn3t.agents.build_verifier  # noqa: F401
    import skyn3t.agents.critic  # noqa: F401
    import skyn3t.agents.reviewer  # noqa: F401
    assert True
