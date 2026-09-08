"""The committed skill snapshot must work without the operator's local caches."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.intelligence.skill_library import SkillLibrary, seed_default_skills
from skyn3t.studio.runner import StudioRunner

_SOURCE = Path(__file__).resolve().parents[1] / "data" / "skills"
_NEW_SKILL_CASES = [
    ("mcp-implementation-and-evaluation", "mcp", "MCP protocol schemas tool listing pagination evaluation"),
    ("postgres-query-and-schema-engineering", "fastapi", "PostgreSQL query plans indexes connection pool migration"),
    ("expo-native-interface", "react_native", "Expo native interface keyboard safe areas accessibility"),
    ("browser-proof-with-playwright", "react", "Playwright browser workflow locators assertions traces"),
    ("expo-native-networking", "expo", "Expo networking offline cancellation retries secure storage"),
    ("nextjs-framework-browser-proof", "nextjs", "Next.js Turbopack framework introspection agent-browser /_next/mcp"),
    ("rag-retrieval-and-grounding-evaluation", "rag", "RAG retrieval grounding recall chunks multi-hop abstention"),
    ("swift-async-state-and-test-isolation", "swift_ios", "Swift Testing async callbacks actor isolation XCTest"),
]


@pytest.fixture
def snapshot(tmp_path):
    manifest = json.loads((_SOURCE / ".distribution.json").read_text(encoding="utf-8"))
    destination = tmp_path / "skills"
    destination.mkdir()
    for relative, entry in manifest["files"].items():
        source = _SOURCE / relative
        assert source.resolve().is_relative_to(_SOURCE.resolve())
        assert not source.is_symlink()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"]
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return SkillLibrary(destination), manifest


def test_snapshot_retains_review_decisions_without_runtime_state(snapshot):
    library, manifest = snapshot
    skills = library.all()
    held = [skill for skill in skills if "hygiene:quarantine" in skill.tags]
    assert manifest["schema_version"] == 1
    assert len(skills) == 137
    assert len(held) == 37
    assert seed_default_skills(library) == 0
    assert library.get("delivered-empty") is None
    assert library.is_retired("delivered-empty")
    assert not any(library.can_promote_external(skill.slug) for skill in skills)
    assert all("review-held" in skill.tags for skill in held)
    assert ".skill_scores.json" not in manifest["files"]
    assert ".skill_hub_imports.json" not in manifest["files"]
    assert not any("skill-maintenance" in path for path in manifest["files"])


def test_snapshot_has_consistent_evidence_or_explicit_local_only_receipts(snapshot):
    library, manifest = snapshot
    local_only = manifest["local_only_evidence"]
    for skill in library.all():
        if skill.provenance is None:
            continue
        index_path = skill.provenance.metadata["skyn3t-evidence-index"]
        index = json.loads((library.dir / index_path).read_text(encoding="utf-8"))
        assert index["advisory_sha256"] == hashlib.sha256(skill.body.encode()).hexdigest()
        for source in index["sources"]:
            entries = [source]
            if source.get("license_receipt"):
                entries.append(source["license_receipt"])
            for entry in entries:
                relative = entry["evidence_path"]
                if relative in manifest["files"]:
                    assert manifest["files"][relative]["sha256"] == entry["sha256"]
                else:
                    assert local_only[relative]["sha256"] == entry["sha256"]
                    assert local_only[relative]["reason"]
                    assert not (library.dir / relative).exists()
        if skill.source == "github-distilled" and "hygiene:quarantine" not in skill.tags:
            primary = skill.provenance.evidence_path
            if primary in manifest["files"]:
                assert library._retained_evidence_matches(skill)
            else:
                assert skill.slug == "gh-ericjmarti-inventory-hunter"
                assert local_only[primary]["reason"] == (
                    "credential-shaped-documentation-example-blocked-by-push-protection"
                )
                assert not library._retained_evidence_matches(skill)


def test_runtime_grading_does_not_rewrite_committed_advice(snapshot):
    library, _ = snapshot
    skill = library.get("mcp-implementation-and-evaluation")
    assert skill
    path = library.dir / f"{skill.slug}.md"
    before = path.read_bytes()
    previous_uses = skill.uses
    library.record_use(skill.slug, helpful=True, quality=0.9)
    assert skill.uses == previous_uses + 1
    assert path.read_bytes() == before
    assert (library.dir / ".skill_scores.json").is_file()


def test_snapshot_does_not_publish_literal_slack_webhooks(snapshot):
    library, manifest = snapshot
    pattern = re.compile(
        rb"https://hooks\.slack(?:-gov)?\.com/services/[^\s\"'<>`]+",
        re.IGNORECASE,
    )
    for relative in manifest["files"]:
        if pattern.search((library.dir / relative).read_bytes()):
            pytest.fail(f"Webhook-shaped URL in {relative}; retain the raw document locally.")


def test_new_advisories_and_hermes_merge_survive_a_cache_free_checkout(snapshot, tmp_path):
    library, _ = snapshot
    bus = EventBus()
    runner = StudioRunner(
        bus, Orchestrator(bus),
        settings=Settings(llm_backend="stub", data_dir=tmp_path / "runtime"),
        skills=library,
    )
    for slug, stack, brief in _NEW_SKILL_CASES:
        advice, selected = runner._skill_advice(stack, brief)
        skill = library.get(slug)
        assert skill and slug in selected
        assert skill.body in advice
    advice, selected = runner._skill_advice(
        "fastapi",
        "HTTP GraphQL response semantics errors despite HTTP 200 content type fixture assertions",
    )
    assert "api-and-interface-design" in selected
    assert "HTTP response-semantics proof branch:" in advice
    assert "bodyless contract such as HEAD or 204" in advice
