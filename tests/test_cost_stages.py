# tests/test_cost_stages.py
"""Per-stage cost attribution (Spec 2): slice the LLM-call ledger at stage
boundaries so the manifest records where a build's tokens actually went."""
from __future__ import annotations

from skyn3t.observability.cost_tracker import CostTracker


class _Call:
    def __init__(self, cost, pt=0, ct=0):
        self.cost_usd = cost
        self.prompt_tokens = pt
        self.completion_tokens = ct
        self.backend = "fake"
        self.model = "fake"


class _Budget:
    def __init__(self):
        self.calls = []
        self.spent_build = 0.0
        self.spent_day = 0.0
        self.tokens_day = 0

    def reset_build(self):
        self.spent_build = 0.0


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


def test_stage_methods_safe_for_unknown_build():
    ct = CostTracker(budget=_Budget())
    ct.start_stage("ghost", "plan")  # no such build — must not raise
    rec = ct.end_stage("ghost", "plan")
    assert rec["cost_usd"] == 0.0
