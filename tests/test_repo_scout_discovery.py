"""RepoScout must keep discovering NEW repos, not re-find the same handful.

Root cause of "scout finds nothing anymore": the discovery space was 4 fixed
topics x top-5-by-stars = 20 repos. Once those are proposed, dedupe rejects
every repeat, so no new proposals ever surface. The fix is pagination (advance
the page per topic each scout) plus a broader topic pool.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from skyn3t.cortex.repo_scout import _SCOUT_TOPICS, RepoScout

_VALID_REPO = {
    "full_name": "owner/repo",
    "html_url": "https://github.com/owner/repo",
    "stargazers_count": 100,
    "description": None,
    "language": None,
}


def _github_response(monkeypatch, payload):
    client_type = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, content=json.dumps(payload), headers={"Content-Type": "application/json"},
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "skyn3t.cortex.repo_scout.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    return requests


def test_page_advances_per_topic():
    s = RepoScout()
    # Repeated scouts of the SAME topic walk forward through result pages.
    assert [s._next_page("python cli tool") for _ in range(3)] == [1, 2, 3]


def test_pages_are_independent_per_topic():
    s = RepoScout()
    s._next_page("a")
    s._next_page("a")
    assert s._next_page("a") == 3
    assert s._next_page("b") == 1  # a different topic starts fresh


def test_topic_pool_is_broad():
    # A tiny fixed pool exhausts immediately; the pool must be substantially
    # larger so rotation alone surfaces varied repos.
    assert len(_SCOUT_TOPICS) >= 12
    assert len(set(_SCOUT_TOPICS)) == len(_SCOUT_TOPICS)  # no duplicates


def test_topics_rotate_distinctly_and_wrap():
    s = RepoScout()
    n = len(_SCOUT_TOPICS)
    topics = [s._next_topic() for _ in range(n + 1)]
    assert topics[0] != topics[1]
    assert len(set(topics[:n])) == n   # all distinct within one cycle
    assert topics[n] == topics[0]      # wraps after a full cycle


async def test_repo_scout_truthful_receipt_live_success(monkeypatch, tmp_path):
    s = RepoScout(settings=pytest.importorskip("types").SimpleNamespace(data_dir=str(tmp_path)))

    async def _mock_page(topic, page=1):
        return [{"full_name": "owner/repo", "html_url": "https://github.com/owner/repo"}], 100

    monkeypatch.setattr(s, "_search_github_page", _mock_page)

    repos, receipt = await s.search_with_receipt("python cli tool")
    assert receipt.source == "github"
    assert receipt.outcome == "live"
    assert receipt.reason == "ok"
    assert receipt.page == 1
    assert receipt.items_count == 1
    assert receipt.total_count == 100
    assert repos[0]["full_name"] == "owner/repo"
    # Page cursor advanced after valid live response
    assert s._peek_page("python cli tool") == 2


async def test_repo_scout_truthful_receipt_degraded_fallback_403(monkeypatch, tmp_path):
    s = RepoScout(settings=pytest.importorskip("types").SimpleNamespace(data_dir=str(tmp_path)))

    async def _mock_page(topic, page=1):
        req = httpx.Request("GET", "https://api.github.com/search")
        resp = httpx.Response(403, request=req)
        raise httpx.HTTPStatusError("Forbidden", request=req, response=resp)

    monkeypatch.setattr(s, "_search_github_page", _mock_page)

    repos, receipt = await s.search_with_receipt("python cli tool")
    assert receipt.source == "offline_seed"
    assert receipt.outcome == "degraded"
    assert receipt.reason == "http_403"
    assert receipt.page == 1
    assert receipt.items_count > 0
    # Page cursor preserved on error
    assert s._peek_page("python cli tool") == 1


async def test_repo_scout_truthful_receipt_degraded_fallback_429(monkeypatch, tmp_path):
    s = RepoScout(settings=pytest.importorskip("types").SimpleNamespace(data_dir=str(tmp_path)))

    async def _mock_page(topic, page=1):
        req = httpx.Request("GET", "https://api.github.com/search")
        resp = httpx.Response(429, request=req)
        raise httpx.HTTPStatusError("Rate limit exceeded", request=req, response=resp)

    monkeypatch.setattr(s, "_search_github_page", _mock_page)

    _repos, receipt = await s.search_with_receipt("python cli tool")
    assert receipt.source == "offline_seed"
    assert receipt.outcome == "degraded"
    assert receipt.reason == "http_429"
    assert s._peek_page("python cli tool") == 1


async def test_repo_scout_truthful_receipt_degraded_malformed_json(monkeypatch, tmp_path):
    s = RepoScout(settings=pytest.importorskip("types").SimpleNamespace(data_dir=str(tmp_path)))

    async def _mock_page(topic, page=1):
        raise ValueError("malformed_json")

    monkeypatch.setattr(s, "_search_github_page", _mock_page)

    _repos, receipt = await s.search_with_receipt("python cli tool")
    assert receipt.source == "offline_seed"
    assert receipt.outcome == "degraded"
    assert receipt.reason == "malformed_json"
    assert s._peek_page("python cli tool") == 1


async def test_repo_scout_truthful_receipt_degraded_malformed_items(monkeypatch, tmp_path):
    s = RepoScout(settings=pytest.importorskip("types").SimpleNamespace(data_dir=str(tmp_path)))

    async def _mock_page(topic, page=1):
        raise ValueError("malformed_items")

    monkeypatch.setattr(s, "_search_github_page", _mock_page)

    _repos, receipt = await s.search_with_receipt("python cli tool")
    assert receipt.source == "offline_seed"
    assert receipt.outcome == "degraded"
    assert receipt.reason == "malformed_items"
    assert s._peek_page("python cli tool") == 1


async def test_repo_scout_valid_zero_results_advances_and_does_not_degrade(monkeypatch, tmp_path):
    s = RepoScout(settings=pytest.importorskip("types").SimpleNamespace(data_dir=str(tmp_path)))

    async def _mock_page(topic, page=1):
        # A search query that explicitly excludes forks and returns 0 items
        return [], 0

    monkeypatch.setattr(s, "_search_github_page", _mock_page)

    repos, receipt = await s.search_with_receipt("bolt.diy in:name fork:false")
    assert repos == []
    assert receipt.source == "github"
    assert receipt.outcome == "live"
    assert receipt.reason == "ok"
    assert receipt.items_count == 0
    assert receipt.total_count == 0
    # Page cursor MUST advance for valid live zero results
    assert s._peek_page("bolt.diy in:name fork:false") == 2


async def test_repo_scout_cancellation_preserves_page_cursor(monkeypatch, tmp_path):
    s = RepoScout(settings=pytest.importorskip("types").SimpleNamespace(data_dir=str(tmp_path)))

    async def _mock_page(topic, page=1):
        raise asyncio.CancelledError()

    monkeypatch.setattr(s, "_search_github_page", _mock_page)

    with pytest.raises(asyncio.CancelledError):
        await s.search_with_receipt("python cli tool")
    assert s._peek_page("python cli tool") == 1


async def test_repo_scout_same_topic_concurrent_requests_serialized(monkeypatch, tmp_path):
    s = RepoScout(settings=pytest.importorskip("types").SimpleNamespace(data_dir=str(tmp_path)))
    order = []

    async def _mock_page(topic, page=1):
        order.append(f"start-{page}")
        await asyncio.sleep(0.01)
        order.append(f"end-{page}")
        return [{"full_name": f"owner/repo{page}"}], 10

    monkeypatch.setattr(s, "_search_github_page", _mock_page)

    res1, res2 = await asyncio.gather(
        s.search_with_receipt("python cli tool"),
        s.search_with_receipt("python cli tool"),
    )

    assert order == ["start-1", "end-1", "start-2", "end-2"]
    assert res1[1].page == 1
    assert res2[1].page == 2
    assert s._peek_page("python cli tool") == 3


@pytest.mark.parametrize("bad_first", [True, False], ids=["selected", "beyond-limit"])
@pytest.mark.parametrize("record", [
    {},
    {**_VALID_REPO, "full_name": "unknown"},
    {**_VALID_REPO, "full_name": "owner/../repo"},
    {**_VALID_REPO, "full_name": ["owner", "repo"]},
    {**_VALID_REPO, "stargazers_count": "not-a-number"},
    {**_VALID_REPO, "stargazers_count": []},
    {**_VALID_REPO, "stargazers_count": -1},
    {**_VALID_REPO, "stargazers_count": True},
    {**_VALID_REPO, "stargazers_count": float("inf")},
    {**_VALID_REPO, "description": {"text": "not a string"}},
    {**_VALID_REPO, "language": ["Python"]},
    {**_VALID_REPO, "html_url": 42},
    {**_VALID_REPO, "html_url": "https://example.com/owner/repo"},
])
async def test_malformed_repository_http_records_never_consume_a_page(monkeypatch, record, bad_first):
    scout = RepoScout(max_results=1)
    topic = "malformed repository response"
    scout._next_page(topic)
    persisted = scout._state_path.read_bytes()
    requests = _github_response(
        monkeypatch, {
            "items": [record, dict(_VALID_REPO)] if bad_first else [dict(_VALID_REPO), record],
            "total_count": 2,
        },
    )

    proposals, receipt = await scout.scout_with_receipt(topic)

    assert len(requests) == 1
    assert receipt.outcome == "degraded"
    assert receipt.source == "offline_seed"
    assert receipt.reason == "malformed_items"
    assert all(proposal.payload["offline"] for proposal in proposals)
    assert scout._peek_page(topic) == 2
    assert scout._state_path.read_bytes() == persisted


@pytest.mark.parametrize("count", [-1, True, "many", float("inf")])
async def test_malformed_total_count_preserves_cursor(monkeypatch, count):
    scout = RepoScout()
    _github_response(monkeypatch, {"items": [dict(_VALID_REPO)], "total_count": count})

    _proposals, receipt = await scout.scout_with_receipt("invalid total count")

    assert receipt.outcome == "degraded"
    assert scout._peek_page("invalid total count") == 1
    assert not scout._state_path.exists()


async def test_large_valid_star_count_cannot_overflow_scoring(monkeypatch):
    scout = RepoScout()
    _github_response(monkeypatch, {
        "items": [{**_VALID_REPO, "stargazers_count": 10 ** 400}],
        "total_count": 1,
    })

    proposals, receipt = await scout.scout_with_receipt("large star count")

    assert receipt.outcome == "live"
    assert proposals[0].confidence == 0.8
    assert scout._peek_page("large star count") == 2


async def test_persistence_failure_is_exposed_without_mislabeling_live_data(monkeypatch):
    scout = RepoScout()
    _github_response(monkeypatch, {"items": [dict(_VALID_REPO)], "total_count": 1})
    logger = Mock()
    monkeypatch.setattr("skyn3t.cortex.repo_scout.log", logger)

    def fail_write(*args, **kwargs):
        raise OSError("injected storage failure")

    monkeypatch.setattr("skyn3t.atomic_io.atomic_write_text", fail_write)

    proposals, receipt = await scout.scout_with_receipt("storage unavailable")

    assert receipt.source == "github"
    assert receipt.outcome == "live"
    assert receipt.reason == "cursor_persist_failed"
    assert proposals[0].payload["offline"] is False
    assert scout._peek_page("storage unavailable") == 2
    assert not scout._state_path.exists()
    logger.warning.assert_called_once()
