"""Browser evidence must describe a visible, exercised outcome."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import test_improve_engine as improve_fixtures
import test_web_interact_check as browser_fixtures

from skyn3t.core.events import EventBus
from skyn3t.studio import improve as improve_module
from skyn3t.studio import web_interact_check as wic
from skyn3t.studio.proof_run import ProofResult


def test_url_only_assertion_is_not_a_ui_outcome():
    plan = {
        "actions": [
            browser_fixtures._SPA_PLAN["actions"][1],
            {"op": "expect_url_contains", "label": "same URL", "contains": "/"},
        ]
    }

    actions, error = wic._validated_actions(plan)

    assert not actions
    assert "UI" in error


def test_backend_state_assertion_cannot_be_only_whitespace():
    plan = json.loads(browser_fixtures._PASS_SCRIPT)
    plan["actions"][-1]["contains"] = " "

    actions, error = wic._validated_actions(plan, require_backend=True)

    assert not actions
    assert "backend state assertion" in error


@pytest.mark.parametrize("field,value", [("op", []), ("by", {})])
def test_unhashable_action_fields_are_reported_before_browser_launch(field, value):
    action = dict(browser_fixtures._SPA_PLAN["actions"][1])
    action[field] = value
    result = wic._drive_interaction("http://127.0.0.1:1", {"actions": [action]})

    assert result["script_error"] is True
    assert result["error"]
    assert result["steps"] == []


def test_prompt_includes_grounded_outcome_ids(tmp_path):
    surface = wic.harvest_action_surface(tmp_path, "static")
    surface["ids"] = ["result-panel"]

    prompt = wic._build_script_prompt("http://127.0.0.1:1", surface)

    assert "#result-panel" in prompt


@pytest.mark.requires_loopback
@pytest.mark.skipif(not wic.playwright_available(), reason="playwright not installed")
def test_hidden_success_text_cannot_verify_a_dead_button(tmp_path, monkeypatch):
    monkeypatch.setattr(
        browser_fixtures,
        "_GUESTBOOK_HTML",
        browser_fixtures._GUESTBOOK_HTML.replace('type="submit"', 'type="button"'),
    )
    original = json.loads(browser_fixtures._PASS_SCRIPT)["actions"]
    plan = {
        "actions": [
            original[0],
            original[3],
            {
                "op": "expect_text", "label": "verify visible success", "by": "selector",
                "selector": "#success", "contains": "Thanks for signing!",
                "timeout_ms": 500,
            },
        ]
    }
    runner = browser_fixtures._FixtureRunner()
    app = asyncio.run(runner.start(tmp_path, "static"))
    try:
        result = wic._drive_interaction(runner.url, plan)
    finally:
        runner.stop(app)

    assert result["passed"] is False, result
    assert result["coverage"]["ui_assertions"] == 0


@pytest.mark.requires_loopback
@pytest.mark.skipif(not wic.playwright_available(), reason="playwright not installed")
def test_static_navigation_does_not_hide_a_delayed_main_workflow(tmp_path, monkeypatch):
    loading_controls = "".join(f"<button>Loading {index}</button>" for index in range(12))
    html = browser_fixtures._SPA_HTML.replace(
        '<div id="root"></div>',
        f'<nav><a href="/">Home</a></nav><div id="root">{loading_controls}</div>',
    ).replace("}, 150);", "}, 600);")
    monkeypatch.setattr(browser_fixtures, "_SPA_HTML", html)
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    prompts = []

    def author(prompt):
        prompts.append(prompt)
        return json.dumps(browser_fixtures._SPA_PLAN)

    result = asyncio.run(wic.check_web_interact(
        tmp_path, "react", settings=SimpleNamespace(), llm=author,
        app_runner=browser_fixtures._FixtureRunner(spa=True),
        brief="Save tasks and retain them after reload.",
    ))

    assert result["ok"] is True and result["skipped"] is False, result
    assert len(prompts) == 1
    assert "New task" in prompts[0]
    assert "Add task" in prompts[0]
    assert "#tasks" in prompts[0]


@pytest.mark.parametrize("has_backend", [False, True])
def test_success_without_completed_assertion_evidence_is_not_verified(
    tmp_path, monkeypatch, has_backend,
):
    if has_backend:
        browser_fixtures._write_fixture_app(tmp_path)
    monkeypatch.setattr(wic, "playwright_available", lambda: True)
    monkeypatch.setattr(
        wic, "_harvest_rendered_surface",
        lambda url: {"buttons": [{"text": "Add task"}], "ids": ["tasks"]},
    )

    async def start(project_dir, stack):
        return SimpleNamespace(
            status="running", url="http://127.0.0.1:1", pid=None, log_path=None,
            project_dir=str(project_dir), kind="static",
        )

    result = asyncio.run(wic.check_web_interact(
        tmp_path, "static", settings=SimpleNamespace(),
        llm=lambda prompt: (
            browser_fixtures._PASS_SCRIPT if has_backend
            else json.dumps(browser_fixtures._SPA_PLAN)
        ),
        app_runner=SimpleNamespace(start=start, stop=lambda app: None),
        drive_fn=lambda url, plan: {
            "passed": True, "steps": ["clicked a control"], "console_errors": [],
            "coverage": {
                "ui_assertions": 1 if has_backend else 0,
                "backend_assertions": 0, "reloads": 0,
            },
        },
    ))

    assert result["skipped"] is True, result
    assert result["reason"]


@pytest.mark.parametrize("has_manifest", [True, False])
def test_improve_persists_fresh_browser_evidence_for_both_project_kinds(
    tmp_path, monkeypatch, has_manifest,
):
    settings = improve_fixtures._settings(tmp_path)
    project = improve_fixtures._seed_project(settings.projects_dir, "demo")
    if not has_manifest:
        (project / "skyn3t_manifest.json").unlink()
    observed = {
        "ok": False, "skipped": False, "issues": ["dead button"],
        "coverage": {"ui_assertions": 0, "backend_assertions": 0, "reloads": 0},
    }
    calls = []

    async def check(project_dir, stack, *, settings, brief):
        calls.append((project_dir, brief))
        assert str(project) != str(project_dir)
        assert (project / "main.py").read_text() == "print('original')\n"
        return observed

    monkeypatch.setattr(improve_module, "check_web_interact", check)
    engine = improve_module.ImproveEngine(
        EventBus(), improve_fixtures._FakeOrchestrator(), settings=settings,
    )
    result = asyncio.run(engine.improve("demo", "retain saved tasks"))

    assert result.status == "completed", result.detail
    assert len(calls) == 1
    assert "retain saved tasks" in calls[0][1]
    if has_manifest:
        assert "demo" in calls[0][1]
    assert result.detail["web_interact"] == observed
    manifest = json.loads((project / "skyn3t_manifest.json").read_text())
    assert manifest["extra"]["web_interact"] == observed
    assert manifest["extra"]["improve_history"][-1]["web_interact"] == observed


@pytest.mark.parametrize("enabled,proof_passes", [(False, True), (True, False)])
def test_improve_invalidates_disabled_evidence_but_preserves_rejected_delivery(
    tmp_path, monkeypatch, enabled, proof_passes,
):
    settings = improve_fixtures._settings(tmp_path)
    settings.web_interact_check_enabled = enabled
    project = improve_fixtures._seed_project(settings.projects_dir, "demo")
    path = project / "skyn3t_manifest.json"
    manifest = json.loads(path.read_text())
    old_evidence = {"ok": True, "skipped": False, "interactions": ["old flow"]}
    manifest["extra"] = {"web_interact": old_evidence}
    path.write_text(json.dumps(manifest))

    async def unexpected_check(*args, **kwargs):
        pytest.fail("disabled or proof-rejected runs must not run browser QA")

    monkeypatch.setattr(improve_module, "check_web_interact", unexpected_check)
    if not proof_passes:
        monkeypatch.setattr(
            improve_module, "proof_run",
            lambda *args, **kwargs: ProofResult(passed=False, score=0.0),
        )
    engine = improve_module.ImproveEngine(
        EventBus(), improve_fixtures._FakeOrchestrator(), settings=settings,
    )
    outcome = asyncio.run(engine.improve("demo", "retain saved tasks"))
    saved = json.loads(path.read_text())

    if proof_passes:
        assert outcome.status == "completed", outcome.detail
        assert "web_interact" not in saved["extra"]
        assert "web_interact" not in outcome.detail
    else:
        assert outcome.status == "failed"
        assert saved["extra"]["web_interact"] == old_evidence
        assert (project / "main.py").read_text() == "print('original')\n"


@pytest.mark.parametrize(
    "result_maker,expected",
    [
        (lambda: wic._skip("playwright not installed"), "not_checked"),
        (
            lambda: wic._score(
                {"apis": [], "checked": []},
                {"passed": False, "error": "click failed", "steps": ["click"],
                 "console_errors": [], "coverage": {}},
                [], {},
            ),
            "failed",
        ),
        (
            lambda: wic._score(
                {"apis": [], "checked": []},
                {"passed": True, "error": "", "steps": ["act"],
                 "console_errors": [],
                 "coverage": {"ui_assertions": 1, "backend_assertions": 0,
                              "reloads": 0}},
                [], {},
            ),
            "passed",
        ),
    ],
)
def test_every_interaction_result_carries_explicit_status(result_maker, expected):
    result = result_maker()
    assert result["status"] == expected
    assert result["ok"] is (expected != "failed")
    assert result["skipped"] is (expected == "not_checked")


def _required_product(*texts):
    from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord
    return ProductSpecV1(project_id="tasks", requirements=[
        RequirementRecord(id=f"req-{index}", text=text) for index, text in enumerate(texts)
    ])


def test_unchecked_contract_requirements_remain_in_denominator(tmp_path, monkeypatch):
    monkeypatch.setattr(wic, "playwright_available", lambda: False)
    result = asyncio.run(wic.check_web_interact(
        tmp_path, "static", settings=SimpleNamespace(),
        product_spec=_required_product(*[f"Save item {index}" for index in range(10)]),
    ))
    assert result["summary"] == {"required": 10, "passed": 0, "failed": 0, "not_checked": 10}
    assert result["status"] == "not_checked"
    assert result["blocks_delivery"] is False
    assert len({o["outcome_id"] for o in result["outcomes"]}) == 10


@pytest.mark.requires_loopback
@pytest.mark.skipif(not wic.playwright_available(), reason="playwright not installed")
@pytest.mark.parametrize("variant", ["working", "noop", "volatile"])
def test_required_save_and_persistence_use_real_browser_evidence(tmp_path, monkeypatch, variant):
    html = browser_fixtures._SPA_HTML
    if variant == "noop":
        html = html.replace("tasks.push(document.getElementById('entry').value);", "")
    monkeypatch.setattr(browser_fixtures, "_SPA_HTML", html)
    (tmp_path / "index.html").write_text(html)

    class FreshRunner:
        async def start(self, project_dir, stack):
            self.runner = browser_fixtures._FixtureRunner(spa=True, persistent=variant != "volatile")
            return await self.runner.start(project_dir, stack)

        def stop(self, app):
            self.runner.stop(app)

    def author(prompt):
        actions = browser_fixtures._SPA_PLAN["actions"]
        return json.dumps({"actions": actions if '"kind": "persistence"' in prompt else actions[:3]})

    result = asyncio.run(wic.check_web_interact(
        tmp_path, "static", settings=SimpleNamespace(), llm=author,
        app_runner=FreshRunner(), product_spec=_required_product("Save tasks and persist after reload"),
    ))
    expected = {"working": (2, 0), "noop": (0, 2), "volatile": (1, 1)}[variant]
    assert result["summary"] == {"required": 2, "passed": expected[0], "failed": expected[1], "not_checked": 0}, result
    assert result["fresh"] is True
    assert result["blocks_delivery"] is (variant != "working")
    (tmp_path / "index.html").write_text(html + "<!-- edited -->")
    stale = wic.refresh_web_interact(result, tmp_path)
    assert stale["status"] == "not_checked"
    assert stale["summary"]["not_checked"] == 2
    assert stale["blocks_delivery"] is False


def test_required_persistence_cannot_be_replaced_by_generic_success():
    plan = {"actions": browser_fixtures._SPA_PLAN["actions"][:3]}
    assert wic._validate_required_flow(plan, "persistence")
    assert not wic._validate_required_flow(browser_fixtures._SPA_PLAN, "persistence")
    assert {o["kind"] for o in wic.required_outcomes(_required_product("Save tasks; validate invalid input"))} == {"primary", "invalid_input"}


@pytest.mark.requires_loopback
@pytest.mark.skipif(not wic.playwright_available(), reason="playwright not installed")
@pytest.mark.parametrize("rejects", [True, False])
def test_required_invalid_input_exercises_rejection(tmp_path, monkeypatch, rejects):
    html = browser_fixtures._SPA_HTML.replace('<ul id="tasks"></ul>', '<ul id="tasks"></ul><p id="error"></p>')
    if rejects:
        html = html.replace("tasks.push(document.getElementById('entry').value);", """
        if (!document.getElementById('entry').value.trim()) {
          document.getElementById('error').textContent = 'Task is required'; return;
        }
        tasks.push(document.getElementById('entry').value);""")
    monkeypatch.setattr(browser_fixtures, "_SPA_HTML", html)
    (tmp_path / "index.html").write_text(html)

    class FreshRunner:
        async def start(self, project_dir, stack):
            self.runner = browser_fixtures._FixtureRunner(spa=True)
            return await self.runner.start(project_dir, stack)

        def stop(self, app):
            self.runner.stop(app)

    def author(prompt):
        actions = browser_fixtures._SPA_PLAN["actions"][:3]
        if '"kind": "invalid_input"' in prompt:
            actions = [dict(actions[0], value=""), actions[1],
                       dict(actions[2], selector="#error", contains="Task is required", timeout_ms=500)]
        return json.dumps({"actions": actions})

    result = asyncio.run(wic.check_web_interact(
        tmp_path, "static", settings=SimpleNamespace(), llm=author, app_runner=FreshRunner(),
        product_spec=_required_product("Save tasks and reject invalid input"),
    ))
    assert result["summary"] == {"required": 2, "passed": 2 if rejects else 1, "failed": 0 if rejects else 1, "not_checked": 0}, result
    assert result["blocks_delivery"] is (not rejects)


@pytest.mark.parametrize("posture", ["lab", "release"])
def test_improve_required_outcome_failure_obeys_release_posture(tmp_path, monkeypatch, posture):
    settings = improve_fixtures._settings(tmp_path)
    settings.build_posture = posture
    project = improve_fixtures._seed_project(settings.projects_dir, "demo")

    async def check(*args, **kwargs):
        return {"status": "failed", "ok": False, "skipped": False, "fresh": True,
                "blocks_delivery": True, "issues": ["required save produced no result"]}

    monkeypatch.setattr(improve_module, "check_web_interact", check)
    engine = improve_module.ImproveEngine(EventBus(), improve_fixtures._FakeOrchestrator(), settings=settings)
    result = asyncio.run(engine.improve("demo", "save tasks"))
    assert result.status == ("failed" if posture == "release" else "completed"), result.detail
    if posture == "release":
        assert result.detail["delivery_blocked"] == "required_browser_outcome_failed"
        assert (project / "main.py").read_text() == "print('original')\n"


@pytest.mark.parametrize("posture,changed", [("lab", False), ("release", False), ("release", True)])
def test_build_settles_only_fresh_required_failures(tmp_path, posture, changed):
    from skyn3t.studio.gate_posture import GatePosture
    from skyn3t.studio.manifest import BuildManifest
    from skyn3t.worktree import source_tree_snapshot
    (tmp_path / "index.html").write_text("<button>Save</button>")
    manifest = BuildManifest(slug="tasks", brief="Save tasks", stack="static")
    manifest.extra["web_interact"] = {
        "source_identity": source_tree_snapshot(tmp_path), "issues": ["saved item missing"],
        "outcomes": [{"outcome_id": "save:primary", "required": True, "status": "failed", "reliable": True}],
    }
    if changed:
        (tmp_path / "index.html").write_text("<button>Save fixed</button>")
    runner = browser_fixtures._studio_runner(tmp_path)
    verdict = runner._settle_web_interact(manifest, tmp_path, "go", posture=GatePosture(posture=posture))
    assert verdict == ("no_go" if posture == "release" and not changed else "go")
    assert manifest.extra["web_interact"]["status"] == ("not_checked" if changed else "failed")
