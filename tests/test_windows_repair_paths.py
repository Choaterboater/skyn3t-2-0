"""Windows diagnostic separators remain portable repair and artifact paths."""

import asyncio
from pathlib import Path, PureWindowsPath

import pytest

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.agents.test_author import TestAuthorAgent as AuthorAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


@pytest.mark.parametrize("style", ["windows", "mixed", "file-url"])
@pytest.mark.parametrize("relative", ["config/workflows.yaml", ".github/workflows/ci.yml", "src/server.py"])
def test_nested_repair_target_survives_windows_diagnostic_separators(tmp_path, style, relative):
    root = tmp_path / "MCP project"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("broken\n", encoding="utf-8")
    windows_root = str(root).replace("/", "\\")
    named = windows_root + "\\" + relative.replace("/", "\\")
    if style == "mixed":
        named = windows_root + "/" + relative
    elif style == "file-url":
        named = "file://" + named
    gap = f"TESTS FAILED:\nERROR: {named}: invalid configuration at line 1"
    agent = CodeImproverAgent(event_bus=EventBus())
    assert agent._targets_from_gaps([gap], root) == [relative]


def test_windows_file_url_keeps_failing_test_after_source_and_drops_ghost(tmp_path):
    for relative in ("tests/css-contract.test.mjs", "app/globals.css", "components/Logo.jsx"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    windows_root = str(tmp_path).replace("/", "\\")
    gap = (
        "NODE TESTS FAILED: AssertionError in globals.css, app/globals.css, components/Logo.jsx\n"
        f"at TestContext (file://{windows_root}/tests/css-contract.test.mjs:232:12)"
    )
    targets = CodeImproverAgent(event_bus=EventBus())._targets_from_gaps([gap], tmp_path)
    assert targets == ["app/globals.css", "components/Logo.jsx", "tests/css-contract.test.mjs"]


def test_normalized_windows_import_still_rejects_worktree_escape(tmp_path):
    (tmp_path / "src").mkdir()
    agent = CodeImproverAgent(event_bus=EventBus())
    targets = agent._targets_from_gaps(
        [r"UNRESOLVED IMPORT: src\main.js -> ..\..\outside.py"], tmp_path,
    )
    assert "outside.py" not in targets
    assert not agent._confined(tmp_path, (tmp_path / "../outside.py").resolve())


def test_authored_test_file_path_is_portable_for_windows_relative_paths(tmp_path, monkeypatch):
    original = type(tmp_path).relative_to

    def windows_relative(path, *args, **kwargs):
        relative = original(path, *args, **kwargs)
        if path.name == "test_acceptance_app.py":
            return PureWindowsPath(*relative.parts)
        return relative

    with monkeypatch.context() as context:
        context.setattr(type(tmp_path), "relative_to", windows_relative)
        result = asyncio.run(AuthorAgent(event_bus=EventBus()).run(TaskRequest(
            type="test_author", payload={"worktree_dir": str(tmp_path), "slug": "app"},
        )))
    assert result.success
    assert result.output["test_files"] == ["tests/test_acceptance_app.py"]
    assert (Path(tmp_path) / "tests" / "test_acceptance_app.py").is_file()
