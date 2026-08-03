"""GitHub-repo learning: the Cortex INGEST handler ingests into RAG (opt-in)."""

from __future__ import annotations

import asyncio
import base64
import sys
from types import SimpleNamespace

import skyn3t.agents.github_fetch as gh
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import Proposal, ProposalType
from skyn3t.intelligence.skill_library import SkillLibrary, content_sha256


def _prop(payload: dict) -> Proposal:
    return Proposal(type=ProposalType.INGEST, title="ingest", payload=payload)


class _FakeRag:
    def __init__(self, n: int = 3, raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self._n = n
        self._raises = raises

    def ingest_text(self, text, source="", kind="", metadata=None):
        self.calls.append((text, source, kind, metadata))
        if self._raises:
            raise RuntimeError("boom")
        return self._n


_PINNED_SHA = "a" * 40


def _evidence(
    url: str,
    text: str,
    *,
    pinned_revision: str | None = None,
    license: str | None = None,
    markdown_files: tuple[gh.GitHubMarkdownEvidence, ...] = (),
) -> gh.GitHubRepoEvidence:
    return gh.GitHubRepoEvidence(
        source_url=url,
        text=text,
        source_path="README.md",
        pinned_revision=pinned_revision,
        license=license,
        markdown_files=markdown_files,
    )


async def _fetch_ok(url):  # noqa: ANN001
    return _evidence(url, "README text")


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
    return _evidence(url, _RICH_TEXT, pinned_revision=_PINNED_SHA, license="MIT")


_DOC_TEXT = _RICH_TEXT.replace(
    "README:\n# Flask",
    "Markdown:\n# Testing and delivery patterns",
).replace(
    "## Installing",
    "## Repeatable verification",
)


async def _fetch_per_file(url):  # noqa: ANN001
    return _evidence(
        url,
        _RICH_TEXT,
        pinned_revision=_PINNED_SHA,
        license="MIT",
        markdown_files=(
            gh.GitHubMarkdownEvidence(source_path="README.md", text=_RICH_TEXT),
            gh.GitHubMarkdownEvidence(source_path="docs/testing.md", text=_DOC_TEXT),
        ),
    )


def test_handler_ingests_into_rag(monkeypatch):
    rag = _FakeRag(n=3)
    reg = HandlerRegistry(rag=rag)
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/pallets/flask"})))
    assert res["applied"] is True
    assert res["ingested"] == 3
    assert rag.calls and rag.calls[0][2] == "github"
    assert "github.com/pallets/flask" in rag.calls[0][1]
    assert rag.calls[0][3] == {
        "external_unreviewed": True,
        "source_kind": "github_readme",
        "source_url": "https://github.com/pallets/flask",
        "source_path": "README.md",
    }


def test_handler_synthesizes_url_from_repo_field(monkeypatch):
    rag = _FakeRag()
    reg = HandlerRegistry(rag=rag)
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({"repo": "pallets/flask"})))
    assert res["applied"] is True
    assert "github.com/pallets/flask" in rag.calls[0][1]


def test_handler_degrades_offline(monkeypatch):
    rag = _FakeRag()
    reg = HandlerRegistry(rag=rag)
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_none)
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/x/y"})))
    assert res["degraded"] is True
    assert res["ingested"] == 0
    assert not rag.calls  # never reached ingest


def test_handler_preserves_legacy_rag_ingest_signature(monkeypatch):
    class _LegacyRag:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def ingest_text(self, text, source="", kind=""):
            self.calls.append((text, source, kind))
            return 2

    rag = _LegacyRag()
    reg = HandlerRegistry(rag=rag)
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_ok)

    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/pallets/flask"})))

    assert res["applied"] is True
    assert res["ingested"] == 2
    assert rag.calls and rag.calls[0][2] == "github"


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
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/x/y"})))
    # retryable-degraded, NOT applied:False (which would mark the proposal FAILED)
    assert res["applied"] is True
    assert res["degraded"] is True
    assert res["ingested"] == 0
    assert "error" in res


class _FakeSkills:
    def __init__(self):
        self.added = []

    def add(
        self,
        title,
        body,
        *,
        stack="generic",
        tags=None,
        source="manual",
        slug=None,
        description="",
        provenance=None,
    ):
        self.added.append(
            {
                "title": title,
                "body": body,
                "stack": stack,
                "tags": tags,
                "source": source,
                "slug": slug,
                "description": description,
                "provenance": provenance,
            }
        )
        return None


