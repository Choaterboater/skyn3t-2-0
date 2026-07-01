"""Regression tests for the adversarial-review fixes on hermes-grade-builds.

Covers the findings whose ORIGINAL code passed CI by accident:
- #2 parallel slices must actually run concurrently through REAL orchestrator
  routing (the old test stubbed _submit_stage and never exercised the per-agent
  _run_lock that serialized everything).
- #1 the stylesheet stub must NOT write outside the project tree.
- #6/#7 the Python import gate must not false-positive on stdlib-submodule
  imports under a local name collision, nor on try/except-ImportError guards.
"""

from __future__ import annotations

import asyncio

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.planner import BuildPlan
from skyn3t.studio.proof_run import _unresolved_python_imports
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.stages import StageSpec
from skyn3t.worktree import create_worktree

_ARCH = [
    {"path": "src/App.jsx"}, {"path": "src/main.jsx"}, {"path": "src/components/Card.jsx"},
    {"path": "src/index.css"}, {"path": "index.html"}, {"path": "package.json"},
    {"path": "api/main.py"}, {"path": "api/routes/users.py"}, {"path": "tests/test_users.py"},
]


# --------------------------------------------------------------------------
# #2 — slices must actually run concurrently (not serialize on _run_lock)
# --------------------------------------------------------------------------

def test_slices_run_concurrently_through_real_routing(tmp_path, monkeypatch):
    bus = EventBus()
    settings = Settings(projects_dir=tmp_path / "P", data_dir=tmp_path / "d",
                        logs_dir=tmp_path / "l", critic_enabled=False,
                        parallel_code_slices=True)
    runner = StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)
    # Register a real codegen agent so _registered_codegen_agent finds one to clone.
    asyncio.run(runner.orchestrator.register(CodeAgent(event_bus=bus)))

    state = {"active": 0, "max": 0}

    class _SlowAgent:  # replaces the per-slice CodeAgent
        def __init__(self, *, event_bus=None, llm=None, config=None):
            pass

        async def initialize(self):
            pass

        async def execute(self, task):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.15)
            state["active"] -= 1
            sc = task.payload["slice_scope"]
            return TaskResult(task_id=task.task_id, success=True,
                              output={"files": [f"{sc['name']}.x"], "slice": sc["name"]})

    monkeypatch.setattr("skyn3t.agents.code_agent.CodeAgent", _SlowAgent)

    plan = BuildPlan(slug="demo", brief="x", stack="react", stages=[],
                     checklist=["package.json"], best_of_n=1)
    prior = {"architect": {"plan": {"files": _ARCH}}}
    slices = runner._maybe_slices(plan, prior)
    assert slices and len(slices) >= 2
    main_wt = create_worktree(str(settings.projects_dir), "demo")
    spec = StageSpec(name="code", agent_type="code", capability="codegen")

    asyncio.run(runner._run_code_parallel_slices(
        plan, spec, str(settings.projects_dir / "demo"), prior, [], {},
        "cid", main_wt, [main_wt], slices))

    assert state["max"] >= 2, f"slices serialized — max concurrent execute()={state['max']}"


# --------------------------------------------------------------------------
# #1 — stylesheet stub must stay inside the project tree
#
# The dedicated `_stub_dangling_stylesheets` this originally covered is retired
# (its job is fully subsumed by the general `scaffold_missing_imports`, which
# now handles bare/side-effect imports and stylesheet-aware content too — see
# tests/test_scaffold_missing_imports.py). Re-pinned here against the new
# mechanism so this file's historical "findings that passed CI by accident"
# record stays accurate and green.
# --------------------------------------------------------------------------

def test_escaping_stylesheet_not_stubbed_out_of_tree(tmp_path):
    from skyn3t.studio.proof_run import scaffold_missing_imports

    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (proj / "src" / "main.jsx").write_text(
        "import '../../sibling/theme.css';\nexport default function App(){return null}\n",
        encoding="utf-8")
    written = scaffold_missing_imports(proj)
    assert written == []  # escaping path is left flagged, not stubbed
    assert not (sibling / "theme.css").exists()  # nothing written out-of-tree
    # a normal in-tree dangling stylesheet is still stubbed
    (proj / "src" / "main.jsx").write_text(
        "import './styles/app.css';\nexport default function App(){return null}\n",
        encoding="utf-8")
    written2 = scaffold_missing_imports(proj)
    assert "src/styles/app.css" in written2
    assert (proj / "src" / "styles" / "app.css").exists()


# --------------------------------------------------------------------------
# #6 / #7 — Python import gate robustness
# --------------------------------------------------------------------------

def test_stdlib_submodule_not_flagged_under_local_name_collision(tmp_path):
    # A local 'email/' asset dir must NOT make the stdlib `email.mime.text` import
    # look unresolved.
    (tmp_path / "email").mkdir()
    (tmp_path / "main.py").write_text(
        "from email.mime.text import MIMEText\nprint(MIMEText)\n", encoding="utf-8")
    assert _unresolved_python_imports(tmp_path) == []


def test_try_except_importerror_guarded_import_not_flagged(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    # app/optional.py is intentionally absent, guarded by try/except ImportError.
    (tmp_path / "main.py").write_text(
        "try:\n    import app.optional\nexcept ImportError:\n    app_optional = None\n",
        encoding="utf-8")
    assert _unresolved_python_imports(tmp_path) == []
    # But an UNGUARDED missing local import is still flagged.
    (tmp_path / "boot.py").write_text("import app.missing\n", encoding="utf-8")
    assert any("app.missing" in g for g in _unresolved_python_imports(tmp_path))
