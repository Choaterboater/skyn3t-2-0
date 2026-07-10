# tests/test_cost_stages.py
"""Per-stage cost attribution (Spec 2): slice the LLM-call ledger at stage
boundaries so the manifest records where a build's tokens actually went."""
from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.observability.cost_tracker import CostTracker


class _Call:
    def __init__(
        self,
        cost,
        pt=0,
        ct=0,
        *,
        status="succeeded",
        exposure=0.0,
        generation_id="",
        cost_source="unknown",
    ):
        self.cost_usd = cost
        self.prompt_tokens = pt
        self.completion_tokens = ct
        self.backend = "fake"
        self.model = "fake"
        self.status = status
        self.estimated_exposure_usd = exposure
        self.generation_id = generation_id
        self.cost_source = cost_source


class _Budget:
    def __init__(self):
        self.calls = []
        self.spent_build = 0.0
        self.spent_day = 0.0
        self.tokens_day = 0

    def reset_build(self):
        self.spent_build = 0.0


def test_unpaired_end_stage_does_not_recount_prior_stages():
    # A stage whose start_stage was skipped (e.g. an exception) must attribute
    # nothing, not fall back to build-start and re-count earlier stages.
    budget = _Budget()
    ct = CostTracker(budget=budget)
    ct.start_build("b1")
    ct.start_stage("b1", "plan")
    budget.calls.append(_Call(0.05, 100, 50))
    ct.end_stage("b1", "plan")
    ghost = ct.end_stage("b1", "code")  # never started
    assert ghost["cost_usd"] == 0.0 and ghost["tokens"] == 0


def test_duplicate_end_stage_returns_zero():
    # Calling end_stage twice for one stage must not re-slice the same calls.
    budget = _Budget()
    ct = CostTracker(budget=budget)
    ct.start_build("b1")
    ct.start_stage("b1", "plan")
    budget.calls.append(_Call(0.05, 100, 50))
    first = ct.end_stage("b1", "plan")
    assert round(first["cost_usd"], 4) == 0.05
    second = ct.end_stage("b1", "plan")
    assert second["cost_usd"] == 0.0


def test_per_stage_cost_attribution_slices_the_ledger():
    budget = _Budget()
    ct = CostTracker(budget=budget)
    ct.start_build("b1")

    ct.start_stage("b1", "plan")
    budget.calls.append(_Call(0.01, 100, 50))
    budget.calls.append(_Call(0.02, 200, 100))
    s1 = ct.end_stage("b1", "plan")
    assert s1["stage"] == "plan"
    assert round(s1["cost_usd"], 4) == 0.03 and s1["tokens"] == 450

    ct.start_stage("b1", "code")
    budget.calls.append(_Call(0.05, 300, 200))
    s2 = ct.end_stage("b1", "code")
    assert round(s2["cost_usd"], 4) == 0.05 and s2["tokens"] == 500

    rep = ct.end_build("b1")
    stages = {x["stage"]: x for x in rep["stages"]}
    assert set(stages) == {"plan", "code"}
    # build-level total is all three calls; the per-stage slices partition it.
    assert round(rep["cost_usd"], 4) == 0.08
    assert round(stages["plan"]["cost_usd"] + stages["code"]["cost_usd"], 4) == 0.08


def test_per_stage_does_not_disturb_build_attribution():
    # per-stage slicing is read-only — it must not claim calls or change the
    # build-level cost (which other overlapping builds rely on).
    budget = _Budget()
    ct = CostTracker(budget=budget)
    ct.start_build("b1")
    ct.start_stage("b1", "code")
    budget.calls.append(_Call(0.04, 100, 100))
    ct.end_stage("b1", "code")
    rep = ct.end_build("b1")
    assert round(rep["cost_usd"], 4) == 0.04 and rep["tokens"] == 200


def test_end_build_is_idempotent_and_preserves_terminal_cost():
    budget = _Budget()
    ct = CostTracker(budget=budget)
    ct.start_build("b1")
    budget.calls.append(_Call(0.75, 100, 25))

    first = ct.end_build("b1")
    second = ct.end_build("b1")

    assert second == first
    assert second["cost_usd"] == 0.75
    assert second["tokens"] == 125


def test_provider_generation_cost_evidence_is_preserved():
    budget = _Budget()
    ct = CostTracker(budget=budget)
    ct.start_build("b1")
    budget.calls.append(_Call(
        0.75,
        100,
        25,
        generation_id="gen-123",
        cost_source="provider",
    ))

    report = ct.end_build("b1")

    assert report["usage_evidence"][0]["generation_id"] == "gen-123"
    assert report["usage_evidence"][0]["cost_usd"] == 0.75
    assert report["usage_evidence"][0]["cost_source"] == "provider"


def test_failed_attempt_exposure_is_separate_from_confirmed_cost():
    budget = _Budget()
    ct = CostTracker(budget=budget)
    ct.start_build("b1")
    ct.start_stage("b1", "code")
    budget.calls.extend([
        _Call(0.0, status="failed_transient", exposure=1.25),
        _Call(0.4, 100, 50),
    ])

    stage = ct.end_stage("b1", "code")
    report = ct.end_build("b1")

    assert report["cost_usd"] == 0.4
    assert report["failed_attempts"] == 1
    assert report["max_unconfirmed_exposure_usd"] == 1.25
    assert report["usage_evidence"] == [{
        "generation_id": None,
        "model": "fake",
        "backend": "fake",
        "status": "failed_transient",
        "cost_usd": 0.0,
        "cost_source": "unknown",
        "max_unconfirmed_exposure_usd": 1.25,
    }]
    assert stage["failed_attempts"] == 1
    assert stage["max_unconfirmed_exposure_usd"] == 1.25


def test_stage_methods_safe_for_unknown_build():
    ct = CostTracker(budget=_Budget())
    ct.start_stage("ghost", "plan")  # no such build — must not raise
    rec = ct.end_stage("ghost", "plan")
    assert rec["cost_usd"] == 0.0


def test_disabled_caps_are_reported_as_unlimited_not_exhausted():
    budget = _Budget()
    budget.spent_day = 123.0
    budget.tokens_day = 9_000_000
    tracker = CostTracker(
        settings=Settings(
            per_build_usd_cap=0,
            daily_usd_cap=0,
            daily_token_cap=0,
        ),
        budget=budget,
    )

    report = tracker.report()

    assert report["daily_cap_enabled"] is False
    assert report["build_cap_enabled"] is False
    assert report["token_cap_enabled"] is False
    assert report["daily_remaining_usd"] is None
    assert report["build_remaining_usd"] is None
