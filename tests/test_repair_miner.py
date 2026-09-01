"""Repair miner: stuck->resolved deterministic repairs become up-front knowledge.

Covers the LSO-style self-learning edge end to end:

  manifest (repair key + passed proof) -> finding with the right error class;
  clean first-pass / unresolved / malformed manifests -> no findings;
  findings persist as deduped lessons (stable title text, store default score);
  the same (stack, repair_key) on a 2nd 'go' build promotes to a
  ``won-repair-<key>-<stack>`` skill that loads and injects;
  injection -> grading attributes uses to the mined lesson AND skill;
  a promoted repair skill with 3+ all-unhelpful uses is demoted by outcome.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.intelligence.build_patterns import BuildPatternBoard
from skyn3t.intelligence.repair_miner import (
    RepairFinding,
    demote_failing_repair_skills,
    lesson_text,
    mine_repairs,
    persist_findings_as_lessons,
    record_and_promote,
    repair_skill_slug,
)
from skyn3t.intelligence.skill_library import SkillLibrary
from skyn3t.memory.store import MemoryStore

_ASTRO_EXTRA = {
    "deterministic_repairs": {"astro_estree_pin": ["package.json"]},
    "proof": {"passed": True},
}


def _manifest(extra=None, *, verdict="go", stack="astro", score=88.0):
    return SimpleNamespace(
        extra=extra if extra is not None else dict(_ASTRO_EXTRA),
        verdict=verdict,
        stack=stack,
        score=score,
        slug="site-a",
        build_id="b1",
    )


# ---- mining ---------------------------------------------------------------


def test_astro_estree_pin_with_passed_proof_yields_one_finding():
    findings = mine_repairs(_manifest())
    assert len(findings) == 1
    f = findings[0]
    assert f.repair_key == "astro_estree_pin"
    assert f.error_class == "astro build fails on Node 24 ESM/CJS interop"
    assert f.stack == "astro"
    assert f.files_count == 1


def test_finding_carries_one_gate_finding_line_as_evidence():
    extra = dict(
        _ASTRO_EXTRA,
        seo={"skipped": False, "issues": ["page has no meta description"]},
    )
    (f,) = mine_repairs(_manifest(extra))
    assert f.example_evidence == "seo: page has no meta description"


def test_clean_first_pass_build_yields_no_findings():
    assert mine_repairs(_manifest({"proof": {"passed": True}})) == []
    assert mine_repairs(_manifest({})) == []
    # Repairs ran but changed nothing — nothing was ever stuck.
    quiet = {
        "deterministic_repairs": {"astro_estree_pin": [], "npm_deps_added": []},
        "proof": {"passed": True},
    }
    assert mine_repairs(_manifest(quiet)) == []


def test_stuck_but_unresolved_build_yields_no_findings():
    extra = {"deterministic_repairs": {"astro_estree_pin": ["package.json"]}}
    # Proof still failing and no 'go' verdict -> the repair did NOT resolve it.
    assert mine_repairs(_manifest(extra, verdict="no_go")) == []
    failed = dict(extra, proof={"passed": False})
    assert mine_repairs(_manifest(failed, verdict="no_go")) == []
    # A 'go' verdict alone is enough resolution signal even without proof dict.
    assert len(mine_repairs(_manifest(extra, verdict="go"))) == 1


def test_fix_loop_repair_records_are_mined_and_llm_noise_ignored():
    extra = {
        "fix_attempt_1": {
            "passed": False,
            "repairs": {
                "use_client_added": ["app/page.tsx"],
                "contrast_issues": [{"ratio": 2.1}],  # advisory lint, not a repair
                "unknown_future_key": ["x"],  # forward compat: ignored
            },
        },
        "fix_attempt_2": {"passed": True, "repairs": {"use_client_added": []}},
        "proof": {"passed": True},
    }
    findings = mine_repairs(_manifest(extra, stack="nextjs"))
    assert [f.repair_key for f in findings] == ["use_client_added"]
    assert findings[0].files_count == 1


def test_all_recording_sources_merge_per_repair_key():
    extra = {
        "npm_deps_added": ["react"],  # legacy per-key recording
        "final_consistency_repairs": {"imports_scaffolded": ["src/a.js"]},
        "runtime_self_heal": {
            "rounds": [{"repairs": {"npm_deps_added": ["react-dom"]}}]
        },
        "proof": {"passed": True},
    }
    findings = mine_repairs(_manifest(extra, stack="react"))
    by_key = {f.repair_key: f for f in findings}
    assert set(by_key) == {"npm_deps_added", "imports_scaffolded"}
    assert by_key["npm_deps_added"].files_count == 2  # react + react-dom unioned


def test_mining_never_raises_on_malformed_manifest():
    assert mine_repairs(None) == []
    assert mine_repairs(SimpleNamespace(extra=None, verdict="go")) == []
    junk = SimpleNamespace(
        extra={
            "proof": "not-a-dict",
            "deterministic_repairs": [1, 2, 3],
            "fix_attempt_1": "garbage",
            "fix_attempt_2": {"repairs": "not-a-dict"},
            "runtime_self_heal": {"rounds": "nope"},
            "npm_deps_added": {"unexpected": "dict"},
        },
        verdict="go",
        stack="astro",
    )
    assert mine_repairs(junk) == []

    class Exploding:
        @property
        def extra(self):  # pragma: no cover - exercised via mine_repairs
            raise RuntimeError("boom")

    assert mine_repairs(Exploding()) == []


# ---- persist as lessons ---------------------------------------------------


def test_lesson_minted_with_stable_title_and_deduped(tmp_path):
    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        findings = mine_repairs(_manifest())
        first = await persist_findings_as_lessons(
            findings, store, stack="astro", source_build="b1"
        )
        assert len(first) == 1
        title = "When astro astro build fails on Node 24 ESM/CJS interop: apply astro_estree_pin"
        assert lesson_text(findings[0]) == title
        # A second identical build dedupes (and bumps nothing — one row, graded
        # by outcome later), it does not duplicate.
        second = await persist_findings_as_lessons(
            mine_repairs(_manifest()), store, stack="astro", source_build="b2"
        )
        assert second == []
        rows = await store.relevant_lessons("astro", limit=50)
        assert [r["text"] for r in rows] == [title]
        assert rows[0]["score"] == 0.0, "fresh lesson starts mid-range; grading moves it"
        await store.close()

    asyncio.run(go())


def test_lesson_dedupe_holds_when_evidence_differs(tmp_path):
    """The lesson text is the stable title, so DIFFERENT builds with the same
    (stack, repair_key) still dedupe — volatile evidence rides on the skill."""

    async def go():
        store = MemoryStore(Settings(data_dir=tmp_path))
        await store.init_db()
        a = mine_repairs(_manifest())
        b = mine_repairs(
            _manifest({"deterministic_repairs": {"astro_estree_pin": ["pkg.json", "lock.json"]},
                       "proof": {"passed": True}})
        )
        assert b[0].files_count == 2  # genuinely different build
        await persist_findings_as_lessons(a, store, stack="astro", source_build="b1")
        await persist_findings_as_lessons(b, store, stack="astro", source_build="b2")
        rows = await store.relevant_lessons("astro", limit=50)
        assert len(rows) == 1
        await store.close()

    asyncio.run(go())


# ---- promote to skills ----------------------------------------------------

_SLUG = "won-repair-astro-estree-pin-astro"


def test_repair_skill_slug_shape():
    assert repair_skill_slug("astro", "astro_estree_pin") == _SLUG


def test_promotion_fires_on_second_go_build_and_skill_loads(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    board = BuildPatternBoard(tmp_path / "patterns.json")
    findings = mine_repairs(_manifest())

    first = record_and_promote(findings, board, lib, score=88.0, go=True, example="a1")
    assert first == [], "one qualifying build is not enough"
    assert lib.get(_SLUG) is None

    second = record_and_promote(findings, board, lib, score=90.0, go=True, example="a2")
    assert [s.slug for s in second] == [_SLUG]
    sk = lib.get(_SLUG)
    assert sk is not None
    assert sk.source == "repair-mined"
    assert set(sk.tags) >= {"astro", "repair-mined", "astro_estree_pin"}
    assert sk.title == lesson_text(findings[0])
    assert "astro_estree_pin" in sk.body and "package.json" in sk.body
    assert sk.score == 0.5, "fresh skill starts mid-range; record_use moves it"
    assert (tmp_path / "skills" / f"{_SLUG}.md").exists()

    # Loads through a fresh library and is injectable up front for the stack.
    reloaded = SkillLibrary(tmp_path / "skills")
    assert reloaded.get(_SLUG) is not None
    assert any(s.slug == _SLUG for s in reloaded.relevant("astro", limit=10))

    # A third qualifying build reinforces the pattern but never duplicates.
    third = record_and_promote(findings, board, lib, score=91.0, go=True, example="a3")
    assert third == []
    assert len([s for s in lib.all() if s.slug == _SLUG]) == 1


def test_no_promotion_for_nogo_builds(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    board = BuildPatternBoard(tmp_path / "patterns.json")
    findings = mine_repairs(_manifest())
    assert record_and_promote(findings, board, lib, score=30.0, go=False) == []
    assert record_and_promote(findings, board, lib, score=30.0, go=False) == []
    assert lib.get(_SLUG) is None, "only 'go' builds count toward promotion"


# ---- prune by outcome -----------------------------------------------------


def _promoted(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    board = BuildPatternBoard(tmp_path / "patterns.json")
    findings = mine_repairs(_manifest())
    record_and_promote(findings, board, lib, score=88.0, go=True, example="a1")
    record_and_promote(findings, board, lib, score=90.0, go=True, example="a2")
    return lib


def test_demotion_after_three_unhelpful_uses(tmp_path):
    lib = _promoted(tmp_path)
    for _ in range(3):
        lib.record_use(_SLUG, helpful=False)
    assert demote_failing_repair_skills(lib) == [_SLUG]
    sk = lib.get(_SLUG)
    assert "hygiene:quarantine" in sk.tags
    assert all(s.slug != _SLUG for s in lib.relevant("astro", limit=10))
    # Quarantine persists to disk: a reloaded library keeps it out of injection.
    reloaded = SkillLibrary(tmp_path / "skills")
    assert "hygiene:quarantine" in reloaded.get(_SLUG).tags
    assert all(s.slug != _SLUG for s in reloaded.relevant("astro", limit=10))


def test_no_demotion_when_a_use_was_helpful_or_uses_too_few(tmp_path):
    lib = _promoted(tmp_path)
    lib.record_use(_SLUG, helpful=False)
    lib.record_use(_SLUG, helpful=False)
    assert demote_failing_repair_skills(lib) == [], "fewer than 3 uses: too early to judge"
    lib.record_use(_SLUG, helpful=True)
    assert demote_failing_repair_skills(lib) == [], "one helpful use clears the prune"


def test_demotion_only_touches_repair_mined_skills(tmp_path):
    lib = _promoted(tmp_path)
    other = lib.add(
        title="Winning astro build shape", body="shape", stack="astro",
        tags=["astro", "build-distilled"], source="build-distilled", slug="won-astro-shape",
    )
    for slug in (_SLUG, other.slug):
        for _ in range(3):
            lib.record_use(slug, helpful=False)
    assert demote_failing_repair_skills(lib) == [_SLUG]
    assert "hygiene:quarantine" not in lib.get(other.slug).tags


def test_repair_finding_dataclass_is_self_describing():
    f = RepairFinding(
        stack="astro",
        error_class="astro build fails on Node 24 ESM/CJS interop",
        repair_key="astro_estree_pin",
        files_count=1,
        example_evidence="seo: missing description",
    )
    assert lesson_text(f) == (
        "When astro astro build fails on Node 24 ESM/CJS interop: apply astro_estree_pin"
    )
    assert repair_skill_slug(f.stack, f.repair_key) == _SLUG
