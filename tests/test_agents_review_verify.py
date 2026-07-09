"""Offline tests for the review + verification agents.

No network, no heavy deps. Exercises the heuristic/static paths and the
reward-hardening guard against a real temp project on disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from skyn3t.agents.boot_verifier import BootVerifierAgent
from skyn3t.agents.build_verifier import BuildVerifierAgent, detect_reward_hacking
from skyn3t.agents.consistency_reviewer import ConsistencyReviewerAgent
from skyn3t.agents.contract_verifier import ContractVerifierAgent, extract_planned_files
from skyn3t.agents.critic import CriticAgent, static_scan
from skyn3t.agents.integration_verifier import IntegrationVerifierAgent
from skyn3t.agents.reviewer import ReviewerAgent, heuristic_score
from skyn3t.agents.test_author import (
    TestAuthorAgent,
    derive_acceptance,
    derive_asset_paths,
    derive_planned_pages,
    render_test_file,
)
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


def test_detect_reward_hacking_flags_skips_even_with_assertions(tmp_path):
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "app.py").write_text("def f():\n    return 1\n")
    (root / "test_app.py").write_text(
        "import pytest\n\n"
        "def test_structure():\n"
        "    assert True\n\n"
        "@pytest.mark.skip(reason='not implemented')\n"
        "def test_acceptance_behavior():\n"
        "    assert f() == 2\n"
    )
    result = detect_reward_hacking(str(root), {"tests_passed": 1})
    assert result["suspicious"]
    assert any("skipped/xfailed" in flag for flag in result["flags"])


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


def test_build_verifier_reinstalls_docker_node_modules_before_host_build(tmp_path, monkeypatch):
    import skyn3t.agents.build_verifier as bv

    root = tmp_path / "web"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({
        "name": "web",
        "scripts": {"build": "vite build"},
        "dependencies": {"vite": "^4.4.9"},
    }))
    (root / "src").mkdir()
    (root / "src" / "main.jsx").write_text("export default null\n")
    nm = root / "node_modules"
    nm.mkdir()
    (nm / ".skyn3t-docker-install.json").write_text(
        json.dumps({"backend": "docker", "container_os": "linux", "fingerprint": "abc"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(bv.shutil, "which",
                        lambda x: "/usr/bin/npm" if x == "npm" else None)
    install_saw_foreign = []

    async def fake_run(self, cmd, cwd, timeout):
        if cmd[:2] == ["npm", "install"]:
            install_saw_foreign.append((root / "node_modules" / ".skyn3t-docker-install.json").exists())
            (root / "node_modules").mkdir(exist_ok=True)
            return True, "install ok"
        if cmd[:3] == ["npm", "run", "build"]:
            return True, "build ok"
        return False, "unexpected command"

    monkeypatch.setattr(bv.BuildVerifierAgent, "_run", fake_run)

    agent = bv.BuildVerifierAgent(event_bus=EventBus())
    res = _run(agent.run(TaskRequest(
        type="verify_build",
        payload={"worktree_dir": str(root), "stack": "react"})))

    assert res.success
    assert install_saw_foreign == [False]
    assert res.output["verdict"] == "pass"


def test_boot_verifier_routes_fastapi_to_python(tmp_path, monkeypatch):
    """fastapi (a Python web framework) must boot via the Python import-smoke,
    not fall through to the structural web check (finding #20)."""
    root = tmp_path / "api"
    root.mkdir()
    (root / "main.py").write_text("app = object()\n")
    (root / "requirements.txt").write_text("fastapi\n")
    bus = EventBus()
    agent = BootVerifierAgent(event_bus=bus)
    called = {}

    async def fake_py(r, e):
        called["py"] = True
        return True, "python", "ok"

    def fake_node(r, e):
        called["node"] = True
        return True, "node", "ok"

    def fake_web(r, e):
        called["web"] = True
        return True, "web", "ok"

    monkeypatch.setattr(agent, "_boot_python", fake_py)
    monkeypatch.setattr(agent, "_boot_node", fake_node)
    monkeypatch.setattr(agent, "_boot_web", fake_web)

    res = _run(agent.run(TaskRequest(type="verify_boot",
                                     payload={"project_dir": str(root), "stack": "fastapi"})))
    assert res.success
    assert called.get("py") and not called.get("web"), called


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


def test_test_author_accepts_an_astro_page_as_source_and_entrypoint(tmp_path):
    root = tmp_path / "astro-app"
    page = root / "src" / "pages" / "index.astro"
    page.parent.mkdir(parents=True)
    page.write_text("---\nconst title = 'Golf lessons';\n---\n<h1>{title}</h1>\n")
    generated_metadata = root / ".astro" / "content.d.ts"
    generated_metadata.parent.mkdir()
    generated_metadata.write_text(
        'export type Config = typeof import("../src/content.config.mjs");\n'
    )

    generated = render_test_file(
        ["project produces at least one runnable entrypoint"],
        "An Astro golf lesson site",
        "astro-app",
    )
    namespace = {"__file__": str(root / "tests" / "test_acceptance_astro.py")}
    exec(compile(generated, namespace["__file__"], "exec"), namespace)

    namespace["test_project_has_source_content"]()
    namespace["test_project_has_entrypoint"]()
    assert generated_metadata not in namespace["_sources"]()


def test_test_author_promotes_explicit_game_counts_to_real_checks():
    brief = "Code Islands is a 120-level journey with 12 islands and 4 phases."
    crit = derive_acceptance(brief, {})
    assert "include exact brief count: 120 levels" in crit
    assert "include exact brief count: 12 islands" in crit
    assert "include exact brief count: 4 phases" in crit
    src = render_test_file(crit, brief, "code-islands")
    assert "EXACT_COUNT_PHRASES" in src
    assert '"120 levels"' in src


def test_test_author_enforces_exact_hvac_pages_and_generated_assets(tmp_path):
    root = tmp_path / "hvac"
    root.mkdir()
    paid_asset = "assets/air-conditioning-condenser-unit-beside-a-house.webp"
    plan_files = [
        "index.html",
        "about.html",
        "contact.html",
        "financing.html",
        "reviews.html",
        "services/ac-repair.html",
        "services/heating.html",
        "services/installation.html",
        "css/custom.css",
        "js/contact-form.js",
        "js/main.js",
        "js/reviews-carousel.js",
        "package.json",
    ]
    payload = {
        "worktree_dir": str(root),
        "brief": "HVAC service pages, financing, reviews, emergency contact, and photos",
        "slug": "hvac",
        "stack": "static",
        "plan": {"stack": "static", "files": [{"path": path} for path in plan_files]},
        "extra": {
            "assets": [{"subject": "condenser", "file": paid_asset}],
            "asset_foundry": {"selected": {
                "web/hero": {"path": "/assets/hero.png"},
                "web/og": {"path": "/assets/og.png"},
                "web/favicon": {"path": "/assets/favicon.png"},
            }},
        },
    }

    agent = TestAuthorAgent(event_bus=EventBus())
    result = _run(agent.run(TaskRequest(type="test_author", payload=payload)))

    expected_pages = [path for path in plan_files if path.endswith(".html")]
    assert result.output["planned_pages"] == expected_pages
    assert result.output["asset_paths"] == [
        paid_asset,
        "assets/hero.png",
        "assets/og.png",
        "assets/favicon.png",
    ]

    for relative in expected_pages:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<main>HVAC service</main>", encoding="utf-8")
    for relative in result.output["asset_paths"]:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"image")
    (root / "index.html").write_text(
        '<img src="/assets/air-conditioning-condenser-unit-beside-a-house.webp">'
        '<img src="/assets/hero.png"><meta content="/assets/og.png">'
        '<link rel="icon" href="/assets/favicon.png">',
        encoding="utf-8",
    )

    written = root / result.output["test_files"][0]
    namespace = {"__file__": str(written)}
    exec(compile(written.read_text(encoding="utf-8"), str(written), "exec"), namespace)
    for relative in expected_pages:
        namespace["test_planned_feature_page_exists"](relative)
    for relative in result.output["asset_paths"]:
        namespace["test_generated_asset_exists"](relative)
        namespace["test_generated_asset_is_referenced"](relative)

    (root / "financing.html").unlink()
    with pytest.raises(AssertionError, match="planned feature page is missing"):
        namespace["test_planned_feature_page_exists"]("financing.html")


