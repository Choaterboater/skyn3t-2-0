"""Runner orchestration for parallel code slices (Hermes orchestrator-worker).

Gating (`_maybe_slices`) plus the fan-out/merge in `_run_code_parallel_slices`:
each slice agent writes into its own worktree and ALL slices are merged into the
main worktree (config last). `_submit_stage` is stubbed to simulate the scoped
slice agents without launching real codegen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.planner import BuildPlan
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.stages import StageSpec
from skyn3t.worktree import create_worktree, list_files

_ARCH_FILES = [
    {"path": "src/App.jsx"}, {"path": "src/main.jsx"}, {"path": "src/components/Card.jsx"},
    {"path": "src/index.css"}, {"path": "index.html"}, {"path": "package.json"},
    {"path": "api/main.py"}, {"path": "api/routes/users.py"}, {"path": "tests/test_users.py"},
]


def _runner(tmp_path, **kw):
    bus = EventBus()
    settings = Settings(projects_dir=tmp_path / "P", data_dir=tmp_path / "d",
                        logs_dir=tmp_path / "l", critic_enabled=False, **kw)
    return StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)


def _plan(**kw):
    base = dict(slug="demo", brief="a fullstack app", stack="react", stages=[],
                checklist=["package.json", "src/App.jsx"], best_of_n=1)
    base.update(kw)
    return BuildPlan(**base)


_CODE_SPEC = StageSpec(name="code", agent_type="code", capability="codegen")


def test_maybe_slices_gated_off_by_default(tmp_path):
    r = _runner(tmp_path)  # flag defaults False
    assert r._maybe_slices(_plan(), {"architect": {"plan": {"files": _ARCH_FILES}}}) is None


def test_maybe_slices_off_when_best_of_n(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    assert r._maybe_slices(_plan(best_of_n=3),
                           {"architect": {"plan": {"files": _ARCH_FILES}}}) is None


def test_maybe_slices_on_with_flag_and_enough_files(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    slices = r._maybe_slices(_plan(), {"architect": {"plan": {"files": _ARCH_FILES}}})
    assert slices is not None
    assert {"frontend", "backend", "tests", "config"} <= set(slices)
    assert list(slices)[-1] == "config"  # config merges last


def test_run_parallel_slices_merges_every_slice(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    main_wt = create_worktree(str(r.settings.projects_dir), "demo")
    worktrees = [main_wt]
    plan = _plan()
    prior = {"architect": {"plan": {"files": _ARCH_FILES}}}
    slices = r._maybe_slices(plan, prior)

    captured: dict = {}

    async def fake_submit(spec, payload, cid):
        # Simulate a scoped slice agent writing exactly its files into its wt.
        wt = Path(payload["worktree_dir"])
        sc = payload["slice_scope"]
        for rel in sc["files"]:
            p = wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"// {rel}\n", encoding="utf-8")
        captured.setdefault(sc["name"], payload)
        return TaskResult(task_id="x", success=True,
                          output={"files_written": len(sc["files"]), "slice": sc["name"]})

    r._submit_stage = fake_submit  # type: ignore[assignment]

    project_dir = str(r.settings.projects_dir / "demo")
    result = asyncio.run(r._run_code_parallel_slices(
        plan, _CODE_SPEC, project_dir, prior, [], {}, "cid", main_wt, worktrees, slices))

    assert result.success
    merged = set(list_files(main_wt.dir))
    # Files from every slice landed in the main worktree.
    assert {"src/App.jsx", "api/main.py", "tests/test_users.py", "package.json"} <= merged
    assert result.output["files_written"] >= len(_ARCH_FILES)
    assert set(result.metadata["parallel_slices"]["slices"]) == set(slices)
    # Each slice agent received the full manifest as cross-slice context.
    assert "api/main.py" in captured["frontend"]["slice_scope"]["manifest"]


def test_run_parallel_slices_scopes_each_agent_to_its_files(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    main_wt = create_worktree(str(r.settings.projects_dir), "demo")
    plan = _plan()
    prior = {"architect": {"plan": {"files": _ARCH_FILES}}}
    slices = r._maybe_slices(plan, prior)
    seen_scopes: dict = {}

    async def fake_submit(spec, payload, cid):
        sc = payload["slice_scope"]
        seen_scopes[sc["name"]] = set(sc["files"])
        # The scoped plan must only carry this slice's files.
        assert {f["path"] for f in payload["plan"]["files"]} == set(sc["files"])
        return TaskResult(task_id="x", success=True, output={"slice": sc["name"]})

    r._submit_stage = fake_submit  # type: ignore[assignment]
    asyncio.run(r._run_code_parallel_slices(
        plan, _CODE_SPEC, str(r.settings.projects_dir / "demo"),
        prior, [], {}, "cid", main_wt, [main_wt], slices))

    assert "api/main.py" in seen_scopes["backend"]
    assert "api/main.py" not in seen_scopes["frontend"]
    assert "src/App.jsx" in seen_scopes["frontend"]
