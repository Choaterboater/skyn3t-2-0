"""Regression tests for the team's parallel-debug findings (2026-06-21).

- mobile Finding 1 (HIGH): react_native built no_go because `tsc --noEmit` choked
  on the Jest test file — the typecheck must exclude __tests__.
- mobile Finding 2 (MEDIUM): a mobile app is neither web nor server, so packaging
  must NOT add a Dockerfile/compose/.env/web-config.
- mobile Finding 3 (LOW): the planner file checklist must not name files the
  offline scaffold never emits (phantom 'missing' + depressed scores).
"""

from __future__ import annotations

import json

from skyn3t.agents._scaffold import scaffold_for
from skyn3t.agents.stack_detector import StackDetector
from skyn3t.studio.planner import _STACK_FILE_CHECKLIST


def test_react_native_tsconfig_excludes_tests():
    cfg = json.loads(scaffold_for("react_native", "demo", "a fitness app")["tsconfig.json"])
    assert "__tests__" in cfg.get("exclude", [])


def test_react_native_classified_as_mobile_not_fullstack(tmp_path):
    for rel, content in scaffold_for("react_native", "demo", "a fitness app").items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    report = StackDetector.analyze(tmp_path)
    assert report.family == "mobile"
    assert report.has_web is False
    assert report.has_server is False


def test_planner_checklist_matches_scaffold_output():
    # planner stack -> scaffold builder key
    pairs = {
        "fastapi": "fastapi",
        "static": "static_html",
        "react": "react_vite",
        "react_native": "react_native",
    }
    for planner_stack, builder in pairs.items():
        files = set(scaffold_for(builder, "demo", "a task manager"))
        missing = set(_STACK_FILE_CHECKLIST[planner_stack]) - files
        assert not missing, f"{planner_stack} checklist names non-emitted files: {missing}"
