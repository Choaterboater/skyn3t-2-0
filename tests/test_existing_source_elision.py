"""Existing instructional text must not make real self-improvement impossible."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.agents.validate import validate_source

MARKER = "." * 3 + " unchanged"
PROMPT = f"PROMPT = {('Never replace implementation with ' + MARKER)!r}\n"
ORIGINAL = PROMPT + "\ndef answer():\n    return 1\n"
EDITED = PROMPT + "\ndef answer():\n    return 2\n"


def test_existing_instructional_marker_does_not_reject_a_complete_edit():
    assert validate_source(
        "module.py", EDITED, existing_project=True, original=ORIGINAL,
    ) == (True, "")


def test_original_argument_does_not_relax_new_project_validation():
    assert validate_source("module.py", EDITED, original=ORIGINAL)[0] is False


@pytest.mark.parametrize("content", [
    EDITED + f"\ndef unfinished():\n    # {MARKER}\n    pass\n",
    EDITED + PROMPT,
    f"def unfinished():\n    # {MARKER}\n    pass\n",
])
def test_existing_marker_never_exempts_new_or_relocated_stub_markers(content):
    ok, reason = validate_source(
        "module.py", content, existing_project=True, original=ORIGINAL,
    )
    assert ok is False
    assert "stub" in reason


def test_new_file_in_existing_project_has_no_marker_allowance():
    assert validate_source("new.py", EDITED, existing_project=True)[0] is False


async def test_agentic_improver_retains_complete_edits_with_existing_prompt_text(tmp_path):
    path = tmp_path / "module.py"
    path.write_text(ORIGINAL)
    agent = CodeImproverAgent.__new__(CodeImproverAgent)

    async def improve(prompt, workdir, **kwargs):
        path.write_text(EDITED)
        return {"ok": True}

    agent.llm = SimpleNamespace(agentic_build=improve, backend="copilot_cli")
    improved, skipped, ran, error = await agent._agentic_improve(
        tmp_path, "existing Python library", ["improve answer"], "python",
        {"existing_project": True},
    )
    assert ran and not error
    assert improved == ["module.py"]
    assert skipped == {}
    assert path.read_text() == EDITED


async def test_agentic_improver_still_reverts_introduced_elision(tmp_path):
    path = tmp_path / "module.py"
    path.write_text(ORIGINAL)
    broken = EDITED + f"\ndef unfinished():\n    # {MARKER}\n    pass\n"
    agent = CodeImproverAgent.__new__(CodeImproverAgent)

    async def improve(prompt, workdir, **kwargs):
        path.write_text(broken)
        return {"ok": True}

    agent.llm = SimpleNamespace(agentic_build=improve, backend="copilot_cli")
    improved, skipped, ran, error = await agent._agentic_improve(
        tmp_path, "existing Python library", ["improve answer"], "python",
        {"existing_project": True},
    )
    assert ran and not error
    assert improved == []
    assert "module.py" in skipped
    assert path.read_text() == ORIGINAL