def test_test_author_uses_astro_routes_not_components_for_golf_contract(tmp_path):
    plan = {"stack": "astro", "files": [
        {"path": "src/pages/index.astro"},
        {"path": "src/pages/lessons/index.astro"},
        {"path": "src/pages/lessons/[slug].astro"},
        {"path": "src/pages/drills.astro"},
        {"path": "src/pages/equipment.astro"},
        {"path": "src/pages/resources.astro"},
        {"path": "src/pages/book.astro"},
        {"path": "src/pages/api/bookings.ts"},
        {"path": "src/components/Header.astro"},
        {"path": "src/layouts/Layout.astro"},
        {"path": "package.json"},
        {"path": "../escape.astro"},
    ]}
    payload = {
        "plan": plan,
        "stack": "astro",
        "extra": {"asset_foundry": {"selected": {
            "web/hero": {"path": "/assets/hero.png"},
        }}},
    }

    assert derive_planned_pages(plan, "astro") == [
        "src/pages/index.astro",
        "src/pages/lessons/index.astro",
        "src/pages/lessons/[slug].astro",
        "src/pages/drills.astro",
        "src/pages/equipment.astro",
        "src/pages/resources.astro",
        "src/pages/book.astro",
    ]
    assert derive_asset_paths(payload) == ["assets/hero.png"]


def test_test_author_discards_unsafe_planned_and_asset_paths():
    plan = {"stack": "static", "files": [
        {"path": "index.html"},
        {"path": "partials/header.html"},
        {"path": "../outside.html"},
        {"path": "C:\\outside.html"},
        {"path": "dist/copied.html"},
    ]}
    payload = {"extra": {"assets": [
        {"file": "assets/safe.webp"},
        {"file": "../../secret.png"},
        {"file": "C:\\secret.png"},
    ]}}

    assert derive_planned_pages(plan, "static") == ["index.html"]
    assert derive_asset_paths(payload) == ["assets/safe.webp"]


def test_modules_import_without_side_effects():
    # importing the package modules must not require any heavy dep or network
    import skyn3t.agents.build_verifier  # noqa: F401
    import skyn3t.agents.critic  # noqa: F401
    import skyn3t.agents.reviewer  # noqa: F401
    assert True
