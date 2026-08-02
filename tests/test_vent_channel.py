"""Codegen vent channel — agent friction reports reach the learning pipeline.

Ported from Lovable's measured vent loop: when the PIPELINE blocks codegen (a
missing tool, a contradictory directive, an undiagnosable error, an impossible
gate), the agent emits bounded ``VENT: `` lines instead of silently working
around the blocker or faking compliance. CodeAgent parses them into
``run_metadata["vents"]`` + ``logs/<slug>/vents.jsonl`` + CODEGEN_VENT events;
``capture_from_build`` mints deduped "vent" lessons through the existing
add_lesson path. Vents never ship in written files and never fail a build.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import pytest

from skyn3t.agents.code_agent import (
    _VENT_DIRECTIVE,
    CodeAgent,
    parse_vents,
    read_vents_log,
    strip_vent_lines,
)
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus, EventType
from skyn3t.intelligence.learning_loop import LearningLoop

_REAL_APP = "// a real multi-line app\n" + ("const item = { id: 1, name: 'x' };\n" * 400)


@pytest.fixture(autouse=True)
def _logs_dir(tmp_path, monkeypatch):
    """Redirect the per-build vent log into the test's temp dir."""
    from skyn3t.config import settings as settings_mod

    monkeypatch.setenv("SKYN3T_LOGS_DIR", str(tmp_path / "logs"))
    settings_mod.get_settings.cache_clear()
    yield tmp_path / "logs"
    settings_mod.get_settings.cache_clear()


def _make_agent(bus: EventBus) -> CodeAgent:
    agent = CodeAgent(event_bus=bus)
    agent.llm._backend = "claude_cli"  # type: ignore[attr-defined]
    return agent


def _backend_patch(agent: CodeAgent):
    return patch.object(type(agent.llm), "backend", new_callable=lambda: property(
        lambda self: getattr(self, "_backend", "stub")
    ))


async def _run_agentic(agent: CodeAgent, tmp_path, fake_build, slug="vent-app"):
    """Run the monolithic agentic path with a fake agentic_build."""
    with _backend_patch(agent):
        agent.llm.agentic_build = fake_build  # type: ignore[method-assign]
        task = TaskRequest(
            type="codegen",
            payload={"brief": "a react counter app", "slug": slug,
                     "worktree_dir": str(tmp_path)},
            capabilities_required=("codegen",),
        )
        return await agent.run(task)


# ---- 1. the convention reaches every codegen prompt variant ----------------

def test_agentic_prompt_contains_vent_convention_web_and_non_web():
    agent = _make_agent(EventBus())
    plan = {"files": [], "summary": "s"}
    for stack in ("react_vite", "python"):  # web stack + non-web stack
        prompt = agent._agentic_prompt("a small app", stack, plan, "")
        assert _VENT_DIRECTIVE in prompt
        assert "VENT: " in prompt


def test_slice_and_resume_prompts_carry_the_convention():
    agent = _make_agent(EventBus())
    plan = {"files": [], "summary": "s"}
    resume = agent._agentic_resume_prompt("a small app", "react_vite", plan, {}, ["App.jsx"], "", "")
    slice_prompt = agent._agentic_slice_prompt("a small app", "react_vite", "frontend", ["App.jsx"], "App.jsx", "")
    slice_resume = agent._agentic_slice_resume_prompt("a small app", "react_vite", "frontend", ["App.jsx"], {}, ["App.jsx"], "")
    for prompt in (resume, slice_prompt, slice_resume):
        assert _VENT_DIRECTIVE in prompt


# ---- 2. parse + cap + trim + record ----------------------------------------

def test_parse_vents_caps_trims_and_flattens():
    text = (
        "ordinary prose\n"
        "VENT:   missing   the   docker   tool\n"
        "VENT: " + ("x" * 400) + "\n"
        "VENT:\n"          # empty vent -> dropped
        "VENT: third\n"
        "VENT: fourth — over the cap\n"
    )
    vents = parse_vents(text)
    assert vents[0] == "missing the docker tool"        # whitespace flattened
    assert len(vents[1]) == 300                          # trimmed to 300 chars
    assert vents[2] == "third"
    assert len(vents) == 3                               # max 3, fourth ignored
    assert parse_vents("") == [] and parse_vents(None) == []


