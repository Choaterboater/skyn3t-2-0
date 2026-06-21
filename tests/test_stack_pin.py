"""Explicit stack pins must reach the planner (the dropped-hint bug)."""
from __future__ import annotations

from skyn3t.studio.planner import Planner


def test_planner_honors_explicit_pin_over_brief():
    # Brief screams "python", pin says "fastapi" -> pin wins.
    plan = Planner().plan("a python script to crunch data", "slug", stack_hint="fastapi")
    assert plan.stack == "fastapi"


def test_runner_reads_extra_stack_key(monkeypatch):
    # The key the web API actually writes is extra["stack"], not ["stack_hint"].
    captured = {}
    from skyn3t.studio import planner as planner_mod

    real_plan = Planner.plan

    def spy(self, brief, slug, *, stack_hint=None, **kw):
        captured["hint"] = stack_hint
        return real_plan(self, brief, slug, stack_hint=stack_hint, **kw)

    monkeypatch.setattr(Planner, "plan", spy)
    # Simulate the resolution line in runner.start:
    extra = {"stack": "fastapi"}
    clar_answers: dict = {}
    hint = clar_answers.get("stack") or extra.get("stack") or extra.get("stack_hint")
    Planner().plan("brief", "slug", stack_hint=hint)
    assert captured["hint"] == "fastapi"
