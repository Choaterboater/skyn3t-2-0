# tests/test_distill_repo_skill.py
"""Distilled repo skills were hollow: 198 of 200 gh-*.md files in data/skills
were 0-byte husks (an old SkillLibrary wrote with the locale default codec —
cp1252 on Windows — and Path.write_text died on the non-encodable '★' AFTER
creating the file; the 2 survivors carried junk bodies: "consider this repo's
structure" + raw README HTML). _distill_repo_skill must now REFUSE to write
thin content (a failed distill reported in the handler result, not an applied
skill) and must extract CONCRETE, deterministic content: build/run commands +
layout/convention sections with badges/HTML/boilerplate stripped, wrapped in
frontmatter (name, description, stack-relevant tags, source = the GitHub URL)
that SkillLibrary can load back.
"""

from __future__ import annotations

import asyncio

import skyn3t.agents.github_fetch as gh
from skyn3t.cortex.handlers import HandlerRegistry
from skyn3t.cortex.proposal_store import Proposal, ProposalType
from skyn3t.intelligence.skill_library import SkillLibrary, content_sha256, parse_skill

_URL = "https://github.com/acme/coolcli"

# Mirrors fetch_github_repo_text's shape: header lines + "README:\n" + raw README
# (badges, HTML blocks, link-only rows, boilerplate sections included).
_RICH_TEXT = """GitHub repo: acme/coolcli

Description: A fast CLI for todo lists, with sync and plugins.

Language: Python · Stars: 2314

Topics: cli, todo, python, productivity

README:
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./dark.svg">
  <img alt="cover" src="./light.svg">
</picture>

<div align="center">
  <a href="https://example.com">Docs</a> |
  <a href="https://example.com/demo">Demo</a>
</div>

[![Build](https://img.shields.io/badge/build-passing-green.svg)](https://ci.example.com)

# CoolCLI

A fast CLI for todo lists, with sync and plugins.

## Installation

```bash
pip install coolcli
python -m coolcli --help
```

## Project layout

- `coolcli/cli.py` — argparse entrypoint, one command per function
- `coolcli/store.py` — JSONL persistence, atomic writes
- `tests/` — pytest mirrors the package layout

## License

MIT
"""


def _reg(skills: SkillLibrary) -> HandlerRegistry:
    return HandlerRegistry(rag=object(), skills=skills)


def _prop(payload: dict) -> Proposal:
    return Proposal(type=ProposalType.INGEST, title="ingest", payload=payload)


# -- refusal paths -----------------------------------------------------------
def test_empty_repo_text_writes_no_file_and_reports_failure(tmp_path):
    skills = SkillLibrary(tmp_path / "skills")
    slug, err = _reg(skills)._distill_repo_skill(_URL, "", {})
    assert slug is None
    assert err == "empty repo text"
    assert list((tmp_path / "skills").glob("*.md")) == []


def test_whitespace_only_repo_text_is_refused(tmp_path):
    skills = SkillLibrary(tmp_path / "skills")
    slug, err = _reg(skills)._distill_repo_skill(_URL, "  \n \n", {})
    assert slug is None and err


def test_thin_content_is_refused_and_no_file_written(tmp_path):
    # Header-only text (README fetch failed): the composed body is far under
    # the 400-char floor, so nothing durable may be written.
    skills = SkillLibrary(tmp_path / "skills")
    thin = "GitHub repo: acme/coolcli\n\nDescription: short.\n\nLanguage: Python · Stars: 3"
    slug, err = _reg(skills)._distill_repo_skill(_URL, thin, {})
    assert slug is None
    assert "too thin" in (err or "")
    assert list((tmp_path / "skills").glob("*.md")) == []
    assert skills.all() == []


def test_handler_reports_thin_distill_as_skill_error_not_skill(tmp_path, monkeypatch):
    class _FakeRag:
        def ingest_text(self, text, source="", kind="", metadata=None):
            return 3

    async def _fetch_thin(url):
        return gh.GitHubRepoEvidence(
            source_url=url,
            text="README text",
            source_path="README.md",
        )

    monkeypatch.setattr(gh, "fetch_github_repo_evidence", _fetch_thin)
    reg = HandlerRegistry(rag=_FakeRag(), skills=SkillLibrary(tmp_path / "skills"))
    res = asyncio.run(reg.apply(_prop({"url": _URL, "language": "Python"})))
    assert res["applied"] is True  # RAG ingest still applied
    assert res["ingested"] == 3
    assert "skill" not in res
    assert "too thin" in res["skill_error"]
    assert list((tmp_path / "skills").glob("*.md")) == []


