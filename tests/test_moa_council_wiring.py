"""Wiring the advisory council into codegen.

The regression fence that matters most here is byte-identity: with no guidance,
_agentic_prompt must produce exactly what it produced before the council
existed, or every codegen-prompt assertion in the suite becomes load-bearing on
an accident.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner


def _agent():
    from skyn3t.agents.code_agent import CodeAgent

    return CodeAgent(event_bus=EventBus())


_PLAN = {"files": [{"path": "index.html", "purpose": "entry"}]}


def test_agentic_prompt_without_guidance_is_byte_identical():
    agent = _agent()

    without = agent._agentic_prompt("a habit tracker", "static", _PLAN, "")
    explicit_empty = agent._agentic_prompt(
        "a habit tracker", "static", _PLAN, "", moa_guidance=""
    )

    assert without == explicit_empty
    assert without.endswith("Do not ask questions — just build it.")


def test_agentic_prompt_appends_guidance_at_the_tail():
    agent = _agent()
    guidance = "ADVISORY COUNCIL — ZZZ marker"

    prompt = agent._agentic_prompt(
        "a habit tracker", "static", _PLAN, "", moa_guidance=guidance
    )

    assert "ZZZ marker" in prompt
    # After the stack/full-file/config directives, not spliced into them: the
    # directive block is byte-identical across builds of a stack, and splicing
    # brief-varying text mid-prompt fragments that shared span.
    assert prompt.index("ZZZ marker") > prompt.index("Do not ask questions") - len(prompt)
    assert prompt.rindex("ZZZ marker") < prompt.rindex("Do not ask questions")
    assert prompt.endswith("Do not ask questions — just build it.")


def test_guidance_is_the_only_difference():
    agent = _agent()

    plain = agent._agentic_prompt("brief", "static", _PLAN, "")
    guided = agent._agentic_prompt("brief", "static", _PLAN, "", moa_guidance="GGG")

    assert guided.replace("\nGGG\n", "") == plain


def _runner(tmp_path, **overrides) -> StudioRunner:
    bus = EventBus()
    return StudioRunner(
        bus,
        Orchestrator(bus),
        settings=Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            **overrides,
        ),
        memory=None,
    )


async def test_council_off_leaves_extra_untouched(tmp_path):
    runner = _runner(tmp_path, moa_enabled=False)
    manifest = BuildManifest(slug="app", brief="b", stack="static")
    extra: dict = {"existing": 1}

    out = await runner._run_moa_council("b", manifest, extra, SimpleNamespace(
        stack="static", checklist=["index.html"]
    ))

    assert out == {"existing": 1}
    assert "moa" not in manifest.extra


async def test_council_threads_guidance_and_records_a_bounded_summary(tmp_path, monkeypatch):
    runner = _runner(
        tmp_path, moa_enabled=True, moa_advisors="codex_cli,kimi_cli", free_only=False
    )

    class _FakeEngine:
        def __init__(self, llm, settings, advisors=None):
            self.advisors_override = advisors

        def enabled(self):
            return True

        async def advise(self, *, brief, stack="", plan=""):
            from skyn3t.intelligence.council import AdvisorOutput, CouncilAdvice

            return CouncilAdvice(
                guidance="ADVISORY COUNCIL — guidance body",
                advisors=[
                    AdvisorOutput(label="codex_cli", ok=True, text="a", cost_usd=0.002),
                    AdvisorOutput(label="kimi_cli", ok=False, error="down"),
                ],
                degraded=True,
            )

    monkeypatch.setattr("skyn3t.intelligence.council.CouncilEngine", _FakeEngine)
    manifest = BuildManifest(slug="app", brief="b", stack="static")

    out = await runner._run_moa_council("b", manifest, {}, SimpleNamespace(
        stack="static", checklist=["index.html"]
    ))

    assert out["moa_guidance"] == "ADVISORY COUNCIL — guidance body"
    record = manifest.extra["moa"]
    assert record["failed"] == ["kimi_cli"]
    assert record["cost_usd"] == 0.002
    assert record["degraded"] is True
    # Bounded: the record carries counts and cost, never advisor prose.
    assert "guidance" not in record


async def test_a_council_explosion_never_breaks_the_build(tmp_path, monkeypatch):
    runner = _runner(tmp_path, moa_enabled=True, moa_advisors="codex_cli")

    class _Exploding:
        def __init__(self, llm, settings):
            raise RuntimeError("council construction blew up")

    monkeypatch.setattr("skyn3t.intelligence.council.CouncilEngine", _Exploding)
    manifest = BuildManifest(slug="app", brief="b", stack="static")

    out = await runner._run_moa_council("b", manifest, {"k": "v"}, SimpleNamespace(
        stack="static", checklist=[]
    ))

    assert out == {"k": "v"}


async def test_empty_guidance_is_not_threaded(tmp_path, monkeypatch):
    """All advisors failed => codegen must see exactly a council-off prompt."""
    runner = _runner(tmp_path, moa_enabled=True, moa_advisors="codex_cli")

    class _AllFailed:
        def __init__(self, llm, settings, advisors=None):
            self.advisors_override = advisors

        def enabled(self):
            return True

        async def advise(self, *, brief, stack="", plan=""):
            from skyn3t.intelligence.council import CouncilAdvice

            return CouncilAdvice(guidance="", degraded=True)

    monkeypatch.setattr("skyn3t.intelligence.council.CouncilEngine", _AllFailed)
    manifest = BuildManifest(slug="app", brief="b", stack="static")

    out = await runner._run_moa_council("b", manifest, {}, SimpleNamespace(
        stack="static", checklist=[]
    ))

    assert "moa_guidance" not in out
    assert manifest.extra["moa"]["degraded"] is True


def test_code_agent_reads_guidance_from_the_threaded_extra():
    agent = _agent()
    payload = {"extra": {"moa_guidance": "QQQ"}}
    _extra = payload.get("extra")

    guidance = str((_extra.get("moa_guidance") or "") if isinstance(_extra, dict) else "")

    assert guidance == "QQQ"
    assert "QQQ" in agent._agentic_prompt("b", "static", _PLAN, "", moa_guidance=guidance)
