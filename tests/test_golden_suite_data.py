"""Locks the packaged Golden Suite's coverage and deterministic contracts."""

from __future__ import annotations

import json
import math
import re
from importlib import resources
from pathlib import Path, PurePosixPath

from skyn3t.agents._common import _normalize_stack
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.studio.security_check import _WEB_STACKS as SECURITY_STACKS
from skyn3t.studio.seo_check import _SEO_WEB_STACKS as SEO_STACKS
from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS

ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "skyn3t" / "benchmarks" / "golden-v1.json"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
TOP_KEYS = {"schema_version", "suite_id", "name", "description", "cases"}
CASE_KEYS = {"id", "brief", "stack", "tags", "expectations"}
EXPECTATION_KEYS = {
    "expected_stack",
    "min_score",
    "min_intent_score",
    "required_gates",
    "required_artifacts",
}
KNOWN_GATES = {
    "proof",
    "security_check",
    "headless_gate",
    "game_visual",
    "qa_playtest",
    "seo",
    "mcp_check",
    "rag_check",
    "workflow_check",
    "cli_check",
    "cli_playtest",
    "deploy_check",
}
EXPECTED_CASE_IDS = [
    "weather-react",
    "habit-expo",
    "editorial-nextjs",
    "sdk-docs-astro",
    "outdoor-store-remix",
    "support-ops-vue",
    "billing-portal-sveltekit",
    "inventory-react-ts",
    "notes-fastapi",
    "restaurant-static",
    "csv-cleaner-python",
    "shortener-express",
    "markdown-notes-tauri",
    "parcel-platformer-phaser",
    "focus-timer-swift",
    "sqlite-tools-mcp",
    "document-chat-rag",
    "webhook-digest-workflow",
    "marketing-agent-pack",
    "lamp-configurator-threejs",
    "museum-threejs-static",
    "ohlcv-market-data-api",
    "paper-trading-fastapi",
    "model-router-fastapi",
    "release-copilot-python",
    "supabase-auth-nextjs",
    "local-tasks-nextjs",
    "memory-chat-rag",
    "dino-runner-phaser",
    "security-agent-pack",
]


def _suite() -> dict:
    return json.loads(SUITE_PATH.read_text(encoding="utf-8"))


def _expected_deterministic_gates(case: dict) -> set[str]:
    stack = case["stack"]
    artifacts = set(case["expectations"]["required_artifacts"])
    expected = {"proof"}
    if stack in SECURITY_STACKS:
        expected.add("security_check")
    if stack in SEO_STACKS:
        expected.add("seo")
    if stack == "phaser":
        expected.add("headless_gate")
    if stack == "mcp":
        expected.add("mcp_check")
    if stack == "rag":
        expected.add("rag_check")
    if stack == "workflow":
        expected.add("workflow_check")
    if stack == "python":
        expected.add("cli_check")
    if ".skyn3t-cli-playtest.json" in artifacts:
        expected.add("cli_playtest")
    return expected


def test_suite_is_packaged_and_has_the_exact_v1_shape():
    packaged = resources.files("skyn3t.benchmarks").joinpath("golden-v1.json")
    assert packaged.is_file()

    suite = _suite()
    assert set(suite) == TOP_KEYS
    assert suite["schema_version"] == 1
    assert suite["suite_id"] == "golden-v1"
    assert SLUG_RE.fullmatch(suite["suite_id"])
    assert suite["name"].strip()
    assert suite["description"].strip()
    assert len(suite["cases"]) == 30
    assert [case["id"] for case in suite["cases"]] == EXPECTED_CASE_IDS


def test_cases_follow_the_strict_schema_and_quality_floor():
    cases = _suite()["cases"]
    ids: list[str] = []
    briefs: list[str] = []
    for case in cases:
        assert set(case) == CASE_KEYS, case.get("id")
        case_id = case["id"]
        assert isinstance(case_id, str) and len(case_id) <= 64
        assert SLUG_RE.fullmatch(case_id), case_id
        ids.append(case_id)

        brief = case["brief"]
        assert isinstance(brief, str) and 10 <= len(brief) <= 2000
        assert len(brief.split()) >= 15, f"{case_id}: brief is too vague"
        assert brief.startswith("Build "), f"{case_id}: brief must be an actionable build request"
        assert not re.search(r"\b(?:something|anything|whatever|simple|basic)\b", brief, re.I)
        briefs.append(brief.casefold())

        stack = case["stack"]
        assert stack in REAL_BUILDER_STACKS, (case_id, stack)
        tags = case["tags"]
        assert isinstance(tags, list) and 2 <= len(tags) <= 12
        assert len(tags) == len(set(tags))
        assert all(isinstance(tag, str) and len(tag) <= 32 and SLUG_RE.fullmatch(tag)
                   for tag in tags)

        expectations = case["expectations"]
        assert set(expectations) == EXPECTATION_KEYS, case_id
        assert expectations["expected_stack"] == stack
        assert expectations["min_score"] == 60
        assert expectations["min_intent_score"] == 80
        assert math.isfinite(float(expectations["min_score"]))
        assert math.isfinite(float(expectations["min_intent_score"]))

        gates = expectations["required_gates"]
        assert gates and len(gates) == len(set(gates))
        assert set(gates) <= KNOWN_GATES
        assert set(gates) == _expected_deterministic_gates(case), case_id

    assert len(ids) == len(set(ids))
    assert len(briefs) == len(set(briefs))


def test_suite_covers_every_builder_and_key_product_domains():
    cases = _suite()["cases"]
    covered = {case["expectations"]["expected_stack"] for case in cases}
    assert covered == set(REAL_BUILDER_STACKS)

    all_tags = {tag for case in cases for tag in case["tags"]}
    assert {
        "ai-native",
        "automation",
        "commerce",
        "desktop",
        "documentation",
        "finance",
        "game",
        "hospitality",
        "mobile",
        "security",
    } <= all_tags
    assert sum("variant" in case["tags"] for case in cases) >= 10


def test_required_artifacts_are_safe_and_exist_in_each_deterministic_scaffold():
    for case in _suite()["cases"]:
        case_id = case["id"]
        artifacts = case["expectations"]["required_artifacts"]
        assert artifacts and len(artifacts) == len(set(artifacts))
        for artifact in artifacts:
            path = PurePosixPath(artifact)
            assert artifact == path.as_posix(), (case_id, artifact)
            assert not path.is_absolute()
            assert path.parts and all(part not in {"", ".", ".."} for part in path.parts)
            assert "\\" not in artifact and ":" not in artifact and "\x00" not in artifact

        agent_stack = _normalize_stack(case["stack"])
        assert agent_stack, (case_id, case["stack"])
        scaffold = scaffold_for(agent_stack, case_id, case["brief"])
        missing = set(artifacts) - set(scaffold)
        assert not missing, f"{case_id}: scaffold no longer emits {sorted(missing)}"
