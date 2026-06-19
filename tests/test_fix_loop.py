"""Bounded fix loop: a failing objective proof is repaired, not just reported."""

from __future__ import annotations

from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.planner import Planner
from skyn3t.studio.proof_run import proof_run
from skyn3t.studio.runner import StudioRunner


def _runner():
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def test_fill_missing_synthesizes_real_entrypoint(tmp_path):
    # A package with real code but NO runnable root — the exact failure mode the
    # old pipeline shipped (taskcli-v4). The proof must reject it, and the fix
    # loop must WIRE a real entrypoint to the delivered code, not stub-fill.
    runner = _runner()
    plan = Planner(Settings()).plan("a python cli task tool", "mdi")
    proj = tmp_path / "proj"
    pkg = proj / "mdi"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def run():\n    print('idea')\n    return 0\n")
    (proj / "README.md").write_text("# mdi\n")

    before = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    assert before.passed is False  # no runnable entrypoint -> not proven

    filled = runner._fill_missing(str(proj), plan, "a python cli task tool", list(before.missing))
    assert filled >= 1
    # A real wired entrypoint was synthesized (not an empty stub).
    main_py = (proj / "main.py").read_text()
    assert "from mdi.core import run" in main_py

    after = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    assert after.passed is True


def test_stub_for_known_files():
    runner = _runner()
    plan = Planner(Settings()).plan("x", "x")
    assert "[project]" in runner._stub_for("pyproject.toml", plan, "x")
    assert runner._stub_for("src/__init__.py", plan, "x") == ""
    assert "def test" in runner._stub_for("tests/test_basic.py", plan, "x")
    assert runner._stub_for("weird.xyz", plan, "x") is None


async def test_fix_loop_stops_when_proof_passes(tmp_path):
    # A loop with already-passing proof returns immediately (no infinite loop).
    runner = _runner()
    plan = Planner(Settings()).plan("a python tool", "t")
    proj = tmp_path / "p"
    proj.mkdir()
    for rel in plan.checklist:
        f = proj / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n" if rel.endswith(".py") else "# ok\n")
    (proj / "main.py").write_text("print('hi')\n")

    class _M:
        build_id = "b"
        slug = "t"
        brief = "a python tool"
        files = []
        extra = {}

    proof = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    out = await runner._fix_loop(_M(), plan, str(proj), proof, "cid", {"max_fix_attempts": 2})
    assert out.passed is True


# ---- substance gate + best-of-N richness ranking -------------------------
def test_largest_source_bytes(tmp_path):
    runner = _runner()
    proj = tmp_path / "p"
    (proj / "src").mkdir(parents=True)
    (proj / "main.py").write_text("x" * 50)            # thin source
    (proj / "README.md").write_text("y" * 5000)         # not source (excluded)
    (proj / "test_x.py").write_text("z" * 9000)         # test (excluded)
    assert runner._largest_source_bytes(str(proj)) == 50
    # __init__.py counts (can hold the real implementation); bytes SUM
    (proj / "src" / "__init__.py").write_text("z" * 2000)
    assert runner._largest_source_bytes(str(proj)) == 2050
    assert runner._substance_floor == 1500


def test_best_of_n_prefers_richer_over_more_files():
    from skyn3t.studio.best_of_n import Candidate, select

    class _Proof:
        passed = True
        score = 90.0
        files_substantive = 3

    rich = Candidate(index=0, worktree=None, proof=_Proof(), files_written=2, source_bytes=28000)
    thin = Candidate(index=1, worktree=None, proof=_Proof(), files_written=8, source_bytes=559)
    res = select([thin, rich])
    assert res.winner is rich  # substance beats raw file count


# ---- proof gating: runnable entrypoint + the generated suite -------------
def test_proof_rejects_package_with_no_entrypoint(tmp_path):
    # Real package code but no runnable root: NOT proven (the taskcli failure).
    proj = tmp_path / "p"
    pkg = proj / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def run():\n    return 0\n")
    res = proof_run(str(proj), stack="python")
    assert res.passed is False
    assert "<entrypoint>" in res.missing


def _proj_with_test(tmp_path, body: str):
    proj = tmp_path / "p"
    tests = proj / "tests"
    tests.mkdir(parents=True)
    (proj / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (tests / "test_add.py").write_text(body)
    return proj


def test_proof_runs_generated_tests_pass(tmp_path):
    proj = _proj_with_test(
        tmp_path, "from main import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    res = proof_run(str(proj), stack="python", run_tests=True)
    assert res.passed is True
    assert res.detail.get("tests") == "passed"


def test_proof_runs_generated_tests_fail(tmp_path):
    proj = _proj_with_test(
        tmp_path, "from main import add\n\n\ndef test_add():\n    assert add(1, 2) == 99\n"
    )
    res = proof_run(str(proj), stack="python", run_tests=True)
    assert res.passed is False
    assert res.detail.get("tests") == "failed"
    assert "<tests>" in res.missing


def test_proof_soft_skips_when_no_tests(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "main.py").write_text("def add(a, b):\n    return a + b\n")
    res = proof_run(str(proj), stack="python", run_tests=True)
    assert res.passed is True  # no tests is a soft skip, never a hard fail
    assert res.detail.get("tests") == "skipped"


def test_proof_does_not_run_tests_by_default(tmp_path):
    # run_tests defaults OFF: a failing test in the tree must NOT fail a proof
    # unless test execution is explicitly enabled (offline/CI kill-switch).
    proj = tmp_path / "p"
    tests = proj / "tests"
    tests.mkdir(parents=True)
    (proj / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (tests / "test_x.py").write_text("def test_x():\n    assert False\n")
    res = proof_run(str(proj), stack="python")  # no run_tests
    assert res.passed is True
    assert "tests" not in res.detail


# ---- proof gating: real node/react compile (npm install + npm run build) ----
import json as _json
import shutil

import pytest


def _node_proj(tmp_path, build_script: str | None):
    proj = tmp_path / "p"
    (proj / "src").mkdir(parents=True)
    pkg = {"name": "t", "version": "1.0.0", "private": True}
    if build_script is not None:
        pkg["scripts"] = {"build": build_script}
    (proj / "package.json").write_text(_json.dumps(pkg, indent=2) + "\n")
    (proj / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script></body></html>\n'
    )
    (proj / "src" / "main.jsx").write_text("console.log('hi')\n")
    return proj


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_proof_node_build_pass(tmp_path):
    proj = _node_proj(tmp_path, 'node -e "process.exit(0)"')
    res = proof_run(str(proj), stack="react", run_build=True, build_timeout=120)
    assert res.passed is True
    assert res.detail.get("build") == "passed"


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_proof_node_build_fail(tmp_path):
    proj = _node_proj(tmp_path, 'node -e "process.exit(1)"')
    res = proof_run(str(proj), stack="react", run_build=True, build_timeout=120)
    assert res.passed is False
    assert res.detail.get("build") == "failed"
    assert "<build>" in res.missing


def test_proof_node_build_soft_skip_no_script(tmp_path):
    proj = _node_proj(tmp_path, None)  # no build script
    res = proof_run(str(proj), stack="react", run_build=True)
    assert res.passed is True  # no build script -> soft skip, never a hard fail
    assert res.detail.get("build") == "skipped"


def test_proof_does_not_run_build_by_default(tmp_path):
    # run_build defaults OFF at the call site (heavyweight/network); only the
    # runner turns it on from settings.
    proj = _node_proj(tmp_path, 'node -e "process.exit(1)"')
    res = proof_run(str(proj), stack="react")  # no run_build
    assert res.passed is True
    assert "build" not in res.detail