async def test_vents_recorded_in_metadata_events_and_log(tmp_path, _logs_dir):
    bus = EventBus()
    vent_events: list[dict] = []

    async def _on_vent(evt):
        vent_events.append(evt.payload)

    bus.subscribe(EventType.CODEGEN_VENT, _on_vent)

    agent = _make_agent(bus)
    await agent.start()
    calls = {"n": 0}

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i == 0:
            # Under-deliver (writes nothing) AND vent -> forces one resume attempt.
            return {"ok": True, "backend": "claude_cli",
                    "output_text": "working on it\nVENT: no docker tool available\n"}
        pathlib.Path(workdir, "App.jsx").write_text(_REAL_APP)
        return {"ok": True, "completed": True, "backend": "claude_cli",
                "output_text": "done\nVENT: contradictory design directive\nVENT: second retry vent\n"}

    result = await _run_agentic(agent, tmp_path, fake_agentic_build)

    assert calls["n"] == 2, "first under-delivery must trigger the resume attempt"
    # run_metadata -> TaskResult output: capped at 3 across attempts, in order.
    assert result.output["vents"] == [
        "no docker tool available",
        "contradictory design directive",
        "second retry vent",
    ]
    # One CODEGEN_VENT event per vent, tagged with slug + attempt.
    assert vent_events == [
        {"slug": "vent-app", "vent": "no docker tool available", "attempt": 0},
        {"slug": "vent-app", "vent": "contradictory design directive", "attempt": 1},
        {"slug": "vent-app", "vent": "second retry vent", "attempt": 1},
    ]
    # Persisted to logs/<slug>/vents.jsonl, one JSON record per line.
    log_path = _logs_dir / "vent-app" / "vents.jsonl"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert records == vent_events
    # The reader the learning capture uses: bounded + de-duplicated texts.
    assert read_vents_log(_logs_dir, "vent-app") == result.output["vents"]


# ---- 3. vents never leak into written files --------------------------------

def test_strip_vent_lines_is_surgical():
    body = "const a = 1;\nVENT: leaked into the file\nconst b = 2;\n"
    assert strip_vent_lines(body) == "const a = 1;\nconst b = 2;\n"
    plain = "const a = 1;\n"
    assert strip_vent_lines(plain) is plain  # no VENT marker at all -> identity
    midline = "const url = 'VENT: not at line start';\n"
    assert strip_vent_lines(midline) == midline  # VENT: mid-line is not a vent


async def test_vent_lines_never_reach_written_files(tmp_path, _logs_dir):
    agent = _make_agent(EventBus())
    await agent.start()

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        pathlib.Path(workdir, "App.jsx").write_text(
            "VENT: this vent must not ship\n" + _REAL_APP
        )
        return {"ok": True, "completed": True, "backend": "claude_cli",
                "output_text": "VENT: this vent must not ship\n"}

    result = await _run_agentic(agent, tmp_path, fake_agentic_build)

    assert result.output["vents"] == ["this vent must not ship"]  # still captured
    shipped = (tmp_path / "App.jsx").read_text()
    assert "VENT:" not in shipped, "a VENT line must never land in a written source file"
    assert "const item" in shipped, "the scrub must keep the real code intact"


# ---- 4. learning loop mints deduped "vent" lessons -------------------------

async def test_capture_from_build_mints_deduped_vent_lessons():
    loop = LearningLoop(store=None, event_bus=EventBus())
    build = {
        "stack": "react_vite",
        "slug": "vent-app",
        "vents": [
            "no docker tool available",
            "no docker tool available",   # duplicate within one build -> one lesson
            "contradictory design directive",
        ],
    }
    ids = await loop.capture_from_build(dict(build))
    texts = [m["text"] for m in loop._mem]
    vent_texts = [t for t in texts if ": vent — " in t]
    assert vent_texts == [
        "react_vite: vent — no docker tool available",
        "react_vite: vent — contradictory design directive",
    ]
    assert len(ids) == len(texts) and len(ids) > 0

    # A later build venting the SAME friction mints nothing new (existing
    # capture-side dedupe — no parallel store, no duplicate rows).
    again = await loop.capture_from_build(dict(build))
    assert again == [], "recurring vents must dedupe against the stored lesson"


# ---- 5. no vents -> zero behavior change ------------------------------------

def test_prompt_diff_is_exactly_the_directive():
    agent = _make_agent(EventBus())
    prompt = agent._agentic_prompt("a small app", "react_vite", {"files": [], "summary": "s"}, "")
    assert prompt.count(_VENT_DIRECTIVE) == 1
    # Remove the injected directive line and NOTHING vent-related remains.
    assert "VENT:" not in prompt.replace(f"{_VENT_DIRECTIVE}\n", "")


async def test_no_vents_zero_behavior_change(tmp_path, _logs_dir):
    bus = EventBus()
    vent_events: list[dict] = []

    async def _on_vent(evt):
        vent_events.append(evt.payload)

    bus.subscribe(EventType.CODEGEN_VENT, _on_vent)

    agent = _make_agent(bus)
    await agent.start()

    async def fake_agentic_build(prompt, workdir, timeout=None, **kwargs):
        pathlib.Path(workdir, "App.jsx").write_text(_REAL_APP)
        return {"ok": True, "completed": True, "backend": "claude_cli", "output_text": "all done"}

    result = await _run_agentic(agent, tmp_path, fake_agentic_build)

    assert "degraded" not in result.output
    assert "vents" not in result.output, "a vent-free build must not gain a vents key"
    assert vent_events == []
    assert not (_logs_dir / "vent-app" / "vents.jsonl").exists()
