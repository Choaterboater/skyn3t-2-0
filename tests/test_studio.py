"""Offline tests for the studio package.

No network, no heavy deps. Exercises planner ordering (test-first + checklist),
the approval gate, clarification auto-answer, worktree merge_back (delivered !=
empty), proof_run anti-empty-scaffold, best-of-N selection, and a full offline
StudioRunner build with a stub code agent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import best_of_n as bon
from skyn3t.studio.approval_gate import ApprovalGate, GateDecision
from skyn3t.studio.clarification import analyze, clarify
from skyn3t.studio.manifest import BuildManifest, StageRecord
from skyn3t.studio.planner import Planner, detect_stack, file_checklist
from skyn3t.studio.proof_run import proof_run
from skyn3t.studio.runner import StudioRunner
from skyn3t.worktree import cleanup_worktree, create_worktree, list_files, merge_back


# ---- planner -------------------------------------------------------------
def test_detect_stack():
    assert detect_stack("Build a FastAPI REST API") == "fastapi"
    assert detect_stack("a react dashboard ui") == "react"
    assert detect_stack("something vague") == "python"


def test_detect_stack_routes_portals_and_dashboards_to_web_app():
    assert detect_stack("a client portal web app for approvals and invoices") == "react"
    assert detect_stack("an admin settings dashboard for approvals") == "react"


def test_detect_stack_does_not_read_client_as_cli():
    assert detect_stack("a client portal web app") != "cli"
    assert detect_stack("a python client library for an API") == "python"


def test_planner_default_order():
    p = Planner()
    plan = p.plan("Build a python library", "lib")
    names = plan.stage_names
    assert names.index("code") < names.index("review")
    assert names.index("architect") < names.index("code")
    assert plan.checklist  # P2 checklist present


def test_planner_test_first_ordering():
    p = Planner()
    plan = p.plan("Build a python tool", "tool", test_first=True)
    names = plan.stage_names
    assert "test_author" in names
    assert names.index("test_author") < names.index("code")


def test_planner_best_of_n_implies_test_first():
    p = Planner()
    plan = p.plan("Build a thing", "thing", best_of_n=3)
    assert plan.best_of_n == 3
    assert plan.test_first is True
    assert "test_author" in plan.stage_names


def test_planner_full_app_contract_forces_test_author_for_fast_profile():
    plan = Planner().plan(
        "Build a complete Astro website",
        "full-site",
        stack_hint="astro",
        test_first=False,
        best_of_n=1,
        full_app_contract=True,
    )

    assert plan.test_first is True
    assert "test_author" in plan.stage_names
    assert plan.stage_names.index("test_author") < plan.stage_names.index("code")


def test_planner_uses_default_best_of_two_from_settings(tmp_path):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    plan = Planner(settings).plan("Build a thing", "thing")
    assert plan.best_of_n == 2
    assert plan.test_first is True
    assert "test_author" in plan.stage_names


def test_planner_skips_generated_tests_for_self_tested_stacks(tmp_path):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    for stack in ("agent_pack", "mcp", "rag", "workflow"):
        plan = Planner(settings).plan("Build a thing", "thing", stack_hint=stack)
        assert plan.stack == stack
        assert plan.best_of_n == 2
        assert plan.test_first is False
        assert "test_author" not in plan.stage_names


def test_checklist_injected_into_code_stage():
    p = Planner()
    plan = p.plan("fastapi service", "svc")
    code = next(s for s in plan.stages if s.agent_type == "code")
    assert code.extra.get("checklist") == file_checklist("fastapi")


# ---- clarification -------------------------------------------------------
def test_clarify_auto_answers_when_unattended():
    res = clarify("an app", unattended=True)
    assert res.ambiguous
    assert res.auto_answered
    assert all(v for v in res.answers.values())


def test_analyze_detects_specified_aspects():
    res = analyze("a python cli with sqlite and tests for an internal team")
    keys = {q.key for q in res.questions}
    # stack/persistence/tests/audience all specified -> not asked.
    assert "stack" not in keys
    assert "persistence" not in keys


# ---- approval gate -------------------------------------------------------
def test_approval_gate_approve():
    async def run():
        gate = ApprovalGate(enabled=True, auto_approve=False)
        ap = gate.request("b1", "review")
        assert gate.pending("b1")
        ok = gate.approve(ap.approval_id, "lgtm")
        assert ok
        decision = await gate.wait(ap.approval_id)
        assert decision is GateDecision.APPROVED

    asyncio.run(run())


def test_approval_gate_disabled_auto_approves():
    async def run():
        gate = ApprovalGate(enabled=False)
        ap = gate.request("b1", "review")
        decision = await gate.wait(ap.approval_id)
        assert decision is GateDecision.APPROVED

    asyncio.run(run())


def test_approval_gate_timeout_rejects_by_default():
    async def run():
        gate = ApprovalGate(enabled=True, auto_approve=False)
        ap = gate.request("b1", "review")
        decision = await gate.wait(ap.approval_id, timeout=0.05)
        assert decision is GateDecision.REJECTED

    asyncio.run(run())


def test_approval_gate_request_can_override_stale_construction_policy():
    async def run():
        gate = ApprovalGate(enabled=False, auto_approve=True)

        approval = gate.request(
            "build-live-policy",
            "code",
            enabled=True,
            auto_approve=False,
        )

        assert gate.pending("build-live-policy")
        decision = await gate.wait(approval.approval_id, timeout=0.01)
        assert decision is GateDecision.REJECTED

    asyncio.run(run())


# ---- worktree merge_back (delivered != empty) ----------------------------
def test_merge_back_delivers_files(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    wt = create_worktree(base, "demo")
    (Path(wt.dir) / "src").mkdir(parents=True, exist_ok=True)
    (Path(wt.dir) / "src" / "main.py").write_text("print('hi')\n")
    (Path(wt.dir) / "README.md").write_text("# demo\n")

    project = tmp_path / "delivered"
    copied = merge_back(wt.dir, project)
    assert "README.md" in copied
    assert (project / "src" / "main.py").exists()
    assert list_files(project)


# ---- proof_run anti-empty-scaffold ---------------------------------------
def test_proof_run_rejects_empty(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = proof_run(empty, checklist=["src/main.py"])
    assert res.passed is False


def test_proof_run_passes_real_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text("def main():\n    return 1\n")
    (proj / "README.md").write_text("# x\n")
    res = proof_run(proj, checklist=["README.md", "src/main.py"])
    assert res.passed is True
    assert res.files_substantive >= 1
    assert not res.syntax_errors


def test_proof_run_flags_syntax_error(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "broken.py").write_text("def x(:\n")
    res = proof_run(proj)
    assert res.syntax_errors
    assert res.passed is False


# ---- best-of-N selection -------------------------------------------------
def test_best_of_n_select_prefers_passing():
    class P:
        def __init__(self, passed, score, subs):
            self.passed = passed
            self.score = score
            self.files_substantive = subs

    class C:
        def __init__(self, idx, passed, score, subs, fw):
            self.index = idx
            self._p = P(passed, score, subs)
            self.proof = self._p
            self.files_written = fw

        @property
        def passed(self):
            return self._p.passed

        @property
        def rank_key(self):
            return (1 if self.passed else 0, self._p.score, self._p.files_substantive, self.files_written)

        def to_dict(self):
            return {"index": self.index}

    cands = [C(0, False, 90, 5, 5), C(1, True, 50, 2, 2), C(2, True, 80, 4, 4)]
    sel = bon.select(cands)
    assert sel.any_passed
    assert sel.winner.index == 2


def test_best_of_n_fallback_most_complete():
    # No proof set -> not passing; selector falls back to most files_written.
    import tempfile

    from skyn3t.studio.best_of_n import Candidate
    from skyn3t.worktree import create_worktree

    with tempfile.TemporaryDirectory() as d:
        a = Candidate(index=0, worktree=create_worktree(d, "a"))
        b = Candidate(index=1, worktree=create_worktree(d, "b"))
        a.files_written = 1
        b.files_written = 9
        sel = bon.select([a, b])
        assert not sel.any_passed
        assert sel.winner.index == 1


def test_best_of_n_seeds_every_candidate_with_prior_tests_and_assets(tmp_path):
    async def run():
        seed = tmp_path / "seed"
        acceptance = seed / "tests" / "test_acceptance_contract.py"
        asset = seed / "public" / "assets" / "generated.webp"
        acceptance.parent.mkdir(parents=True)
        asset.parent.mkdir(parents=True)
        acceptance.write_text("def test_contract():\n    assert True\n", encoding="utf-8")
        asset.write_bytes(b"real-generated-image")
        seen: list[int] = []

        async def trajectory(worktree, index):
            assert (worktree.path / acceptance.relative_to(seed)).read_text(
                encoding="utf-8"
            ).startswith("def test_contract")
            assert (worktree.path / asset.relative_to(seed)).read_bytes() == b"real-generated-image"
            source = worktree.path / "src" / "main.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"candidate = {index}\n", encoding="utf-8")
            seen.append(index)
            return TaskResult(
                task_id=f"candidate-{index}",
                success=True,
                output={"files_written": 1},
            )

        selection = await bon.sample(
            str(tmp_path / "Projects"),
            "seeded",
            2,
            trajectory,
            seed_dir=str(seed),
            checklist=["src/main.py"],
        )
        assert sorted(seen) == [0, 1]
        assert selection.winner is not None
        assert all(candidate.proof is not None for candidate in selection.candidates)
        assert (selection.winner.worktree.path / asset.relative_to(seed)).read_bytes() == (
            b"real-generated-image"
        )
        cleanup_worktree(selection.winner.worktree)

    asyncio.run(run())


# ---- manifest round-trip -------------------------------------------------
def test_manifest_roundtrip(tmp_path):
    m = BuildManifest(slug="x", brief="b", stack="python")
    m.add_stage(StageRecord(name="code", agent_type="code", capability="codegen", status="completed"))
    path = m.save(tmp_path / "proj")
    assert path.exists()
    loaded = BuildManifest.load(tmp_path / "proj")
    assert loaded is not None
    assert loaded.slug == "x"
    assert loaded.stages[0].name == "code"


# ---- end-to-end offline build -------------------------------------------
class _StubCodeAgent(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        wt = Path(task.payload["worktree_dir"])
        (wt / "src").mkdir(parents=True, exist_ok=True)
        (wt / "tests").mkdir(parents=True, exist_ok=True)
        # Deliver a complete project that satisfies the python checklist so the
        # objective proof-run legitimately passes (no reviewer-go shortcut).
        (wt / "src" / "main.py").write_text("def main():\n    return 42\n")
        (wt / "src" / "__init__.py").write_text("")
        (wt / "tests" / "test_basic.py").write_text("from src.main import main\n\ndef test_main():\n    assert main() == 42\n")
        (wt / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0.1.0'\n")
        (wt / "README.md").write_text("# generated\n\nA demo python tool.\n")
        return TaskResult(task_id=task.task_id, success=True, output={"files_written": 5, "worktree_dir": str(wt)})


class _StubReviewer(BaseAgent):
    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(task_id=task.task_id, success=True, output={"score": 88.0, "verdict": "go", "gaps": []})


class _BriefBlindReviewer(BaseAgent):
    """A reviewer that read the BRIEF and judged the delivery a no_go (the
    delivered tree is structurally complete but does not satisfy what was
    asked for — e.g. a Hello-world CLI for a "website" brief). The score is
    low to reflect poor brief-completeness."""

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"score": 20.0, "verdict": "no_go",
                    "gaps": ["delivery does not match the brief (wanted a website, got a CLI)"]},
        )


def test_final_build_status_marks_no_go_distinctly():
    from skyn3t.studio.runner import _final_build_status

    assert _final_build_status(True, "go") == "completed"
    assert _final_build_status(True, "no_go") == "completed_no_go"
    assert _final_build_status(False, "go") == "failed"
    assert _final_build_status(False, "no_go") == "failed"


def test_studio_runner_wires_security_and_self_healing(tmp_path):
    settings = Settings(
        projects_dir=tmp_path / "Projects",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        approval_gates=False,
    )
    bus = EventBus()
    orch = Orchestrator(bus)
    runner = StudioRunner(bus, orch, settings=settings, memory=None)

    assert runner.audit_log is not None
    assert runner.permission_manager is not None
    assert runner.permission_manager.audit_log is runner.audit_log
    assert runner.approval_gate.audit_log is runner.audit_log
    assert getattr(orch, "_self_healing", None) is not None


def test_studio_runner_end_to_end_offline(tmp_path):
    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        bus = EventBus()
        orch = Orchestrator(bus)

        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        rev = _StubReviewer("rev", "reviewer", "stub", bus)
        rev.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(rev)

        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start("Build a python tool", slug="demo")

        assert outcome.status == "completed"
        assert outcome.verdict == "go"
        assert outcome.files  # delivered != empty
        delivered = Path(outcome.project_dir)
        assert (delivered / "src" / "main.py").exists()
        assert (delivered / "skyn3t_manifest.json").exists()
        assert (delivered / ".skyn3t" / "product.json").exists()
        assert outcome.manifest["extra"]["build_graph"]["status"] == "succeeded"
        assert outcome.manifest["extra"]["product_spec"]["goal"] == (
            "Build a python tool"
        )
        assert outcome.manifest["extra"]["similarity_research"]["status"] == (
            "unavailable"
        )
        assert outcome.manifest["extra"]["similarity_research"]["error"] == (
            "GitHub similarity research is disabled"
        )
        assert "docker" in outcome.manifest["extra"]["lab_toolchain"]["checks"]
        # missing agents (brainstorm/research/etc) recorded as skipped, no crash.
        statuses = {s["name"]: s["status"] for s in outcome.manifest["stages"]}
        assert statuses["brainstorm"] == "skipped"
        assert statuses["code"] == "completed"
        # BUILD_COMPLETED emitted.
        assert any(e.type is EventType.BUILD_COMPLETED for e in bus.history())

    asyncio.run(run())


def test_source_product_contract_reaches_the_generation_prompt(tmp_path):
    async def run():
        from skyn3t.agents._common import knowledge_block
        from skyn3t.studio.product_spec import (
            BacklogRecord,
            ProductSpecV1,
            RequirementRecord,
        )

        class _CapturingCodeAgent(_StubCodeAgent):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.payload = None

            async def execute(self, task: TaskRequest) -> TaskResult:
                self.payload = task.payload
                return await super().execute(task)

        source = ProductSpecV1(
            project_id="source-contract",
            goal="Give dispatchers a dependable field-work view",
            personas=["dispatcher", "field technician"],
            requirements=[RequirementRecord(text="Show assigned work")],
            non_goals=["Do not auto-dispatch work"],
            architecture_decisions=["Keep offline state in a local adapter"],
            backlog=[
                BacklogRecord(
                    title="Explore route clustering",
                    source="github_research",
                )
            ],
        )
        edited = source.improve(
            {
                "requirements": [
                    RequirementRecord(
                        text="Show assigned work with explicit offline status",
                        source="user",
                    ).to_dict()
                ],
                "non_goals": ["Never dispatch work without confirmation"],
            },
            base_version=source.version,
            actor="studio-gui",
            reason="Clarify the field workflow",
        )
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _CapturingCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        reviewer = _StubReviewer("rev", "reviewer", "stub", bus)
        reviewer.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(reviewer)
        runner = StudioRunner(bus, orch, settings=settings, memory=None)

        outcome = await runner.start(
            "Rebuild the field-work coordinator",
            slug="contract-derivative",
            extra={
                "build_profile": "cheap_learned",
                "source_product_spec": edited.to_dict(),
            },
        )

        assert outcome.status in {"completed", "completed_no_go"}
        assert code.payload is not None
        prompt_context = knowledge_block(code.payload)
        assert "Show assigned work with explicit offline status" in prompt_context
        assert "Never dispatch work without confirmation" in prompt_context
        assert "OPTIONAL RESEARCH BACKLOG" in prompt_context
        assert outcome.manifest["extra"]["product_spec"]["version"] == edited.version
        assert outcome.manifest["extra"]["product_spec"]["requirements"] == [
            record.to_dict() for record in edited.requirements
        ]

    asyncio.run(run())


def test_lab_autonomy_toggle_after_runner_construction_applies_to_next_build(
    tmp_path,
):
    async def run():
        class _CountingGuard:
            def __init__(self):
                self.checks = 0

            def check(self):
                self.checks += 1

        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=True,
            lab_autonomy=False,
            best_of_n=1,
        )
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        reviewer = _StubReviewer("rev", "reviewer", "stub", bus)
        reviewer.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(reviewer)
        guard = _CountingGuard()
        runner = StudioRunner(
            bus,
            orch,
            settings=settings,
            memory=None,
            budget_guard=guard,
        )
        runner.stage_timeout = 0.01

        settings.lab_autonomy = True
        outcome = await runner.start(
            "Build a Python tool",
            slug="live-lab-policy",
            extra={
                "build_profile": "cheap_learned",
                "gated_stages": ("code",),
            },
        )

        assert guard.checks == 0
        assert outcome.status == "completed"
        assert outcome.manifest["extra"]["lab_autonomy"] is True
        assert runner.approval_gate.pending(outcome.build_id) == []

    asyncio.run(run())


def test_disabling_lab_autonomy_restores_live_guards_and_approval_gate(tmp_path):
    async def run():
        class _CountingGuard:
            def __init__(self):
                self.checks = 0

            def check(self):
                self.checks += 1

        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=True,
            cortex_auto_approve_safe=False,
            lab_autonomy=True,
            best_of_n=1,
        )
        bus = EventBus()
        guard = _CountingGuard()
        runner = StudioRunner(
            bus,
            Orchestrator(bus),
            settings=settings,
            memory=None,
            budget_guard=guard,
        )
        runner.stage_timeout = 0.01

        settings.lab_autonomy = False
        outcome = await runner.start(
            "Build a Python tool",
            slug="restored-standard-policy",
            extra={
                "build_profile": "cheap_learned",
                "gated_stages": ("code",),
            },
        )

        assert guard.checks > 0
        assert outcome.status == "failed"
        assert outcome.verdict == "no_go"
        assert outcome.manifest["extra"]["lab_autonomy"] is False

    asyncio.run(run())


def test_studio_runner_best_of_n(tmp_path):
    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=3,
        )
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        await orch.register(code)

        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start("Build a python tool", slug="bon", extra={"best_of_n": 3})
        assert outcome.status == "completed"
        assert (Path(outcome.project_dir) / "src" / "main.py").exists()
        evidence = outcome.manifest["extra"]["best_of_n"]
        assert evidence["candidate_count"] == 3
        assert evidence["any_proof_passed"] is True
        assert sum(candidate["selected"] for candidate in evidence["candidates"]) == 1
        code_stage = next(
            stage for stage in outcome.manifest["stages"] if stage["name"] == "code"
        )
        assert code_stage["output_summary"]["best_of_n"] == evidence
        on_disk = BuildManifest.load(outcome.project_dir)
        assert on_disk is not None and on_disk.extra["best_of_n"] == evidence
        completed = [event for event in bus.history() if event.type is EventType.BUILD_COMPLETED]
        live_evidence = completed[-1].payload["best_of_n"]
        assert live_evidence["winner_index"] == evidence["winner_index"]
        assert live_evidence["candidate_count"] == 3
        assert len(live_evidence["candidates"]) == 3

    asyncio.run(run())


def test_brief_aware_no_go_survives_structural_rescore(tmp_path):
    """Regression (fix/swarm-intent-scoring): a hollow build must NOT score go.

    The reviewer STAGE produced a BRIEF-AWARE no_go (the delivery doesn't match
    what was asked for), while the delivered tree is structurally complete so the
    blind structural rescore (entrypoint + >=5 source files + manifest + parseable
    config) would say "go". The final verdict must stay no_go — the structural
    rescore must not overwrite the brief-aware signal.
    """
    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        bus = EventBus()
        orch = Orchestrator(bus)

        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        rev = _BriefBlindReviewer("rev", "reviewer", "stub", bus)
        rev.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(rev)

        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start("Build a marketing website", slug="hollow")

        # The structural rescore WOULD have said go (complete tree); confirm it.
        assert outcome.manifest["extra"]["rescore"]["verdict"] == "go"
        # ...but the brief-aware no_go is honoured -> final verdict no_go.
        assert outcome.verdict == "no_go"
        assert outcome.status == "completed_no_go"
        # Score must not have ratcheted up to the structural reading.
        assert outcome.score < 60.0

    asyncio.run(run())


def test_brief_aware_go_with_good_structure_stays_go(tmp_path):
    """Converse: a reviewer go (brief-aware) + structurally-sound delivery that
    passes its proof must remain go — the fix must not regress legit builds."""
    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects",
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            critic_enabled=False,
            approval_gates=False,
            best_of_n=1,
        )
        bus = EventBus()
        orch = Orchestrator(bus)

        code = _StubCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        rev = _StubReviewer("rev", "reviewer", "stub", bus)
        rev.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(rev)

        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start("Build a python tool", slug="legit")

        assert outcome.manifest["extra"]["rescore"]["verdict"] == "go"
        assert outcome.verdict == "go"
        assert outcome.status == "completed"

    asyncio.run(run())


class _DegradedCodeAgent(BaseAgent):
    """A real (claude_cli) backend that delivers a SUBSTANTIAL, building project
    but flags itself ``degraded`` (it under-delivered / fell back to scaffold).
    The build must NOT score 'go' despite the substantial delivery + go reviewer."""

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        wt = Path(task.payload["worktree_dir"])
        (wt / "src").mkdir(parents=True, exist_ok=True)
        (wt / "tests").mkdir(parents=True, exist_ok=True)
        big = "def main():\n    return 42\n\n\n" + "\n\n".join(
            f"def feature_{i}(x):\n    \"\"\"feature {i}.\"\"\"\n    return x * {i} + {i}\n"
            for i in range(40))
        (wt / "src" / "main.py").write_text(big)
        (wt / "src" / "__init__.py").write_text("")
        (wt / "tests" / "test_basic.py").write_text(
            "from src.main import main\n\ndef test_main():\n    assert main() == 42\n")
        (wt / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1.0'\n")
        (wt / "README.md").write_text("# generated\n\nA demo python tool.\n")
        return TaskResult(task_id=task.task_id, success=True, output={
            "files_written": 5, "worktree_dir": str(wt), "backend": "claude_cli",
            "degraded": True,
            "degraded_reason": "agentic build under-delivered after 1 retry"})


def test_degraded_code_stage_forces_no_go(tmp_path):
    """A substantial, building delivery from a REAL backend that flagged itself
    degraded must be no_go (the learning loop must not reward under-delivery)."""
    async def run():
        settings = Settings(
            projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs", critic_enabled=False, approval_gates=False,
            best_of_n=1)
        bus = EventBus()
        orch = Orchestrator(bus)
        code = _DegradedCodeAgent("coder", "code", "stub", bus)
        code.add_capability(AgentCapability("codegen"))
        rev = _StubReviewer("rev", "reviewer", "stub", bus)  # says go/88
        rev.add_capability(AgentCapability("review"))
        await orch.register(code)
        await orch.register(rev)

        runner = StudioRunner(bus, orch, settings=settings, memory=None)
        outcome = await runner.start("Build a python tool", slug="degraded")

        assert outcome.verdict == "no_go", "degraded delivery must not score go"
        assert outcome.status == "completed_no_go"
        assert "degraded_gate" in outcome.manifest["extra"]

    asyncio.run(run())
