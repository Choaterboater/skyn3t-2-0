"""Divergence-seeded best-of-N design seeds.

Each best-of-N trajectory gets a DISTINCT design seed so candidates diverge
aesthetically, not just in implementation luck. Variant 0 is the control: its
tokens_md is byte-identical to design_md_block(brief). Every variant keeps
--accent-text fitted to WCAG AA against its theme's worst-case surface.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from skyn3t.agents.code_agent import CodeAgent, _seed_tokens_md
from skyn3t.core.events import EventBus
from skyn3t.studio.design_seeds import design_seed_for
from skyn3t.studio.design_tokens import (
    contrast_ratio,
    derive_accent,
    derive_archetype,
    derive_font_pair,
    derive_style,
    derive_theme,
    design_md_block,
)

_BRIEFS = (
    "a habit tracker for busy parents",
    "a neon crypto trading dashboard",  # dark (ink) theme
    "a cozy bakery site",  # warm (sand) theme + keyword font pick
    "a corporate fintech analytics platform",  # cool (slate) theme
)

_HEX_RE = {
    "accent_text": re.compile(r"--accent-text: (#[0-9a-fA-F]{6});"),
    "surface_2": re.compile(r"--surface-2: (#[0-9a-fA-F]{6});"),
}


def test_seed_is_deterministic():
    for brief in _BRIEFS:
        for index in range(6):
            assert design_seed_for(brief, index) == design_seed_for(brief, index)


def test_variant_zero_is_the_control():
    """Variant 0 must be byte-identical to today's behavior: tokens_md IS
    design_md_block(brief), and every axis is the brief-derived pick."""
    for brief in _BRIEFS:
        seed = design_seed_for(brief, 0)
        assert seed["tokens_md"] == design_md_block(brief)
        heading, body = derive_font_pair(brief)
        assert seed["theme"] == derive_theme(brief)
        assert seed["accent"] == derive_accent(brief)
        assert (seed["font_heading"], seed["font_body"]) == (heading, body)
        assert seed["style"] == derive_style(brief)
        assert seed["archetype"] == derive_archetype(brief)


def test_variants_diverge_on_at_least_three_axes():
    for brief in _BRIEFS:
        seeds = [design_seed_for(brief, i) for i in range(4)]
        axes = ("theme", "accent", "font_heading", "font_body", "style", "archetype")
        differing = [axis for axis in axes if len({s[axis] for s in seeds}) > 1]
        assert len(differing) >= 3, f"{brief!r}: only {differing} diverge"
        # Every non-control variant renders a different token block.
        for seed in seeds[1:]:
            assert seed["tokens_md"] != seeds[0]["tokens_md"]


def test_variants_keep_accent_text_fitted_aa():
    """--accent-text must clear WCAG AA (>= 4.5) against the variant theme's
    --surface-2 (the worst-case surface) on every variant, light and dark."""
    for brief in _BRIEFS:
        for index in range(6):
            tokens_md = design_seed_for(brief, index)["tokens_md"]
            accent_text = _HEX_RE["accent_text"].search(tokens_md).group(1)
            surface_2 = _HEX_RE["surface_2"].search(tokens_md).group(1)
            ratio = contrast_ratio(accent_text, surface_2)
            assert ratio >= 4.5, f"{brief!r} variant {index}: {accent_text} on {surface_2} = {ratio}"


def test_seed_tokens_md_extraction():
    seed = design_seed_for("expense tracker", 1)
    assert _seed_tokens_md({"design_seed": seed}) == seed["tokens_md"]
    assert _seed_tokens_md({}) == ""
    assert _seed_tokens_md(None) == ""
    assert _seed_tokens_md({"design_seed": "garbage"}) == ""
    assert _seed_tokens_md({"design_seed": {"tokens_md": 42}}) == ""


_DESIGN = {
    "theme": "dark minimal",
    "palette": {"bg": "#0e1116", "fg": "#e6edf3", "accent": "#6750f2"},
    "typography": "Inter",
    "layout": "card grid",
    "components": ["filter bar", "metric cards"],
    "states": ["empty", "loading", "error"],
}


def _agent() -> CodeAgent:
    return CodeAgent(event_bus=EventBus())


def test_agentic_prompt_injects_seed_tokens_md():
    """With extra['design_seed'] threaded, the seed's tokens_md replaces the
    derived block; the default path stays byte-identical."""
    agent = _agent()
    brief = "a habit tracker for busy parents"
    default = agent._agentic_prompt(brief, "react", {"files": []}, "", design=_DESIGN)
    none_kwarg = agent._agentic_prompt(
        brief, "react", {"files": []}, "", design=_DESIGN, design_tokens_md=None)
    assert default == none_kwarg  # byte-identical default path
    assert design_md_block(brief) in default
    assert "VISUAL DESIGN CONTRACT v1" in default

    seed_md = design_seed_for(brief, 2)["tokens_md"]
    seeded = agent._agentic_prompt(
        brief, "react", {"files": []}, "", design=_DESIGN, design_tokens_md=seed_md)
    assert seed_md in seeded
    # The ONLY difference from the default prompt is the swapped token block.
    assert seeded == default.replace(design_md_block(brief), seed_md)


def test_seed_reaches_resume_slice_and_single_file_prompts():
    agent = _agent()
    brief = "a habit tracker for busy parents"
    seed_md = design_seed_for(brief, 1)["tokens_md"]

    resume = agent._agentic_resume_prompt(
        brief, "react", {"files": []}, {}, ["src/App.jsx"], "timeout", "",
        design=_DESIGN, design_tokens_md=seed_md)
    assert seed_md in resume

    slice_prompt = agent._agentic_slice_prompt(
        brief, "react", "frontend", ["src/App.jsx"], "", "", design=_DESIGN,
        design_tokens_md=seed_md)
    assert seed_md in slice_prompt

    slice_resume = agent._agentic_slice_resume_prompt(
        brief, "react", "frontend", ["src/App.jsx"], {}, ["src/App.jsx"], "timeout",
        design=_DESIGN, design_tokens_md=seed_md)
    assert seed_md in slice_resume

    class _LLM:
        backend = "openrouter"
        supports_agentic = False

        def __init__(self):
            self.prompts = []

        async def complete(self, prompt, **kwargs):
            self.prompts.append(prompt)
            return SimpleNamespace(text="body {}", model="m", backend="openrouter")

    llm = _LLM()
    file_agent = CodeAgent(event_bus=EventBus(), llm=llm)
    asyncio.run(file_agent._generate_file(
        "src/App.jsx", brief, "react", {"files": []}, design=_DESIGN,
        design_tokens_md=seed_md))
    assert len(llm.prompts) == 1
    assert seed_md in llm.prompts[0]
    assert design_md_block(brief) not in llm.prompts[0]


def test_runner_threads_distinct_design_seeds(tmp_path, monkeypatch):
    """_run_code_best_of_n must give each trajectory extra['design_seed'] for
    design web stacks — distinct per index, variant 0 the control — and skip
    seeding entirely for non-design stacks."""
    from pathlib import Path

    from skyn3t.core.agent import TaskResult
    from skyn3t.studio import best_of_n as bon
    from skyn3t.studio import visual_check
    from skyn3t.studio.best_of_n import Candidate, SelectionResult
    from skyn3t.studio.runner import StudioRunner
    from skyn3t.worktree import create_worktree

    monkeypatch.setattr(visual_check, "make_vision_fn", lambda settings: None)
    submitted: list[dict] = []

    async def fake_submit(self, spec, payload, correlation_id):
        submitted.append(payload)
        return TaskResult(task_id=f"t{len(submitted)}", success=True,
                          output={"files_written": 0})

    monkeypatch.setattr(StudioRunner, "_submit_stage", fake_submit)
    monkeypatch.setattr(
        StudioRunner, "_record_best_of_n_match", lambda self, spec, selection: False)

    winner_wt = create_worktree(tmp_path, "bon-winner")
    (Path(winner_wt.dir) / "index.html").write_text("<html></html>", encoding="utf-8")
    sample_kwargs: dict = {}

    async def fake_sample(base_dir, slug, n, trajectory, **kwargs):
        sample_kwargs.update(kwargs)
        for i in range(n):
            await trajectory(create_worktree(tmp_path, f"bon-cand{i}"), i)
        winner = Candidate(
            index=0, worktree=winner_wt,
            result=TaskResult(task_id="w", success=True, output={"files_written": 1}),
            files_written=1,
        )
        selection = SelectionResult(
            winner=winner, candidates=[winner], any_passed=True, reason="fake")
        selection.freeze_evidence()
        return selection

    monkeypatch.setattr(bon, "sample", fake_sample)

    runner = object.__new__(StudioRunner)
    runner.settings = SimpleNamespace(
        projects_dir=tmp_path,
        execution_backend="local",
        run_generated_tests=False,
        generated_test_timeout=1,
        run_generated_build=False,
        generated_build_timeout=1,
        tournament_model_pool="",
        openrouter_codegen_model="",
        preferred_model="",
        free_only=False,
        no_claude=False,
        best_of_n_across_models=False,
    )

    def _run(stack: str) -> None:
        submitted.clear()
        plan = SimpleNamespace(
            best_of_n=3, slug=f"seeded-{stack}", stack=stack,
            brief="a habit tracker for busy parents", checklist=[],
            to_dict=lambda: {"files": [], "summary": ""},
        )
        spec = SimpleNamespace(extra={}, agent_type="code")
        main_wt = create_worktree(tmp_path, f"bon-main-{stack}")
        result = asyncio.run(runner._run_code_best_of_n(
            plan, spec, str(tmp_path / "proj"), {}, [], {"build_id": "b1"},
            "cid", main_wt, []))
        assert result.success is True

    _run("react")
    assert len(submitted) == 3
    seeds = [p["extra"]["design_seed"] for p in submitted]
    assert seeds[0]["tokens_md"] == design_md_block("a habit tracker for busy parents")
    assert len({s["tokens_md"] for s in seeds}) == 3  # one distinct seed per trajectory
    assert sample_kwargs["vision_fn"] is None  # patched judge threads through

    _run("python")  # non-design stack: no seeding, byte-identical extra
    assert len(submitted) == 3
    assert all("design_seed" not in p.get("extra", {}) for p in submitted)
