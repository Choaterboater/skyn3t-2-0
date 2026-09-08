"""Guard tests for auto-promotion quality in ``SkillLibrary.maybe_promote_pattern``.

Five previously auto-promoted ``pattern-*`` skills were retired for having no
actionable content — their bodies were nothing but a win-rate statistic and a
bare stage *count* (``stages: 11``), identical across every stack. Without a
root-cause guard in ``maybe_promote_pattern`` itself, those slugs (or new ones
shaped just like them) can be silently recreated the next time a qualifying
but stats-only :class:`~skyn3t.intelligence.build_patterns.PatternRecord` is
recorded on the pattern board.

These tests exercise the guard directly against ``PatternRecord`` instances so
they do not depend on the current build pipeline's shape producer (which
already emits richer shapes) — the guard must hold even if some other
producer (or the old pipeline, or a future regression) ever emits a bare
count again. No network, no heavy deps.
"""

from __future__ import annotations

from structlog.testing import capture_logs

from skyn3t.intelligence.build_patterns import PatternRecord
from skyn3t.intelligence.skill_library import (
    PROMOTE_MIN_RATE,
    PROMOTE_MIN_USES,
    SkillLibrary,
)

# A qualifying pattern clears both thresholds so the shape guard — not the
# uses/rate gate — is what's actually under test below.
_QUALIFYING_USES = PROMOTE_MIN_USES + 1
_QUALIFYING_WINS = _QUALIFYING_USES  # 100% win rate, comfortably above PROMOTE_MIN_RATE
assert _QUALIFYING_WINS / _QUALIFYING_USES >= PROMOTE_MIN_RATE


def _qualifying_record(shape: object, *, fp: str = "fp000", stack: str = "cli") -> PatternRecord:
    return PatternRecord(
        fp=fp,
        stack=stack,
        shape=shape,  # type: ignore[arg-type]
        uses=_QUALIFYING_USES,
        wins=_QUALIFYING_WINS,
        score_sum=95.0 * _QUALIFYING_USES,
    )


# ---- malformed / empty / statistics-only shapes must never become advice --


def test_rejects_empty_shape():
    lib = SkillLibrary()
    rec = _qualifying_record({}, fp="empty-fp")
    assert lib.maybe_promote_pattern(rec) is None
    assert lib.get("pattern-cli-empty-fp") is None


def test_rejects_retired_stats_only_shape_exact_repro():
    """Reproduces the exact retired ``pattern-cli`` shape: a bare stage count.

    This is the concrete bug the parent audit flagged — 75% over 4 builds,
    body reduced to ``- **stages**: 11`` — reconstructed here to prove the
    guard blocks recreation of that specific already-retired skill. It is one
    example among the generalized checks below, not the whole guard.
    """
    lib = SkillLibrary()
    rec = PatternRecord(
        fp="5975fdb63151f9f9",
        stack="cli",
        shape={"stages": 11},
        uses=4,
        wins=3,  # 75%
        score_sum=300.0,
    )
    assert lib.maybe_promote_pattern(rec) is None
    assert lib.get("pattern-cli-5975fdb63151f9f9") is None


def test_rejects_generic_numeric_only_shape_not_just_stage_count():
    """The guard must generalize past the exact ``stages`` fingerprint.

    Any shape whose leaves are all bare numbers/bools (no names, no file
    paths, no structure) is equally non-actionable, regardless of key names.
    """
    lib = SkillLibrary()
    shape = {"count": 12, "avg_ms": 240.5, "ok": True, "retries": 0}
    rec = _qualifying_record(shape, fp="numeric-only-fp")
    assert lib.maybe_promote_pattern(rec) is None
    assert lib.get("pattern-cli-numeric-only-fp") is None


def test_rejects_shape_with_only_empty_nested_containers():
    lib = SkillLibrary()
    shape = {"stages": [], "meta": {}, "notes": ""}
    rec = _qualifying_record(shape, fp="empty-nested-fp")
    assert lib.maybe_promote_pattern(rec) is None


