"""The reviewer's heuristic must not no_go content-product stacks.

Found by the agent_pack acceptance seal: heuristic_score counted only code
suffixes for substance and only package.json/pyproject-style manifests — a
PERFECT agent pack (markdown personas + catalog.json) scored 46 and no_go'd.
A fifth vocabulary site the drift test never covered; the reviewer now reads
``core.stacks.CONTENT_STACKS`` and scores the content shape.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents._scaffold import scaffold_for
from skyn3t.agents.reviewer import GO_THRESHOLD, heuristic_score


def _materialize(tmp_path: Path, stack: str, brief: str) -> Path:
    for rel, contents in scaffold_for(stack, "demo", brief).items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    return tmp_path


def test_agent_pack_scaffold_clears_the_go_threshold(tmp_path):
    root = _materialize(tmp_path, "agent_pack", "an agent team pack for my law firm")
    score, gaps = heuristic_score(root, {"stack": "agent_pack"})
    assert score >= GO_THRESHOLD, (score, gaps)
    joined = "\n".join(gaps)
    assert "no dependency manifest" not in joined, gaps
    assert "no parseable build" not in joined, gaps


def test_content_treatment_is_scoped_to_content_stacks(tmp_path):
    # The same tree WITHOUT the content-stack claim keeps the strict scoring —
    # a code stack that ships only markdown must still be penalised.
    root = _materialize(tmp_path, "agent_pack", "an agent pack")
    _, gaps_code_stack = heuristic_score(root, {"stack": "fastapi"})
    assert any("manifest" in g for g in gaps_code_stack), gaps_code_stack


def test_broken_catalog_fails_the_config_signal(tmp_path):
    root = _materialize(tmp_path, "agent_pack", "an agent pack")
    (root / "catalog.json").write_text("{not json")
    score, gaps = heuristic_score(root, {"stack": "agent_pack"})
    assert any("catalog.json is not valid JSON" in g for g in gaps), gaps
    assert score < 100.0
