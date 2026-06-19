"""GitHub-repo learning: the Cortex INGEST handler ingests into RAG (opt-in)."""

from __future__ import annotations

import asyncio

import skyn3t.agents.github_fetch as gh
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import Proposal, ProposalType


def _prop(payload: dict) -> Proposal:
    return Proposal(type=ProposalType.INGEST, title="ingest", payload=payload)


class _FakeRag:
    def __init__(self, n: int = 3, raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self._n = n
        self._raises = raises

    def ingest_text(self, text, source="", kind=""):
        self.calls.append((text, source, kind))
        if self._raises:
            raise RuntimeError("boom")
        return self._n


async def _fetch_ok(url):  # noqa: ANN001
    return "README text"


async def _fetch_none(url):  # noqa: ANN001
    return None


def test_handler_ingests_into_rag(monkeypatch):
    rag = _FakeRag(n=3)
    reg = HandlerRegistry(rag=rag)
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/pallets/flask"})))
    assert res["applied"] is True
    assert res["ingested"] == 3
    assert rag.calls and rag.calls[0][2] == "github"
    assert "github.com/pallets/flask" in rag.calls[0][1]


def test_handler_synthesizes_url_from_repo_field(monkeypatch):
    rag = _FakeRag()
    reg = HandlerRegistry(rag=rag)
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({"repo": "pallets/flask"})))
    assert res["applied"] is True
    assert "github.com/pallets/flask" in rag.calls[0][1]


def test_handler_degrades_offline(monkeypatch):
    rag = _FakeRag()
    reg = HandlerRegistry(rag=rag)
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_none)
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/x/y"})))
    assert res["degraded"] is True
    assert res["ingested"] == 0
    assert not rag.calls  # never reached ingest


def test_handler_no_rag_is_unchanged():
    # Opt-in: with no RAG engine, behaviour is the old staging path verbatim.
    reg = HandlerRegistry()
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/x/y"})))
    assert res["applied"] is True
    assert res.get("staged") == "memory"
    assert "ingested" not in res


def test_handler_rag_error_degrades_not_failed(monkeypatch):
    rag = _FakeRag(raises=True)
    reg = HandlerRegistry(rag=rag)
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/x/y"})))
    # retryable-degraded, NOT applied:False (which would mark the proposal FAILED)
    assert res["applied"] is True
    assert res["degraded"] is True
    assert res["ingested"] == 0
    assert "error" in res


class _FakeSkills:
    def __init__(self):
        self.added = []

    def add(self, title, body, *, stack="generic", tags=None, source="manual", slug=None):
        self.added.append(
            {"title": title, "body": body, "stack": stack, "tags": tags, "source": source, "slug": slug}
        )
        return None


def test_handler_distills_skill_on_ingest(monkeypatch):
    rag = _FakeRag(n=2)
    skills = _FakeSkills()
    reg = HandlerRegistry(rag=rag, skills=skills)
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({
        "url": "https://github.com/pallets/flask",
        "description": "web framework",
        "language": "Python",
    })))
    assert res["applied"] is True
    assert res.get("skill")  # slug returned
    assert skills.added, "a skill should be distilled from the ingested repo"
    sk = skills.added[0]
    assert sk["source"] == "github-distilled"
    assert "flask" in sk["slug"]
    assert sk["stack"] == "python"  # mapped from language 'Python'
    assert "github-distilled" in sk["tags"]


def test_handler_no_skills_lib_still_ingests(monkeypatch):
    # Skills lib absent -> ingest still works, no skill, no error.
    rag = _FakeRag(n=1)
    reg = HandlerRegistry(rag=rag)  # no skills
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/x/y"})))
    assert res["applied"] is True
    assert "skill" not in res


def test_fetch_non_github_returns_none():
    assert asyncio.run(gh.fetch_github_repo_text("https://example.com/x/y")) is None


def test_repo_scout_rotates_topics():
    from skyn3t.cortex.repo_scout import RepoScout

    s = RepoScout()
    topics = [s._next_topic() for _ in range(5)]
    assert topics[0] != topics[1]  # consecutive scouts use different topics
    assert len(set(topics[:4])) == 4  # all four distinct before wrap
    assert topics[4] == topics[0]  # wraps around


def test_build_cortex_threads_rag():
    from skyn3t.core.events import EventBus
    from skyn3t.cortex.bootstrap import build_cortex

    sentinel = object()
    cx = build_cortex(EventBus(), rag=sentinel)
    assert cx.handlers.rag is sentinel