def test_handler_distills_skill_on_ingest(monkeypatch):
    rag = _FakeRag(n=2)
    skills = _FakeSkills()
    reg = HandlerRegistry(rag=rag, skills=skills)
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_rich)
    res = asyncio.run(
        reg.apply(
            _prop(
                {
                    "url": "https://github.com/pallets/flask",
                    "description": "web framework",
                    "language": "Python",
                }
            )
        )
    )
    assert res["applied"] is True
    assert res.get("skill")  # slug returned
    assert skills.added, "a skill should be distilled from the ingested repo"
    sk = skills.added[0]
    assert sk["source"] == "github-distilled"
    assert "flask" in sk["slug"]
    assert sk["stack"] == "python"  # mapped from language 'Python'
    assert "github-distilled" in sk["tags"]
    assert "external-candidate" in sk["tags"]
    assert "hygiene:quarantine" in sk["tags"]
    assert sk["provenance"] is not None
    assert sk["provenance"].source_url == "https://github.com/pallets/flask"
    assert sk["provenance"].source_path == "README.md"
    assert sk["provenance"].content_hash == content_sha256(_RICH_TEXT)
    assert sk["provenance"].pinned_revision == _PINNED_SHA
    assert sk["provenance"].license == "MIT"


def test_handler_distills_one_quarantined_skill_per_markdown_file(tmp_path, monkeypatch):
    rag = _FakeRag(n=2)
    skills = SkillLibrary(tmp_path / "skills")
    reg = HandlerRegistry(rag=rag, skills=skills)
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_per_file)

    res = asyncio.run(
        reg.apply(_prop({"url": "https://github.com/pallets/flask", "language": "Python"}))
    )

    assert res["applied"] is True
    assert res["ingested"] == 4  # README + one independently attributed document
    assert res["skill_count"] == 2
    assert len(res["skills"]) == 2
    assert rag.calls[1][1] == (
        f"https://github.com/pallets/flask/blob/{_PINNED_SHA}/docs/testing.md"
    )
    assert rag.calls[1][3]["source_kind"] == "github_markdown"
    distilled = [skills.get(slug) for slug in res["skills"]]
    assert {skill.provenance.source_path for skill in distilled if skill is not None} == {
        "README.md",
        "docs/testing.md",
    }
    assert all(skill is not None and "hygiene:quarantine" in skill.tags for skill in distilled)
    assert res["skills"][0] == "gh-pallets-flask"
    assert res["skills"][1].startswith("gh-pallets-flask-testing-")


def test_handler_refuses_thin_distill_and_reports_skill_error(monkeypatch):
    # A junk/thin fetch ("README text") must NOT write a hollow skill file:
    # the handler reports a failed distill instead of an applied skill.
    rag = _FakeRag(n=2)
    skills = _FakeSkills()
    reg = HandlerRegistry(rag=rag, skills=skills)
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_ok)
    res = asyncio.run(
        reg.apply(
            _prop(
                {
                    "url": "https://github.com/pallets/flask",
                    "language": "Python",
                }
            )
        )
    )
    assert res["applied"] is True  # RAG ingest still applied
    assert "skill" not in res
    assert "too thin" in res["skill_error"]
    assert not skills.added


def test_handler_persists_unicode_github_skill_as_nonempty_utf8(tmp_path, monkeypatch):
    rag = _FakeRag(n=1)
    skills = SkillLibrary(tmp_path / "skills")
    reg = HandlerRegistry(rag=rag, skills=skills)
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_rich)

    res = asyncio.run(
        reg.apply(
            _prop(
                {
                    "url": "https://github.com/pallets/flask",
                    "description": "web framework",
                    "language": "Python",
                    "stars": 123,
                }
            )
        )
    )

    path = tmp_path / "skills" / f"{res['skill']}.md"
    assert path.stat().st_size > 0
    assert "123★" in path.read_text(encoding="utf-8")
    assert SkillLibrary(tmp_path / "skills").get(res["skill"]) is not None


