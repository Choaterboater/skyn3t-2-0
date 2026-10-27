"""Generated artifacts retain the exact bytes used by provenance hashes."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skyn3t.studio.design_tokens import render_design_md, write_design_md
from skyn3t.studio.planner import BuildPlan
from skyn3t.studio.runner import StudioRunner, degraded_baseline_sha256


@pytest.fixture
def windows_text_writes(monkeypatch):
    """Exercise Windows text-mode newline translation on every test platform."""
    original = Path.open

    def open_with_windows_default(self, mode="r", buffering=-1, encoding=None,
                                  errors=None, newline=None):
        if "w" in mode and "b" not in mode and newline is None:
            newline = "\r\n"
        return original(self, mode=mode, buffering=buffering, encoding=encoding,
                        errors=errors, newline=newline)

    monkeypatch.setattr(Path, "open", open_with_windows_default)


def test_generated_design_preserves_token_block_bytes(tmp_path, windows_text_writes):
    brief = "a cozy bakery site"
    assert write_design_md(tmp_path, brief)
    assert (tmp_path / "DESIGN.md").read_bytes() == render_design_md(brief).encode("utf-8")


def test_missing_source_baseline_binds_exact_generated_bytes(
    tmp_path, monkeypatch, windows_text_writes,
):
    content = "def step(state):\n    return state\n"
    rel = "app/engine.py"
    monkeypatch.setattr(StudioRunner, "_runner_scaffold", staticmethod(lambda *_: {rel: content}))
    runner = StudioRunner.__new__(StudioRunner)
    plan = BuildPlan(slug="tool", brief="a tool", stack="python_cli", stages=[])
    assert runner._fill_missing(str(tmp_path), plan, "a tool", [rel]) == 1
    raw = (tmp_path / rel).read_bytes()
    assert raw == content.encode("utf-8")
    assert degraded_baseline_sha256(f"missing 1 owned file(s): {rel}", tmp_path)[rel] == hashlib.sha256(raw).hexdigest()