def test_no_skills_library_returns_neutral(tmp_path):
    reg = HandlerRegistry(rag=object(), skills=None)
    assert reg._distill_repo_skill(_URL, _RICH_TEXT, {}) == (None, None)


# -- happy path --------------------------------------------------------------
def test_rich_readme_produces_loadable_skill_with_frontmatter(tmp_path):
    skills_dir = tmp_path / "skills"
    skills = SkillLibrary(skills_dir)
    slug, err = _reg(skills)._distill_repo_skill(_URL, _RICH_TEXT, {})
    assert err is None
    assert slug == "gh-acme-coolcli"

    path = skills_dir / f"{slug}.md"
    raw = path.read_text(encoding="utf-8")
    assert path.stat().st_size > 400
    # Frontmatter: name, description, stack-relevant tags, and a source kind.
    assert "name: gh-acme-coolcli" in raw
    assert "description:" in raw
    assert "source: github-distilled" in raw
    assert "stack: python" in raw

    sk = parse_skill(raw, fallback_slug=path.stem)
    assert "github-distilled" in sk.tags
    assert "external-candidate" in sk.tags
    assert "hygiene:quarantine" in sk.tags
    assert "python" in sk.tags  # language-detected stack tag
    assert "cli" in sk.tags  # topic tag
    assert sk.source == "github-distilled"
    assert sk.provenance is not None
    assert sk.provenance.source_url == _URL
    assert sk.provenance.source_path == "README"
    assert sk.provenance.content_hash == content_sha256(_RICH_TEXT)
    assert sk.provenance.pinned_revision is None  # direct helper never invents one
    # Concrete, deterministic body: real commands + layout sections.
    assert "pip install coolcli" in sk.body
    assert "python -m coolcli --help" in sk.body
    assert "Layout & conventions" in sk.body
    assert "coolcli/cli.py" in sk.body
    # Junk stripped: no HTML blocks, no badges, no link-only rows, no License.
    for junk in ("<picture", "<img", "<div", "shields.io", "</a>", "License"):
        assert junk not in sk.body
    # Loads back through SkillLibrary (non-empty body, not skipped).
    loaded = SkillLibrary(skills_dir).get(slug)
    assert loaded is not None
    assert loaded.body.strip()
    # External README text is retained for review but never default-injectable.
    assert not skills.relevant("python", limit=5)


def test_meta_falls_back_to_fetched_text_when_payload_is_sparse(tmp_path):
    skills = SkillLibrary(tmp_path / "skills")
    # No payload at all: description/language/stars/topics come from the text.
    slug, err = _reg(skills)._distill_repo_skill(_URL, _RICH_TEXT, {})
    assert err is None
    sk = skills.get(slug)
    assert sk is not None
    assert "2314★" in sk.body
    assert "A fast CLI for todo lists" in sk.description
    assert sk.stack == "python"  # Language: Python -> _LANG_STACK


def test_payload_overrides_fetched_meta(tmp_path):
    skills = SkillLibrary(tmp_path / "skills")
    payload = {"language": "Python", "stars": 99, "description": "Payload desc."}
    slug, err = _reg(skills)._distill_repo_skill(_URL, _RICH_TEXT, payload)
    assert err is None
    sk = skills.get(slug)
    assert "99★" in sk.body
    assert sk.description == "Payload desc."


def test_distill_failure_is_returned_not_raised(tmp_path):
    class _BoomSkills:
        def add(self, *a, **k):
            raise RuntimeError("disk full")

    reg = HandlerRegistry(rag=object(), skills=_BoomSkills())
    slug, err = reg._distill_repo_skill(_URL, _RICH_TEXT, {})
    assert slug is None
    assert "distillation failed" in (err or "")
