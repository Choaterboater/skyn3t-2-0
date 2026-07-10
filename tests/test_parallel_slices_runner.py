"""Runner orchestration for parallel code slices (Hermes orchestrator-worker).

Gating (`_maybe_slices`) plus the fan-out/merge in `_run_code_parallel_slices`:
each slice agent writes into its own worktree and ALL slices are merged into the
main worktree (config last). `_submit_stage` is stubbed to simulate the scoped
slice agents without launching real codegen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.planner import BuildPlan
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.stages import StageSpec
from skyn3t.worktree import create_worktree, list_files

_ARCH_FILES = [
    {"path": "src/App.jsx"}, {"path": "src/main.jsx"}, {"path": "src/components/Card.jsx"},
    {"path": "src/index.css"}, {"path": "index.html"}, {"path": "package.json"},
    {"path": "api/main.py"}, {"path": "api/routes/users.py"}, {"path": "tests/test_users.py"},
]


def _runner(tmp_path, **kw):
    bus = EventBus()
    settings = Settings(projects_dir=tmp_path / "P", data_dir=tmp_path / "d",
                        logs_dir=tmp_path / "l", critic_enabled=False, **kw)
    return StudioRunner(bus, Orchestrator(bus), settings=settings, memory=None)


def _plan(**kw):
    base = dict(slug="demo", brief="a fullstack app", stack="react", stages=[],
                checklist=["package.json", "src/App.jsx"], best_of_n=1)
    base.update(kw)
    return BuildPlan(**base)


_CODE_SPEC = StageSpec(name="code", agent_type="code", capability="codegen")


class _TierResolvingAgenticLLM:
    backend = "openrouter"
    supports_agentic = True

    def __init__(self) -> None:
        self.resolved: list[tuple[str, tuple[str, ...], str]] = []

    def _resolve_pinned_model(self, *, tier, setting_names=(), task_type="", **kwargs):
        self.resolved.append((tier.value, tuple(setting_names), task_type))
        return {
            "ui": "resolved/ui-model",
            "strong": "resolved/strong-model",
            "cheap": "resolved/cheap-model",
        }[tier.value]


def test_maybe_slices_gated_off_by_default(tmp_path):
    r = _runner(tmp_path)  # flag defaults False
    assert r._maybe_slices(_plan(), {"architect": {"plan": {"files": _ARCH_FILES}}}) is None


def test_maybe_slices_off_when_best_of_n(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    assert r._maybe_slices(_plan(best_of_n=3),
                           {"architect": {"plan": {"files": _ARCH_FILES}}}) is None


def test_maybe_slices_on_with_flag_and_enough_files(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    slices = r._maybe_slices(_plan(), {"architect": {"plan": {"files": _ARCH_FILES}}})
    assert slices is not None
    assert {"frontend", "backend", "tests", "config"} <= set(slices)
    assert list(slices)[-1] == "config"  # config merges last


def test_maybe_slices_accepts_per_build_profile_override(tmp_path):
    r = _runner(tmp_path)  # global setting remains off
    slices = r._maybe_slices(
        _plan(),
        {"architect": {"plan": {"files": _ARCH_FILES}}},
        {"parallel_code_slices": True},
    )
    assert slices is not None
    assert {"frontend", "backend", "tests", "config"} <= set(slices)


def test_full_app_enables_semantic_frontend_specialists(tmp_path):
    r = _runner(tmp_path)
    files = [
        {"path": "src/data/site.ts"},
        {"path": "src/components/Card.astro"},
        {"path": "src/layouts/PageLayout.astro"},
        {"path": "src/pages/index.astro"},
        {"path": "src/pages/lessons.astro"},
        {"path": "src/styles/global.css"},
        {"path": "src/lib/navigation.ts"},
        {"path": "package.json"},
    ]
    prior = {"architect": {"plan": {"files": files}}}

    regular = r._maybe_slices(
        _plan(stack="astro"),
        prior,
        {"parallel_code_slices": True, "parallel_code_slices_min_files": 4},
    )
    full_app = r._maybe_slices(
        _plan(stack="astro"),
        prior,
        {
            "parallel_code_slices": True,
            "parallel_code_slices_min_files": 4,
            "full_app_contract": True,
        },
    )

    assert regular is not None and set(regular) == {"frontend", "config"}
    assert full_app is not None
    assert list(full_app) == [
        "frontend_content",
        "frontend_components",
        "frontend_pages",
        "frontend_styles",
        "frontend_core",
        "config",
    ]
    assert sum(len(entries) for entries in full_app.values()) == len(files)


def test_per_build_slice_floor_activates_realistic_four_file_full_app(tmp_path):
    r = _runner(tmp_path, parallel_code_slices_min_files=8)
    prior = {"architect": {"plan": {"files": [
        {"path": "src/pages/index.astro"},
        {"path": "src/pages/lessons.astro"},
        {"path": "src/pages/api/bookings.ts"},
        {"path": "package.json"},
    ]}}}

    assert r._maybe_slices(
        _plan(stack="astro"), prior, {"parallel_code_slices": True}
    ) is None
    slices = r._maybe_slices(
        _plan(stack="astro"),
        prior,
        {
            "parallel_code_slices": True,
            "parallel_code_slices_min_files": 4,
        },
    )

    assert slices is not None
    assert set(slices) == {"frontend", "backend", "config"}


def test_run_parallel_slices_merges_every_slice(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    main_wt = create_worktree(str(r.settings.projects_dir), "demo")
    worktrees = [main_wt]
    plan = _plan()
    prior = {"architect": {"plan": {"files": _ARCH_FILES}}}
    slices = r._maybe_slices(plan, prior)
    asset = Path(main_wt.dir) / "public" / "assets" / "generated.webp"
    acceptance = Path(main_wt.dir) / "tests" / "test_acceptance_contract.py"
    asset.parent.mkdir(parents=True)
    acceptance.parent.mkdir(parents=True)
    asset.write_bytes(b"RIFF\x10\x00\x00\x00WEBPreal-generated-photo")
    acceptance.write_text("def test_contract():\n    assert True\n", encoding="utf-8")

    captured: dict = {}

    async def fake_submit(spec, payload, cid):
        # Simulate a scoped slice agent writing exactly its files into its wt.
        wt = Path(payload["worktree_dir"])
        sc = payload["slice_scope"]
        assert (wt / "public/assets/generated.webp").read_bytes() == asset.read_bytes()
        assert (wt / "tests/test_acceptance_contract.py").is_file()
        for rel in sc["files"]:
            p = wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"// {rel}\n", encoding="utf-8")
        captured.setdefault(sc["name"], payload)
        return TaskResult(task_id="x", success=True,
                          output={"files_written": len(sc["files"]), "slice": sc["name"]})

    r._submit_stage = fake_submit  # type: ignore[assignment]

    project_dir = str(r.settings.projects_dir / "demo")
    result = asyncio.run(r._run_code_parallel_slices(
        plan, _CODE_SPEC, project_dir, prior, [], {}, "cid", main_wt, worktrees, slices))

    assert result.success
    merged = set(list_files(main_wt.dir))
    # Files from every slice landed in the main worktree.
    assert {"src/App.jsx", "api/main.py", "tests/test_users.py", "package.json"} <= merged
    assert (Path(main_wt.dir) / "public/assets/generated.webp").read_bytes() == asset.read_bytes()
    assert (Path(main_wt.dir) / "tests/test_acceptance_contract.py").is_file()
    assert result.output["files_written"] >= len(_ARCH_FILES)
    assert set(result.metadata["parallel_slices"]["slices"]) == set(slices)
    # Each slice agent received the full manifest as cross-slice context.
    assert "api/main.py" in captured["frontend"]["slice_scope"]["manifest"]
    snapshots = [
        event for event in r.event_bus.history()
        if event.type is EventType.STAGE_ARTIFACT_SNAPSHOT
    ]
    assert {event.payload["slice"] for event in snapshots} == set(slices)


def test_summarize_keeps_codegen_override_unavailable() -> None:
    summary = StudioRunner._summarize({
        "files_written": 3,
        "codegen_override_unavailable": "claude",
        "debug_payload": {"large": "ignored"},
    })

    assert summary["files_written"] == 3
    assert summary["codegen_override_unavailable"] == "claude"


def test_run_parallel_slices_aggregates_codegen_override_unavailable(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    main_wt = create_worktree(str(r.settings.projects_dir), "demo")
    plan = _plan()
    prior = {"architect": {"plan": {"files": _ARCH_FILES}}}
    slices = r._maybe_slices(plan, prior)
    unavailable = {
        "frontend": "claude",
        "backend": "codex",
        "tests": "claude",
        "config": "",
    }

    async def fake_submit(spec, payload, cid):
        wt = Path(payload["worktree_dir"])
        sc = payload["slice_scope"]
        for rel in sc["files"]:
            p = wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"// {rel}\n", encoding="utf-8")
        value = unavailable.get(sc["name"], "")
        output = {"files_written": len(sc["files"]), "slice": sc["name"]}
        if value:
            output["codegen_override_unavailable"] = value
        return TaskResult(task_id="x", success=True, output=output)

    r._submit_stage = fake_submit  # type: ignore[assignment]

    result = asyncio.run(r._run_code_parallel_slices(
        plan, _CODE_SPEC, str(r.settings.projects_dir / "demo"),
        prior, [], {}, "cid", main_wt, [main_wt], slices))

    assert result.output["codegen_override_unavailable"] == "claude, codex"


def test_run_parallel_slices_scopes_each_agent_to_its_files(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True)
    main_wt = create_worktree(str(r.settings.projects_dir), "demo")
    plan = _plan()
    prior = {"architect": {"plan": {"files": _ARCH_FILES}}}
    slices = r._maybe_slices(plan, prior)
    seen_scopes: dict = {}

    async def fake_submit(spec, payload, cid):
        sc = payload["slice_scope"]
        seen_scopes[sc["name"]] = set(sc["files"])
        # The scoped plan must only carry this slice's files.
        assert {f["path"] for f in payload["plan"]["files"]} == set(sc["files"])
        return TaskResult(task_id="x", success=True, output={"slice": sc["name"]})

    r._submit_stage = fake_submit  # type: ignore[assignment]
    result = asyncio.run(r._run_code_parallel_slices(
        plan, _CODE_SPEC, str(r.settings.projects_dir / "demo"),
        prior, [], {}, "cid", main_wt, [main_wt], slices))

    assert "api/main.py" in seen_scopes["backend"]
    assert "api/main.py" not in seen_scopes["frontend"]
    assert "src/App.jsx" in seen_scopes["frontend"]
    assert result.output["degraded"] is True
    assert all(f"{name}:" in result.output["degraded_reason"] for name in slices)


def test_openrouter_agentic_slices_resolve_each_slice_tier_model(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True, llm_backend="openrouter")
    main_wt = create_worktree(str(r.settings.projects_dir), "demo")
    plan = _plan()
    prior = {"architect": {"plan": {"files": _ARCH_FILES}}}
    slices = r._maybe_slices(plan, prior)
    llm = _TierResolvingAgenticLLM()
    r._registered_codegen_agent = lambda: SimpleNamespace(llm=llm)  # type: ignore[method-assign]
    captured: dict[str, str] = {}

    async def fake_submit(spec, payload, cid):
        scope = payload["slice_scope"]
        captured[scope["name"]] = payload.get("model_override", "")
        target = Path(payload["worktree_dir"]) / scope["files"][0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// slice\n", encoding="utf-8")
        return TaskResult(
            task_id="x",
            success=True,
            output={"files_written": 1, "slice": scope["name"]},
        )

    r._submit_stage = fake_submit  # type: ignore[assignment]
    asyncio.run(r._run_code_parallel_slices(
        plan, _CODE_SPEC, str(r.settings.projects_dir / "demo"),
        prior, [], {}, "cid", main_wt, [main_wt], slices,
    ))

    assert captured == {
        "frontend": "resolved/ui-model",
        "backend": "resolved/strong-model",
        "tests": "resolved/cheap-model",
        "config": "resolved/cheap-model",
    }
    assert [tier for tier, _pins, _task in llm.resolved].count("cheap") == 2
    assert all(
        pins == ("openrouter_codegen_model", "preferred_model") and task == "codegen"
        for _tier, pins, task in llm.resolved
    )


def test_manual_build_model_override_wins_for_every_agentic_slice(tmp_path):
    r = _runner(tmp_path, parallel_code_slices=True, llm_backend="openrouter")
    main_wt = create_worktree(str(r.settings.projects_dir), "demo")
    plan = _plan()
    prior = {"architect": {"plan": {"files": _ARCH_FILES}}}
    slices = r._maybe_slices(plan, prior)
    llm = _TierResolvingAgenticLLM()
    r._registered_codegen_agent = lambda: SimpleNamespace(llm=llm)  # type: ignore[method-assign]
    captured: dict[str, str] = {}

    async def fake_submit(spec, payload, cid):
        scope = payload["slice_scope"]
        captured[scope["name"]] = payload.get("model_override", "")
        target = Path(payload["worktree_dir"]) / scope["files"][0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// slice\n", encoding="utf-8")
        return TaskResult(
            task_id="x",
            success=True,
            output={"files_written": 1, "slice": scope["name"]},
        )

    r._submit_stage = fake_submit  # type: ignore[assignment]
    asyncio.run(r._run_code_parallel_slices(
        plan, _CODE_SPEC, str(r.settings.projects_dir / "demo"),
        prior, [], {"model_override": "manual/build-model"},
        "cid", main_wt, [main_wt], slices,
    ))

    assert captured == {name: "manual/build-model" for name in slices}
    assert llm.resolved == [], "manual build pin must bypass slice-tier resolution"


def test_local_cli_slices_are_only_pinned_by_explicit_mapping(tmp_path):
    local_cli = SimpleNamespace(backend="claude_cli", supports_agentic=True)
    assert _runner(tmp_path)._slice_model("ui", local_cli) is None
    mapped = _runner(
        tmp_path,
        slice_tier_models={"ui": "claude-explicit-ui"},
    )
    assert mapped._slice_model("ui", local_cli) == "claude-explicit-ui"
