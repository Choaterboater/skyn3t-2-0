"""TestAuthor's generated entrypoint check vs. real JS app layouts, and the
pytest failure text the proof hands the repair loop.

The authored acceptance file is restored after every repair attempt, so an
entrypoint list that misses a stack's convention (Next.js app/pages router,
Remix, Expo, Vite React) is a false failure no repair can clear: it flipped
proof.passed for working apps. And when pytest did fail for real, a blind
500-char tail often cut off the assertion the repair loop needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import skyn3t.studio.proof_run as proof_mod
from skyn3t.agents import _verify_common as vc
from skyn3t.agents.test_author import (
    _CONFIG_ONLY_ENTRY_MARKERS,
    TestAuthorAgent,
    render_test_file,
)
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

_SRC = "export default function Page() { return null }\n"

_STACK_TREES = {
    "nextjs-app-router": {
        "package.json": "{}",
        "next.config.mjs": "export default {}\n",
        "app/layout.tsx": _SRC,
        "app/page.tsx": _SRC,
    },
    "nextjs-app-router-js-src": {
        "package.json": "{}",
        "src/app/page.js": _SRC,
        "src/components/Hero.js": _SRC,
    },
    "nextjs-pages-router": {
        "package.json": "{}",
        "next.config.js": "module.exports = {}\n",
        "pages/_app.jsx": _SRC,
        "pages/index.jsx": _SRC,
    },
    "nextjs-pages-router-src": {
        "package.json": "{}",
        "src/pages/index.tsx": _SRC,
    },
    "remix": {
        "package.json": "{}",
        "app/root.tsx": _SRC,
        "app/routes/_index.tsx": _SRC,
    },
    "expo-classic": {
        "package.json": "{}",
        "app.json": "{}",
        "App.js": _SRC,
    },
    "expo-router": {
        "package.json": "{}",
        "app/_layout.tsx": _SRC,
        "app/(tabs)/home.tsx": _SRC,
    },
    # index.html alone was already accepted; this pins the module entry itself.
    "vite-react": {
        "package.json": "{}",
        "vite.config.js": "export default {}\n",
        "src/main.jsx": _SRC,
        "src/App.jsx": _SRC,
    },
}


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _generated_namespace(root: Path) -> dict:
    generated = render_test_file(
        ["project produces at least one runnable entrypoint"], "A web app", "app",
    )
    namespace = {"__file__": str(root / "tests" / "test_acceptance_app.py")}
    exec(compile(generated, namespace["__file__"], "exec"), namespace)
    return namespace


@pytest.fixture(autouse=True)
def _no_project_dir_override(monkeypatch):
    monkeypatch.delenv("SKYN3T_PROJECT_DIR", raising=False)


@pytest.mark.parametrize("stack", sorted(_STACK_TREES))
def test_generated_entrypoint_check_accepts_js_stack_layouts(tmp_path, stack):
    _write_tree(tmp_path, _STACK_TREES[stack])
    namespace = _generated_namespace(tmp_path)

    namespace["test_project_has_source_content"]()
    namespace["test_project_has_entrypoint"]()


def test_generated_entrypoint_check_still_fails_without_an_entrypoint(tmp_path):
    # Config markers, and the path-restricted .js/.ts route-convention variants
    # and Expo _layout.* outside their route directories, are not runnable
    # entries. (page/layout/root/_index .jsx/.tsx are accepted anywhere — they
    # come from the shared vc.ENTRYPOINT_NAMES; see the next test.)
    _write_tree(tmp_path, {
        "package.json": "{}",
        "next.config.mjs": "export default {}\n",
        "components/page.js": _SRC,
        "components/root.js": _SRC,
        "lib/_layout.ts": _SRC,
        "src/utils/format.ts": _SRC,
    })
    namespace = _generated_namespace(tmp_path)

    namespace["test_project_has_source_content"]()
    with pytest.raises(AssertionError, match="no recognizable entrypoint"):
        namespace["test_project_has_entrypoint"]()


def test_generated_entrypoint_check_accepts_every_runner_entrypoint_name(tmp_path):
    # The runner's own gate (vc.find_entrypoints) and the authored contract must
    # agree, or a tree the runner deems runnable fails a contract no repair can
    # change. Config-only markers are the deliberate exception.
    for name in sorted(vc.ENTRYPOINT_NAMES - _CONFIG_ONLY_ENTRY_MARKERS):
        root = tmp_path / name.replace("+", "plus")
        _write_tree(root, {f"src/{name}": "x = 1\n"})
        assert vc.find_entrypoints(root), name
        _generated_namespace(root)["test_project_has_entrypoint"]()


def test_authored_acceptance_file_passes_under_real_pytest_for_nextjs(tmp_path):
    # End to end: the file TestAuthor writes, run by the proof's pytest probe
    # exactly as a Node-stack build runs it.
    _write_tree(tmp_path, _STACK_TREES["nextjs-app-router"])
    agent = TestAuthorAgent(event_bus=EventBus())
    result = asyncio.run(agent.run(TaskRequest(type="test_author", payload={
        "worktree_dir": str(tmp_path), "brief": "A Next.js landing page",
        "slug": "app", "stack": "nextjs",
    })))
    assert result.output["test_files"] == ["tests/test_acceptance_app.py"]

    ran, passed, summary = proof_mod._run_generated_tests(tmp_path, "nextjs", 60)
    assert (ran, passed) == (True, True), summary

    for rel in ("app/page.tsx", "app/layout.tsx"):
        (tmp_path / rel).unlink()
    ran, passed, summary = proof_mod._run_generated_tests(tmp_path, "nextjs", 60)
    assert (ran, passed) == (True, False)
    assert "test_project_has_entrypoint" in summary
    assert "no recognizable entrypoint" in summary


# --- pytest failure text for the repair loop ---------------------------------

def _noisy_pytest_output(failures: int = 6) -> str:
    traceback = "\n".join(f"    line_{i} = compute({i})  # unrelated context" for i in range(40))
    blocks = []
    for n in range(failures):
        blocks.append(
            f"_________________________________ test_case_{n} _________________________________\n"
            f"\n    def test_case_{n}():\n{traceback}\n"
            f">       assert result == {n}\n"
            f"E       AssertionError: case {n} expected {n} but the app returned None\n"
            f"E       assert None == {n}\n"
            f"\ntests/test_app.py:{10 + n}: AssertionError"
        )
    warnings = "\n".join(
        f"tests/test_app.py::test_case_0\n  /venv/lib/site-packages/lib.py:{i}: "
        f"DeprecationWarning: noisy deprecation number {i}"
        for i in range(60)
    )
    summary = "\n".join(
        f"FAILED tests/test_app.py::test_case_{n} - AssertionError: case {n} expected {n}..."
        for n in range(failures)
    )
    return (
        "FFFFFF" + "." * 20 + "                                [100%]\n"
        "=================================== FAILURES ===================================\n"
        + "\n".join(blocks)
        + "\n=============================== warnings summary ===============================\n"
        + warnings
        + "\n=========================== short test summary info ============================\n"
        + summary
        + f"\n{failures} failed, 20 passed, 60 warnings in 0.52s"
    )


def test_distilled_pytest_failures_keep_failed_and_assertion_lines():
    out = _noisy_pytest_output()
    # The old blind tail lost the assertion detail entirely.
    assert "E       AssertionError: case 0" not in out[-500:]

    kept = proof_mod._distill_pytest_failures(out)

    assert len(kept) <= 1800
    assert "FAILED tests/test_app.py::test_case_0 - AssertionError" in kept
    assert "E       AssertionError: case 0 expected 0 but the app returned None" in kept
    assert kept.index("FAILED tests/test_app.py::test_case_0") < kept.index("E       AssertionError")
    # The tail (with pytest's totals line) is still carried.
    assert kept.rstrip().endswith("6 failed, 20 passed, 60 warnings in 0.52s")
    # Traceback context lines are not mistaken for failure lines.
    assert "unrelated context" not in kept.split("...pytest output tail:")[0]


def test_distilled_pytest_failures_short_or_lineless_output_is_plain_tail():
    short = "F\nFAILED tests/test_x.py::test_y - assert 1 == 2\n1 failed in 0.01s"
    assert proof_mod._distill_pytest_failures(short) == short

    crash = "Traceback (most recent call last):\n" + "x" * 3000 + "\nSystemExit: 3"
    assert proof_mod._distill_pytest_failures(crash) == crash[-1800:]

    logged = "ERROR    app:main.py:3 boom\n" + "y" * 3000
    assert proof_mod._distill_pytest_failures(logged) == logged[-1800:]


def test_failed_generated_tests_summary_keeps_assertion_from_noisy_run(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_noisy.py").write_text(
        "import warnings\n\n\n"
        "def test_entry():\n"
        "    for i in range(80):\n"
        "        warnings.warn(f'noisy warning number {i} ' + 'x' * 40, DeprecationWarning)\n"
        "    assert 'app/page.tsx' == 'pages/index.tsx', 'entry route is wired wrong'\n",
        encoding="utf-8",
    )

    ran, passed, summary = proof_mod._run_generated_tests(tmp_path, "python", 60)

    assert (ran, passed) == (True, False)
    assert len(summary) <= 1800
    assert "FAILED tests/test_noisy.py::test_entry" in summary
    assert "E       AssertionError: entry route is wired wrong" in summary
    error_gaps = proof_mod.extract_error_gaps({"tests": "failed", "test_summary": summary})
    assert any(
        gap.startswith("TESTS FAILED") and "entry route is wired wrong" in gap
        for gap in error_gaps
    )
