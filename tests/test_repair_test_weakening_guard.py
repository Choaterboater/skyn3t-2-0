"""Do-no-harm: a test-failure repair must not "pass" by weakening the test.

bellwork-v3: a css-contract test failed because app/globals.css had no `.light`
rule for a class components/Logo.jsx uses. Over 6 fix-loop rounds the repair
edited tests/css-contract.test.mjs and tests/demo-store.test.mjs instead of the
source. Target ordering (source first) is fixed separately; this pins the write
guard: when the gap is a TEST failure, a rewrite of a test file that drops
assertions / test cases, adds skip/todo/only, or swaps in a tautology is
rejected (original kept, skipped reason "weakened_test"). Legit test fixes that
keep the assertions (e.g. a wrong import path) and all source edits still land.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.agents.code_improver import CodeImproverAgent, _weakens_test
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

_TEST_ORIG = (
    "import test from 'node:test';\n"
    "import assert from 'node:assert/strict';\n"
    "import { classes } from '../lib/css.mjs';\n\n"
    "test('every class used has a rule', () => {\n"
    "  assert.deepEqual(classes.missing, []);\n"
    "});\n\n"
    "test('logo uses light variant', () => {\n"
    "  assert.ok(classes.all.includes('light'));\n"
    "});\n"
)
_TEST_ASSERT_DELETED = (
    "import test from 'node:test';\n"
    "import assert from 'node:assert/strict';\n"
    "import { classes } from '../lib/css.mjs';\n\n"
    "test('every class used has a rule', () => {\n"
    "  // relaxed: .light is optional\n"
    "});\n\n"
    "test('logo uses light variant', () => {\n"
    "  assert.ok(classes.all.includes('light'));\n"
    "});\n"
)
_TEST_IMPORT_FIXED = _TEST_ORIG.replace("'../lib/css.mjs'", "'../lib/css-contract.mjs'")
_GAP = (
    "NODE TESTS FAILED — fix these failing tests:\n"
    "✖ every class used has a rule\n"
    "  AssertionError: classes without a rule in app/globals.css:\n"
    "    components/Logo.jsx: .light\n"
    "      at TestContext (tests/css-contract.test.mjs:6:10)\n"
)


@pytest.mark.parametrize("weakened", [
    _TEST_ASSERT_DELETED,
    _TEST_ORIG.replace("test('logo uses", "test.skip('logo uses"),
    _TEST_ORIG.replace("test('logo uses", "test.todo('logo uses"),
    _TEST_ORIG.replace("test('every class", "test.only('every class"),
    _TEST_ORIG.replace("assert.deepEqual(classes.missing, []);", "assert.ok(true);"),
    _TEST_ORIG.replace(
        "assert.deepEqual(classes.missing, []);",
        "// assert.deepEqual(classes.missing, []);"),
    _TEST_ORIG.split("\n\ntest('logo uses")[0] + "\n",  # whole test case removed
])
def test_weakening_rewrites_are_detected(weakened):
    assert _weakens_test("tests/css-contract.test.mjs", _TEST_ORIG, weakened) is True


def test_import_fix_and_added_assertions_are_not_weakening():
    rel = "tests/css-contract.test.mjs"
    assert _weakens_test(rel, _TEST_ORIG, _TEST_IMPORT_FIXED) is False
    stronger = _TEST_ORIG.replace(
        "assert.ok(classes.all.includes('light'));",
        "assert.ok(classes.all.includes('light'));\n  assert.equal(classes.missing.length, 0);")
    assert _weakens_test(rel, _TEST_ORIG, stronger) is False


def test_python_test_weakening_detected():
    orig = "def test_a():\n    assert f() == 1\n\ndef test_b():\n    assert g()\n"
    assert _weakens_test("tests/test_x.py", orig, orig.replace("    assert g()\n", "    pass\n"))
    assert _weakens_test("tests/test_x.py", orig,
                         "import pytest\n\n@pytest.mark.skip\n" + orig)
    assert _weakens_test("tests/test_x.py", orig, orig.replace("assert f() == 1", "assert True"))
    assert not _weakens_test("tests/test_x.py", orig, "import os\n" + orig)


def test_non_test_files_and_new_files_are_never_flagged():
    assert _weakens_test("app/globals.css", ".a{}\n.b{}\n", ".a{}\n") is False
    assert _weakens_test("lib/util.js", "assert(x);\n", "x;\n") is False
    assert _weakens_test("tests/new.test.mjs", "", "test('x', () => {});\n") is False


# --- the classic (per-file) repair write path --------------------------------

class _LLM:
    backend = "openrouter"

    def __init__(self, by_file):
        self.by_file = by_file

    async def complete(self, prompt, **kwargs):
        return SimpleNamespace(text=self.by_file.get(kwargs.get("file_hint"), ""),
                               backend="openrouter")


def _seed(tmp_path: Path) -> None:
    for rel, body in {
        "tests/css-contract.test.mjs": _TEST_ORIG,
        "app/globals.css": ".dark { color: black; }\n",
        "components/Logo.jsx": "export default function Logo() {\n"
                               "  return <span className=\"light\">B</span>;\n}\n",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8")


def _run(tmp_path: Path, llm, gaps, files):
    improver = CodeImproverAgent(event_bus=EventBus(), llm=llm)

    async def go():
        await improver.start()
        return await improver.run(TaskRequest(type="code_improve", payload={
            "brief": "bellwork", "worktree_dir": str(tmp_path), "gaps": gaps,
            "files": files, "agentic": False}))

    return asyncio.run(go())


def test_classic_repair_rejects_weakened_test_but_keeps_source_fix(tmp_path):
    _seed(tmp_path)
    css_fixed = ".dark { color: black; }\n.light { color: white; }\n"
    llm = _LLM({"tests/css-contract.test.mjs": _TEST_ASSERT_DELETED,
                "app/globals.css": css_fixed})
    result = _run(tmp_path, llm, [_GAP], ["app/globals.css", "tests/css-contract.test.mjs"])
    assert result.success
    assert result.output["files"] == ["app/globals.css"]
    assert result.output["skipped"]["tests/css-contract.test.mjs"] == "weakened_test"
    assert (tmp_path / "tests/css-contract.test.mjs").read_text() == _TEST_ORIG
    assert (tmp_path / "app/globals.css").read_text() == css_fixed


def test_classic_repair_accepts_test_import_fix(tmp_path):
    _seed(tmp_path)
    llm = _LLM({"tests/css-contract.test.mjs": _TEST_IMPORT_FIXED})
    result = _run(tmp_path, llm, [_GAP], ["tests/css-contract.test.mjs"])
    assert result.output["files"] == ["tests/css-contract.test.mjs"]
    assert (tmp_path / "tests/css-contract.test.mjs").read_text() == _TEST_IMPORT_FIXED


def test_guard_only_applies_to_test_failure_gaps(tmp_path):
    """A free-text improve goal (not a failing-test repair) may restructure tests."""
    _seed(tmp_path)
    llm = _LLM({"tests/css-contract.test.mjs": _TEST_ASSERT_DELETED})
    result = _run(tmp_path, llm, ["simplify the css contract test"],
                  ["tests/css-contract.test.mjs"])
    assert result.output["files"] == ["tests/css-contract.test.mjs"]


# --- the agentic improve snapshot path ---------------------------------------

class _AgenticLLM:
    backend = "openrouter"

    def __init__(self, writes):
        self.writes = writes

    async def agentic_build(self, prompt, workdir, **kwargs):
        for rel, content in self.writes.items():
            (Path(workdir) / rel).write_text(content, encoding="utf-8")
        return {"ok": True, "backend": "openrouter"}

    async def complete(self, prompt, **kwargs):  # pragma: no cover - not reached
        return SimpleNamespace(text="", backend="openrouter")


def test_agentic_repair_reverts_weakened_test_but_keeps_source_fix(tmp_path):
    _seed(tmp_path)
    css_fixed = ".dark { color: black; }\n.light { color: white; }\n"
    llm = _AgenticLLM({"tests/css-contract.test.mjs": _TEST_ASSERT_DELETED,
                       "app/globals.css": css_fixed})
    improver = CodeImproverAgent(event_bus=EventBus(), llm=llm)

    async def go():
        await improver.start()
        return await improver.run(TaskRequest(type="code_improve", payload={
            "brief": "bellwork", "worktree_dir": str(tmp_path), "gaps": [_GAP],
            "agentic": True}))

    result = asyncio.run(go())
    assert result.success
    assert result.output["files"] == ["app/globals.css"]
    assert result.output["skipped"]["tests/css-contract.test.mjs"] == "weakened_test"
    assert (tmp_path / "tests/css-contract.test.mjs").read_text() == _TEST_ORIG
