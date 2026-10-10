"""A live review reply hiccup must not become a silent score-0 no_go.

Real-model builds lost their whole score to the reviewer: a fenced JSON reply,
object-shaped gaps, or a reasoning model burning a 1500-token ceiling before
emitting JSON all read as "unavailable" (score 0, which dominates the final
blend). Now a reply that is exactly one fenced block around strict JSON is
read, gaps are coerced to strings, the output ceiling is larger, and a failed
attempt gets one retry. A review STILL unavailable after the retry stays
fail-closed: explicitly unavailable, score 0, never a passing fallback.

Nothing is picked out of prose or a partly broken reply. Every extraction
heuristic tried here could be steered so that JSON quoted from the untrusted
brief/source/evidence, or a per-file object nested in a cut-off verdict, picked
the score (a false go). Those replies retry and then fail closed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from skyn3t.adapters.llm import LLMResult
from skyn3t.agents.reviewer import ReviewerAgent, _parse_review
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


def _assert_fails_closed(result, judge):
    assert len(judge.calls) == 2
    assert result.metadata["assessment_status"] == "unavailable"
    assert result.metadata["review_attempts"] == 2
    assert result.output["score"] == 0.0
    assert result.output["verdict"] == "no_go"


@pytest.mark.parametrize("text", [
    '```json\n{"score": 82, "gaps": ["no retry on fetch"]}\n```',
    '```\n{"score": 82, "gaps": ["no retry on fetch"]}\n```',
    '  ```json\r\n{"score": 82, "gaps": ["no retry on fetch"]}```  ',
], ids=["json-tag", "no-tag", "crlf-closing-on-json-line"])
def test_reply_that_is_one_fenced_json_block_is_assessed(tmp_path, text):
    judge = _Judge(text)
    result = _review(tmp_path, judge)
    assert result.metadata["assessment_status"] == "assessed"
    assert result.metadata["review_attempts"] == 1
    assert result.output["score"] == 82
    assert result.output["verdict"] == "go"
    assert "no retry on fetch" in result.output["gaps"]


_SNIPPET_REVIEW = json.dumps({"score": 72, "gaps": [
    "validate the body:\n```js\nif (!body.name) throw new Error('name required')\n```",
    "README code block is missing its closing ```",
]})


@pytest.mark.parametrize("text", [_SNIPPET_REVIEW, f"```json\n{_SNIPPET_REVIEW}\n```"],
                         ids=["strict", "fenced"])
def test_fence_inside_a_gap_string_stays_in_the_gap(tmp_path, text):
    judge = _Judge(text)
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 1
    assert result.metadata["assessment_status"] == "assessed"
    assert result.output["score"] == 72
    assert any("```js" in g for g in result.output["gaps"])


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


def test_mapping_gaps_are_coerced_to_file_issue_strings(tmp_path):
    judge = _Judge(json.dumps({"score": 66, "gaps": {
        "api.py": "no validation", "db.py": {"issue": "no migrations"},
    }}))
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 1
    assert result.metadata["assessment_status"] == "assessed"
    assert result.output["score"] == 66
    assert "api.py: no validation" in result.output["gaps"]
    assert "db.py: no migrations" in result.output["gaps"]


def test_valid_score_with_uncoercible_gaps_keeps_the_score_without_retry(tmp_path):
    # A clear rejection must not be re-sampled into a go just because its
    # gaps were unreadable.
    judge = _Judge('{"score": 30, "gaps": 7}', '{"score": 95, "gaps": []}')
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 1
    assert result.metadata["assessment_status"] == "assessed"
    assert result.metadata["review_attempts"] == 1
    assert result.output["score"] == 30
    assert result.output["verdict"] == "no_go"
    assert "reviewer gaps were unreadable" in result.output["gaps"]


_PER_FILE = '"files": [{"file": "ui.tsx", "score": 95, "gaps": []}]'


@pytest.mark.parametrize("text", [
    '{"score": 58, "gaps": ["auth missing"], ' + _PER_FILE + "}",
    '```json\n{"score": 58, "gaps": ["auth missing"], ' + _PER_FILE + "}\n```",
    '{"score": 58, "gaps": [{"quoted": {"score": 100, "gaps": []}}]}',
], ids=["strict", "fenced", "nested-in-gap"])
def test_nested_score_objects_never_replace_the_verdict_score(tmp_path, text):
    judge = _Judge(text)
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 1
    assert result.metadata["assessment_status"] == "assessed"
    assert result.output["score"] == 58
    assert result.output["verdict"] == "no_go"


_VERDICT = '{"score": 38, "gaps": ["create_user has no input validation"]}'


@pytest.mark.parametrize("text", [
    # Prose around an otherwise valid verdict.
    f"Here is my review:\n{_VERDICT}\nHope this helps.",
    f"Here is my review.\n```json\n{_VERDICT}\n```\nThanks.",
    # JSON quoted from the evidence or the app before the verdict.
    f'The evidence includes:\n```json\n{{"passed": true, "score": 100}}\n```\n{_VERDICT}',
    f'The fixture {{"score": 92, "gaps": []}} is seeded.\nVerdict: {_VERDICT}',
    f'The app ships:\n```json\n{{"score": 95, "gaps": []}}\n```\n```json\n{_VERDICT}\n```',
    # A per-file block, then a verdict that does not decode (unescaped quotes).
    'Per-file:\n```json\n{"file": "ui.tsx", "score": 95, "gaps": []}\n```\n'
    'Overall: {"score": 40, "gaps": ["the "admin" route has no auth"]}',
    # A per-file block, then a verdict cut off at the token cap.
    'ui.tsx:\n```json\n{"file": "ui.tsx", "score": 95, "gaps": []}\n```\n'
    'Overall:\n```json\n{"score": 40, "gaps": ["auth missing", "no te',
    # A cut-off or malformed verdict with per-file scores nested inside.
    '```json\n{"score": 40, "gaps": ["auth missing"], "files": [{"file": "ui.tsx", '
    '"score": 92, "gaps": []}, {"file": "api.py", "sco',
    'Verdict: {"score": 40, "gaps": ["auth is\nmissing"], ' + _PER_FILE + "}",
    'Verdict: {"score": 40, "gaps": ["the "admin}" route has no auth"], ' + _PER_FILE + "}",
    'Note {"debug: true} first.\nVerdict: {"score": 40, "gaps": ["stray } in parser.ts"], '
    + _PER_FILE + "}",
    # A stray ``` in prose or in a gap mis-pairs any fence scan.
    'Here is my review:\n```json\n{"score": 40, "gaps": ["README block is missing its '
    'closing ```", "api.py: no auth"], ' + _PER_FILE + "}\n```",
    "The README's ``` block never closes.\nVerdict: "
    '{"score": 40, "gaps": ["README: unterminated ``` block"], ' + _PER_FILE + "}",
], ids=["prose-inline", "prose-fenced", "quoted-evidence", "quoted-fixture",
        "fenced-fixture", "per-file-then-broken-verdict", "per-file-then-truncated",
        "truncated-nested", "raw-newline", "unescaped-quote-brace", "broken-prose-json",
        "stray-fence-in-gap", "stray-fence-in-prose"])
def test_anything_but_strict_or_whole_fenced_json_fails_closed(tmp_path, text):
    # Untrusted or nested JSON must never pick the score; retry, then unavailable.
    judge = _Judge(text)
    _assert_fails_closed(_review(tmp_path, judge), judge)


@pytest.mark.parametrize("text", [
    "x " + '{"a":' * 50_000,
    "{" * 250_000,
    '{"a": "' + '\\"{' * 80_000,
    '{"a": 1} ' * 27_000,
    "```" + "a" * 250_000,
    "```json\n" + " " * 250_000,
    "```json\n{" * 25_000,
    '```json\n{"score": 50, "gaps": []}\n```\n' * 6_000,
], ids=["unclosed-nesting", "open-braces", "escaped-quotes", "many-objects",
        "unclosed-fence-word", "unclosed-fence-space", "many-broken-fences", "many-fences"])
def test_pathological_reply_fails_closed_quickly(text):
    started = time.perf_counter()
    with pytest.raises((ValueError, RecursionError)):
        _parse_review(text)
    assert time.perf_counter() - started < 1.0


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
    _assert_fails_closed(result, judge)
    assert result.metadata["review_error"] == "JSONDecodeError"
    assert any(g.startswith("Semantic review unavailable") for g in result.output["gaps"])


def test_failed_provider_twice_stays_unavailable(tmp_path):
    judge = _Judge(LLMResult(text='{"score":95}', model="m", backend="openrouter", status="failed"))
    _assert_fails_closed(_review(tmp_path, judge), judge)


@pytest.mark.parametrize("text", [
    '{"score": true, "gaps": []}', '```json\n{"score": false}\n```', '[{"score": 90}]',
    '{"gaps": []}',
], ids=["bool", "fenced-bool", "list", "no-score"])
def test_invalid_review_objects_are_still_rejected(tmp_path, text):
    judge = _Judge(text)
    _assert_fails_closed(_review(tmp_path, judge), judge)


def test_slow_failure_is_not_retried(tmp_path, monkeypatch):
    """A slow failure (a CLI timeout, a long reasoning pass) is not retried: two
    of them could outlast the stage budget, and a timed-out reviewer stage falls
    back to the structural verdict. It fails closed after one attempt instead."""
    from skyn3t.agents import reviewer as reviewer_mod

    monkeypatch.setattr(reviewer_mod, "_REVIEW_RETRY_WITHIN_S", 0.0)
    judge = _Judge(
        LLMResult(text="", model="m", backend="openrouter", status="failed_cli_timeout"),
        '{"score": 95, "gaps": []}',
    )
    result = _review(tmp_path, judge)
    assert len(judge.calls) == 1
    assert result.metadata["assessment_status"] == "unavailable"
    assert result.metadata["review_attempts"] == 1
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
