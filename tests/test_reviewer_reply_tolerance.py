"""A live review reply hiccup must not become a silent score-0 no_go.

Real-model builds lost their whole score to the reviewer: a fenced or
prose-wrapped JSON reply, object-shaped gaps, or a reasoning model burning a
1500-token ceiling before emitting JSON all read as "unavailable" (score 0,
which dominates the final blend). The reply is now parsed tolerantly, gaps are
coerced to strings, the output ceiling is larger, and a failed attempt gets one
retry. A review STILL unavailable after the retry stays fail-closed: explicitly
unavailable, score 0, never a passing structural fallback.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from skyn3t.adapters.llm import LLMResult
from skyn3t.agents.reviewer import ReviewerAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


def _good_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"name": "x", "scripts": {}}))
    (root / "index.js").write_text("function main() { return 42; }\nmain();\n")
    for name in ("util.js", "lib.js", "more.js", "extra.js"):
        (root / name).write_text("export const A = 1;\nexport const B = 2;\n")
    return root


class _Judge:
    """Live-looking client replaying scripted replies (str text or LLMResult)."""

    backend = "openrouter"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def complete(self, prompt, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, LLMResult):
            return reply
        return LLMResult(text=reply, model="m", backend="openrouter")


def _review(tmp_path, judge):
    return asyncio.run(ReviewerAgent(event_bus=EventBus(), llm_client=judge).execute(
        TaskRequest(type="review", payload={"project_dir": str(_good_project(tmp_path))}),
    ))


def test_fenced_json_reply_is_assessed(tmp_path):
    judge = _Judge('```json\n{"score": 82, "gaps": ["no retry on fetch"]}\n```')
    result = _review(tmp_path, judge)
    assert result.metadata["assessment_status"] == "assessed"
    assert result.metadata["review_attempts"] == 1
    assert result.output["score"] == 82
    assert result.output["verdict"] == "go"
    assert "no retry on fetch" in result.output["gaps"]


def test_prose_wrapped_json_reply_is_assessed(tmp_path):
    judge = _Judge('Here is my review:\n{"score": 74, "gaps": []}\nHope this helps.')
    result = _review(tmp_path, judge)
    assert result.metadata["assessment_status"] == "assessed"
    assert result.output["score"] == 74
    assert len(judge.calls) == 1


def test_object_gaps_are_coerced_to_readable_strings(tmp_path):
    judge = _Judge(json.dumps({"score": 70, "gaps": [
        {"file": "src/api.py", "function": "create_user", "issue": "no input validation"},
        {"severity": None, "flag": True},
        None,
        "",
        "plain gap",
    ]}))
    result = _review(tmp_path, judge)
    assert result.metadata["assessment_status"] == "assessed"
    gaps = result.output["gaps"]
    assert "src/api.py: create_user: no input validation" in gaps
    assert '{"severity":null,"flag":true}' in gaps
    assert "plain gap" in gaps
    assert "" not in gaps and None not in gaps


def test_bare_string_gaps_becomes_a_single_gap(tmp_path):
    result = _review(tmp_path, _Judge('{"score": 65, "gaps": "auth is stubbed"}'))
    assert result.metadata["assessment_status"] == "assessed"
    assert "auth is stubbed" in result.output["gaps"]


def test_gap_caps_still_apply(tmp_path):
    judge = _Judge(json.dumps({"score": 70, "gaps": [{"issue": "x" * 5000}] * 30}))
    result = _review(tmp_path, judge)
    review_gaps = [g for g in result.output["gaps"] if g.startswith("x")]
    assert len(review_gaps) == 12
    assert all(len(g) == 1000 for g in review_gaps)


def test_uncoercible_gaps_type_is_still_rejected(tmp_path):
    judge = _Judge('{"score": 90, "gaps": 7}')
    result = _review(tmp_path, judge)
    assert result.metadata["assessment_status"] == "unavailable"
    assert result.output["score"] == 0.0


def test_garbage_then_valid_reply_is_assessed_on_the_retry(tmp_path):
    judge = _Judge("", '{"score": 88, "gaps": []}')
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 2
    assert result.metadata["assessment_status"] == "assessed"
    assert result.metadata["review_attempts"] == 2
    assert result.output["score"] == 88
    assert result.output["verdict"] == "go"


def test_failed_provider_status_then_valid_reply_is_assessed(tmp_path):
    judge = _Judge(
        LLMResult(text="", model="m", backend="openrouter", status="malformed_response"),
        '{"score": 77, "gaps": []}',
    )
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 2
    assert result.metadata["assessment_status"] == "assessed"
    assert result.output["score"] == 77


def test_garbage_twice_stays_unavailable_with_score_zero(tmp_path):
    judge = _Judge("I think this project is pretty good!", "still not json")
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 2
    assert result.metadata["assessment_status"] == "unavailable"
    assert result.metadata["review_attempts"] == 2
    assert result.metadata["review_error"] == "ValueError"
    assert result.output["score"] == 0.0
    assert result.output["verdict"] == "no_go"
    assert any(g.startswith("Semantic review unavailable") for g in result.output["gaps"])


def test_failed_provider_twice_stays_unavailable(tmp_path):
    judge = _Judge(LLMResult(text='{"score":95}', model="m", backend="openrouter", status="failed"))
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 2
    assert result.metadata["assessment_status"] == "unavailable"
    assert result.output["score"] == 0.0
    assert result.output["verdict"] == "no_go"


@pytest.mark.parametrize("text", ['{"score": true, "gaps": []}', '```json\n{"score": false}\n```'])
def test_bool_score_is_still_rejected(tmp_path, text):
    judge = _Judge(text)
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 2
    assert result.metadata["assessment_status"] == "unavailable"
    assert result.output["score"] == 0.0
    assert result.output["verdict"] == "no_go"


def test_raised_call_is_not_retried(tmp_path):
    judge = _Judge(RuntimeError("budget exhausted"))
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 1
    assert result.metadata["assessment_status"] == "unavailable"
    assert result.metadata["review_attempts"] == 1
    assert result.metadata["review_error"] == "RuntimeError"
    assert result.output["score"] == 0.0


def test_review_requests_a_4000_token_ceiling(tmp_path):
    judge = _Judge('{"score": 70, "gaps": []}')
    _review(tmp_path, judge)
    assert judge.calls[0]["max_tokens"] == 4000


def test_no_live_client_is_not_retried():
    review, error, attempts = asyncio.run(
        ReviewerAgent(event_bus=EventBus())._maybe_llm_review({}, None),
    )
    assert review is None
    assert error == "no_live_client"
    assert attempts == 0