def test_rejects_non_dict_bare_number_shape():
    lib = SkillLibrary()
    rec = _qualifying_record(11, fp="bare-number-fp")
    assert lib.maybe_promote_pattern(rec) is None


def test_refusal_is_logged_not_silent():
    lib = SkillLibrary()
    rec = PatternRecord(
        fp="5975fdb63151f9f9",
        stack="cli",
        shape={"stages": 11},
        uses=4,
        wins=3,
        score_sum=300.0,
    )
    with capture_logs() as logs:
        assert lib.maybe_promote_pattern(rec) is None
    assert any(
        entry.get("event") == "skills.pattern_promotion_refused"
        and entry.get("reason") == "stats_only_shape"
        for entry in logs
    )


# ---- legitimate structural patterns must still promote --------------------


def test_promotes_named_stage_list_shape():
    """A stage-*name* list (not a bare count) is real, reusable structure."""
    lib = SkillLibrary()
    shape = {"stages": ["plan", "code", "test", "review"]}
    rec = _qualifying_record(shape, fp="named-stages-fp")
    promoted = lib.maybe_promote_pattern(rec)
    assert promoted is not None
    assert promoted.source == "auto-promoted"
    assert promoted.slug == "pattern-cli-named-stages-fp"


def test_promotes_nested_pipeline_shape_like_current_producer():
    """Mirrors the current ``_build_pattern_shape`` schema-2 producer output:

    a nested list of stage dicts carrying real names/roles, not just counts.
    """
    lib = SkillLibrary()
    shape = {
        "schema": 2,
        "pipeline": [
            {
                "name": "plan",
                "agent_type": "planner",
                "capability": "planning",
                "optional": False,
                "gated": False,
            },
            {
                "name": "code",
                "agent_type": "coder",
                "capability": "codegen",
                "optional": False,
                "gated": True,
            },
        ],
        "test_first": True,
        "best_of_n": 2,
    }
    rec = _qualifying_record(shape, fp="pipeline-fp", stack="fastapi")
    promoted = lib.maybe_promote_pattern(rec)
    assert promoted is not None
    assert "code" in promoted.body or "plan" in promoted.body


def test_promotes_plain_string_valued_shape():
    """Regression: a simple, meaningful string value must keep promoting."""
    lib = SkillLibrary()
    rec = _qualifying_record({"plan": "tdd"}, fp="plan-fp", stack="django")
    promoted = lib.maybe_promote_pattern(rec)
    assert promoted is not None


# ---- guard composes correctly with existing threshold + idempotence -------


def test_min_uses_and_min_rate_still_enforced_for_substantial_shapes():
    """The new shape guard must not loosen or bypass the existing gate."""
    lib = SkillLibrary()
    shape = {"stages": ["plan", "code"]}
    below_uses = PatternRecord(
        fp="below-uses-fp", stack="cli", shape=shape, uses=1, wins=1, score_sum=95.0
    )
    assert lib.maybe_promote_pattern(below_uses) is None

    below_rate = PatternRecord(
        fp="below-rate-fp",
        stack="cli",
        shape=shape,
        uses=_QUALIFYING_USES,
        wins=1,
        score_sum=50.0 * _QUALIFYING_USES,
    )
    assert lib.maybe_promote_pattern(below_rate) is None


def test_promotion_is_idempotent_for_qualifying_pattern():
    lib = SkillLibrary()
    shape = {"stages": ["plan", "code", "test"]}
    rec = _qualifying_record(shape, fp="idempotent-fp")
    first = lib.maybe_promote_pattern(rec)
    second = lib.maybe_promote_pattern(rec)
    assert first is not None
    assert second is not None
    assert first.slug == second.slug
    assert [sk.slug for sk in lib.all()].count(first.slug) == 1
