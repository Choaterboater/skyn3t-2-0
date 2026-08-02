"""GitHub-repo learning: the Cortex INGEST handler ingests into RAG (opt-in)."""

from __future__ import annotations

import asyncio

import skyn3t.agents.github_fetch as gh
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import Proposal, ProposalType
from skyn3t.intelligence.skill_library import SkillLibrary


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


_RICH_TEXT = (
    "GitHub repo: pallets/flask\n\n"
    "Description: The Python micro framework for building web applications.\n\n"
    "Language: Python · Stars: 67000\n\n"
    "Topics: flask, python, web\n\n"
    "README:\n"
    "# Flask\n\n"
    "Flask is a lightweight WSGI web application framework designed to make\n"
    "getting started quick and easy, with the ability to scale up to complex\n"
    "applications. It began as a simple wrapper around Werkzeug and Jinja and\n"
    "has become one of the most popular Python web frameworks.\n\n"
    "## Installing\n\n"
    "```bash\n"
    "pip install Flask\n"
    "python -m flask --app hello run\n"
    "```\n\n"
    "## Layout\n\n"
    "- `src/flask/app.py` — the Flask application object and dispatch\n"
    "- `src/flask/blueprints.py` — blueprint registration and nesting\n"
    "- `tests/` — pytest suite mirroring the package structure\n"
)


async def _fetch_rich(url):  # noqa: ANN001
    return _RICH_TEXT


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

    def add(self, title, body, *, stack="generic", tags=None, source="manual", slug=None, description=""):
        self.added.append(
            {"title": title, "body": body, "stack": stack, "tags": tags, "source": source, "slug": slug, "description": description}
        )
        return None


def test_handler_distills_skill_on_ingest(monkeypatch):
    rag = _FakeRag(n=2)
    skills = _FakeSkills()
    reg = HandlerRegistry(rag=rag, skills=skills)
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_rich)
    res = asyncio.run(reg.apply(_prop({
        "url": "https://github.com/pallets/flask",
        "description": "web framework",
        "language": "Python",
    })))
    assert res["applied"] is True
    assert res.get("skill")  # slug returned
    assert skills.added, "a skill should be distilled from the ingested repo"
    sk = skills.added[0]
    assert sk["source"] == "https://github.com/pallets/flask"  # frontmatter source = repo URL
    assert "flask" in sk["slug"]
    assert sk["stack"] == "python"  # mapped from language 'Python'
    assert "github-distilled" in sk["tags"]


def test_handler_refuses_thin_distill_and_reports_skill_error(monkeypatch):
    # A junk/thin fetch ("README text") must NOT write a hollow skill file:
    # the handler reports a failed distill instead of an applied skill.
    rag = _FakeRag(n=2)
    skills = _FakeSkills()
    reg = HandlerRegistry(rag=rag, skills=skills)
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({
        "url": "https://github.com/pallets/flask",
        "language": "Python",
    })))
    assert res["applied"] is True  # RAG ingest still applied
    assert "skill" not in res
    assert "too thin" in res["skill_error"]
    assert not skills.added


def test_handler_persists_unicode_github_skill_as_nonempty_utf8(tmp_path, monkeypatch):
    rag = _FakeRag(n=1)
    skills = SkillLibrary(tmp_path / "skills")
    reg = HandlerRegistry(rag=rag, skills=skills)
    monkeypatch.setattr(gh, "fetch_github_repo_text", _fetch_rich)

    res = asyncio.run(reg.apply(_prop({
        "url": "https://github.com/pallets/flask",
        "description": "web framework",
        "language": "Python",
        "stars": 123,
    })))

    path = tmp_path / "skills" / f"{res['skill']}.md"
    assert path.stat().st_size > 0
    assert "123★" in path.read_text(encoding="utf-8")
    assert SkillLibrary(tmp_path / "skills").get(res["skill"]) is not None


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
    from skyn3t.cortex.repo_scout import _SCOUT_TOPICS, RepoScout

    s = RepoScout()
    n = len(_SCOUT_TOPICS)
    topics = [s._next_topic() for _ in range(n + 1)]
    assert topics[0] != topics[1]  # consecutive scouts use different topics
    assert len(set(topics[:n])) == n  # all distinct within one cycle
    assert topics[n] == topics[0]  # wraps around after a full cycle


def test_build_cortex_threads_rag():
    from skyn3t.core.events import EventBus
    from skyn3t.cortex.bootstrap import build_cortex

    sentinel = object()
    cx = build_cortex(EventBus(), rag=sentinel)
    assert cx.handlers.rag is sentinel