def test_handler_no_skills_lib_still_ingests(monkeypatch):
    # Skills lib absent -> ingest still works, no skill, no error.
    rag = _FakeRag(n=1)
    reg = HandlerRegistry(rag=rag)  # no skills
    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_ok)
    res = asyncio.run(reg.apply(_prop({"url": "https://github.com/x/y"})))
    assert res["applied"] is True
    assert "skill" not in res


class _FetchResponse:
    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FetchClient:
    responses: list[_FetchResponse] = []
    calls: list[tuple[str, dict[str, str]]] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]):
        self.calls.append((url, headers))
        return self.responses.pop(0)


def test_fetch_evidence_records_only_github_supplied_commit_and_license(monkeypatch):
    encoded = base64.b64encode(b"# Example\nUse a proof gate.\n").decode("ascii")
    _FetchClient.calls = []
    _FetchClient.responses = [
        _FetchResponse(
            200,
            {
                "description": "An example.",
                "language": "Python",
                "stargazers_count": 7,
                "topics": ["quality"],
                "default_branch": "main",
                "license": {"spdx_id": "MIT"},
            },
        ),
        _FetchResponse(200, {"sha": _PINNED_SHA}),
        _FetchResponse(
            200,
            {"path": "docs/README.md", "encoding": "base64", "content": encoded},
        ),
        _FetchResponse(200, {"tree": []}),
    ]
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=_FetchClient))

    evidence = asyncio.run(gh.fetch_github_repo_evidence("https://github.com/acme/example"))

    assert evidence is not None
    assert evidence.source_url == "https://github.com/acme/example"
    assert evidence.source_path == "docs/README.md"
    assert evidence.pinned_revision == _PINNED_SHA
    assert evidence.license == "MIT"
    assert "Revision: " + _PINNED_SHA in evidence.text
    assert "Source path: docs/README.md" in evidence.text
    assert len(_FetchClient.calls) == 4


def test_fetch_evidence_collects_only_bounded_safe_markdown_at_the_pinned_revision(monkeypatch):
    readme = base64.b64encode(b"# Example\nUse a proof gate.\n").decode("ascii")
    guide = base64.b64encode(
        b"# Verification guide\n\nRun the proof suite before promotion.\n"
    ).decode("ascii")
    _FetchClient.calls = []
    _FetchClient.responses = [
        _FetchResponse(200, {"default_branch": "main"}),
        _FetchResponse(200, {"sha": _PINNED_SHA}),
        _FetchResponse(200, {"path": "README.md", "encoding": "base64", "content": readme}),
        _FetchResponse(
            200,
            {
                "tree": [
                    {"path": "README.md", "type": "blob", "size": 20},
                    {"path": "docs/guide.md", "type": "blob", "size": 100},
                    {"path": "../outside.md", "type": "blob", "size": 100},
                    {"path": "docs/large.md", "type": "blob", "size": 99_999},
                    {"path": "src/main.py", "type": "blob", "size": 100},
                ]
            },
        ),
        _FetchResponse(
            200,
            {"path": "docs/guide.md", "encoding": "base64", "content": guide},
        ),
    ]
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=_FetchClient))

    evidence = asyncio.run(gh.fetch_github_repo_evidence("https://github.com/acme/example"))

    assert evidence is not None
    assert [item.source_path for item in evidence.markdown_files] == ["README.md", "docs/guide.md"]
    assert "Source path: docs/guide.md" in evidence.markdown_files[1].text
    assert _FetchClient.calls[-1][0].endswith(f"contents/docs/guide.md?ref={_PINNED_SHA}")
    assert len(_FetchClient.calls) == 5


def test_fetch_evidence_never_uses_a_branch_name_as_a_pin(monkeypatch):
    encoded = base64.b64encode(b"# Example\n").decode("ascii")
    _FetchClient.calls = []
    _FetchClient.responses = [
        _FetchResponse(200, {"default_branch": "main", "license": {"spdx_id": "NOASSERTION"}}),
        _FetchResponse(200, {"sha": "main"}),
        _FetchResponse(200, {"path": "README.md", "encoding": "base64", "content": encoded}),
    ]
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=_FetchClient))

    evidence = asyncio.run(gh.fetch_github_repo_evidence("https://github.com/acme/example"))

    assert evidence is not None
    assert evidence.pinned_revision is None
    assert evidence.license is None
    assert "Default branch: main" in evidence.text
    assert "Revision:" not in evidence.text


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
