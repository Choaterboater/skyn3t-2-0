from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from skyn3t.studio.graph_runtime import (
    ArtifactRef,
    DynamicSpecialistSubgraph,
    EvidenceBundle,
    GraphDefinition,
    GraphExecutor,
    GraphNodeSpec,
    GraphRun,
    GraphStore,
    NodeAttempt,
    NodeContext,
    NodeResult,
    NodeStatus,
    RoutingSnapshot,
    node_cache_key,
)


def _basic_graph() -> GraphDefinition:
    return GraphDefinition(
        graph_id="build-app",
        version="3",
        nodes=(
            GraphNodeSpec(
                id="plan",
                kind="planner",
                input_schema="BuildBrief",
                output_schema="BuildPlan",
                cacheable=True,
            ),
            GraphNodeSpec(
                id="code",
                kind="codegen",
                deps=("plan",),
                input_schema="BuildPlan",
                output_schema="Workspace",
                mutates_workspace=True,
                write_set=("src",),
                max_retries=2,
                cacheable=False,
            ),
        ),
    )


def test_node_status_has_the_durable_lifecycle_values() -> None:
    assert {status.value for status in NodeStatus} == {
        "pending",
        "ready",
        "running",
        "succeeded",
        "failed",
        "blocked",
        "cancelled",
        "cached",
    }


def test_graph_definition_validates_dependencies_and_has_stable_topology() -> None:
    graph = _basic_graph()

    assert graph.node("code").deps == ("plan",)
    assert [node.id for node in graph.topological_nodes()] == ["plan", "code"]

    with pytest.raises(ValueError, match="duplicate node id"):
        GraphDefinition(
            graph_id="duplicate",
            nodes=(
                GraphNodeSpec(id="same", kind="one"),
                GraphNodeSpec(id="same", kind="two"),
            ),
        )

    with pytest.raises(ValueError, match="unknown dependency"):
        GraphDefinition(
            graph_id="missing",
            nodes=(GraphNodeSpec(id="code", kind="codegen", deps=("plan",)),),
        )

    with pytest.raises(ValueError, match="cycle"):
        GraphDefinition(
            graph_id="cycle",
            nodes=(
                GraphNodeSpec(id="one", kind="step", deps=("two",)),
                GraphNodeSpec(id="two", kind="step", deps=("one",)),
            ),
        )


def test_node_cache_key_is_canonical_and_covers_every_execution_input() -> None:
    graph = _basic_graph()
    node = graph.node("plan")
    routing = RoutingSnapshot(
        backend="openrouter",
        model="example/model",
        profile="balanced",
        parameters={"temperature": 0.2, "limits": {"tokens": 2000}},
    )
    kwargs = {
        "graph": graph,
        "node": node,
        "upstream_outputs": {"research": {"b": 2, "a": 1}},
        "toolchain": {"python": "3.13", "node": "22"},
        "prompt": {"system": "build carefully", "version": 4},
        "routing": routing,
        "inputs": {"brief": "build a useful app"},
    }

    first = node_cache_key(**kwargs)
    reordered = node_cache_key(
        **{
            **kwargs,
            "upstream_outputs": {"research": {"a": 1, "b": 2}},
            "toolchain": {"node": "22", "python": "3.13"},
        }
    )

    assert first == reordered
    assert len(first) == 64

    variants = (
        {**kwargs, "graph": replace(graph, version="4")},
        {**kwargs, "node": replace(node, kind="researcher")},
        {**kwargs, "upstream_outputs": {"research": {"a": 9, "b": 2}}},
        {**kwargs, "toolchain": {"python": "3.12", "node": "22"}},
        {**kwargs, "prompt": {"system": "different", "version": 4}},
        {
            **kwargs,
            "routing": replace(routing, model="another/model"),
        },
        {**kwargs, "inputs": {"brief": "build a different app"}},
    )
    assert all(node_cache_key(**variant) != first for variant in variants)


@pytest.mark.asyncio
async def test_graph_store_round_trips_run_attempt_artifact_status_and_output(
    tmp_path,
) -> None:
    database = tmp_path / "graphs.sqlite3"
    store = GraphStore(database)
    await asyncio.gather(store.initialize(), store.initialize())
    graph = _basic_graph()
    routing = RoutingSnapshot(backend="stub", model="deterministic")
    run = GraphRun(
        run_id="run-roundtrip",
        graph=graph,
        inputs={"brief": "make an app"},
        routing=routing,
    )
    artifact = ArtifactRef(
        artifact_id="artifact-plan",
        run_id=run.run_id,
        node_id="plan",
        kind="json",
        uri="workspace://build-plan.json",
        digest="sha256:abc123",
        metadata={"schema": "BuildPlan"},
    )
    evidence = EvidenceBundle(
        facts={"proof": "schema-valid"},
        artifacts=(artifact,),
    )
    attempt = NodeAttempt(
        run_id=run.run_id,
        node_id="plan",
        attempt=1,
        status=NodeStatus.SUCCEEDED,
        cache_key="cache-plan",
        routing=routing,
        output={"files": ["src/main.py"]},
        evidence=evidence,
    )

    await store.save_run(run)
    await store.set_run_status(run.run_id, NodeStatus.RUNNING)
    await store.set_node_status(run.run_id, "plan", NodeStatus.READY)
    await store.set_node_status(run.run_id, "plan", NodeStatus.RUNNING)
    await store.save_attempt(attempt)
    await store.save_artifacts(evidence.artifacts)
    await store.set_node_status(
        run.run_id,
        "plan",
        NodeStatus.SUCCEEDED,
        output=attempt.output,
        cache_key=attempt.cache_key,
    )

    loaded = await store.load_run(run.run_id)

    assert loaded is not None
    assert loaded.graph == graph
    assert loaded.inputs == {"brief": "make an app"}
    assert loaded.routing == routing
    assert loaded.status is NodeStatus.RUNNING
    assert loaded.node_statuses["plan"] is NodeStatus.SUCCEEDED
    assert loaded.node_outputs["plan"] == {"files": ["src/main.py"]}
    assert loaded.attempts == (attempt,)
    assert loaded.artifacts == (artifact,)
    assert loaded.node_statuses["code"] is NodeStatus.PENDING

    await store.close()
    reopened = GraphStore(database)
    await reopened.initialize()
    assert await reopened.load_run(run.run_id) == loaded
    await reopened.close()


@pytest.mark.asyncio
async def test_graph_store_recovers_only_interrupted_running_nodes(tmp_path) -> None:
    store = GraphStore(tmp_path / "recovery.sqlite3")
    await store.initialize()
    graph = _basic_graph()
    run = GraphRun(run_id="run-recover", graph=graph)
    await store.save_run(run)
    await store.set_run_status(run.run_id, NodeStatus.RUNNING)
    await store.set_node_status(
        run.run_id,
        "plan",
        NodeStatus.SUCCEEDED,
        output={"plan": "preserve-me"},
    )
    await store.set_node_status(run.run_id, "code", NodeStatus.RUNNING)
    await store.save_attempt(
        NodeAttempt(
            run_id=run.run_id,
            node_id="code",
            attempt=1,
            status=NodeStatus.RUNNING,
        )
    )

    recovered = await store.recover_run(run.run_id)

    assert recovered is not None
    assert recovered.status is NodeStatus.READY
    assert recovered.node_statuses == {
        "plan": NodeStatus.SUCCEEDED,
        "code": NodeStatus.READY,
    }
    assert recovered.node_outputs["plan"] == {"plan": "preserve-me"}
    assert recovered.attempts[0].status is NodeStatus.FAILED
    assert recovered.attempts[0].error == "interrupted before completion"
    assert recovered.attempts[0].finished_at is not None
    await store.close()


@pytest.mark.asyncio
async def test_graph_store_commits_node_success_atomically_across_crash_window(
    tmp_path,
) -> None:
    store = GraphStore(tmp_path / "atomic-success.sqlite3")
    graph = GraphDefinition(
        graph_id="atomic-success",
        nodes=(GraphNodeSpec(id="mutate", kind="mutate"),),
    )
    run = GraphRun(run_id="atomic-run", graph=graph)
    await store.save_run(run)
    await store.set_run_status(run.run_id, NodeStatus.RUNNING)
    await store.set_node_status(run.run_id, "mutate", NodeStatus.RUNNING)
    await store.save_attempt(
        NodeAttempt(
            run_id=run.run_id,
            node_id="mutate",
            attempt=1,
            status=NodeStatus.RUNNING,
            cache_key="atomic-cache",
        )
    )
    artifact = ArtifactRef(
        artifact_id="atomic-artifact",
        run_id=run.run_id,
        node_id="mutate",
        kind="report",
        uri="artifact://atomic-report",
    )
    succeeded = NodeAttempt(
        run_id=run.run_id,
        node_id="mutate",
        attempt=1,
        status=NodeStatus.SUCCEEDED,
        cache_key="atomic-cache",
        output={"mutated": True},
        evidence=EvidenceBundle(
            facts={"verified": True},
            artifacts=(artifact,),
        ),
    )

    await store._write(
        lambda connection: connection.execute(
            """
            CREATE TRIGGER fail_success_state
            BEFORE UPDATE OF status ON graph_node_states
            WHEN NEW.status = 'succeeded'
            BEGIN
                SELECT RAISE(ABORT, 'simulated crash window');
            END
            """
        )
    )

    with pytest.raises(Exception, match="simulated crash window"):
        await store.commit_node_success(succeeded, cacheable=True)

    loaded = await store.load_run(run.run_id)
    assert loaded is not None
    assert loaded.node_statuses["mutate"] is NodeStatus.RUNNING
    assert [attempt.status for attempt in loaded.attempts] == [NodeStatus.RUNNING]
    assert loaded.artifacts == ()
    assert await store.get_cached_result("atomic-cache") is None
    await store.close()


@pytest.mark.asyncio
async def test_graph_store_persists_node_cache_entries(tmp_path) -> None:
    store = GraphStore(tmp_path / "cache.sqlite3")
    await store.initialize()
    result = NodeResult(
        output={"answer": 42},
        evidence=EvidenceBundle(facts={"verified": True}),
    )

    assert await store.get_cached_result("missing") is None
    await store.put_cached_result("stable-key", result)

    assert await store.get_cached_result("stable-key") == result
    await store.close()


@pytest.mark.asyncio
async def test_executor_runs_dependency_ready_nodes_concurrently_then_joins(
    tmp_path,
) -> None:
    graph = GraphDefinition(
        graph_id="parallel",
        nodes=(
            GraphNodeSpec(id="left", kind="leaf"),
            GraphNodeSpec(id="right", kind="leaf"),
            GraphNodeSpec(id="join", kind="join", deps=("left", "right")),
        ),
    )
    active = 0
    max_active = 0
    both_started = asyncio.Event()
    join_inputs = {}

    async def leaf(context: NodeContext):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active -= 1
        return {"node": context.node.id}

    async def join(context: NodeContext):
        join_inputs.update(context.upstream_outputs)
        return {"joined": sorted(context.upstream_outputs)}

    store = GraphStore(tmp_path / "parallel.sqlite3")
    executor = GraphExecutor(
        store,
        handlers={"leaf": leaf, "join": join},
        max_concurrency=4,
    )

    run = await executor.execute(graph, run_id="parallel-run")

    assert run.status is NodeStatus.SUCCEEDED
    assert set(run.node_statuses.values()) == {NodeStatus.SUCCEEDED}
    assert max_active == 2
    assert join_inputs == {
        "left": {"node": "left"},
        "right": {"node": "right"},
    }
    await store.close()


@pytest.mark.asyncio
async def test_executor_offloads_synchronous_handlers_from_event_loop(tmp_path) -> None:
    graph = GraphDefinition(
        graph_id="sync-handler",
        nodes=(GraphNodeSpec(id="blocking", kind="blocking"),),
    )
    loop_progressed = asyncio.Event()

    def blocking(_context: NodeContext):
        time.sleep(0.08)
        return {"loop_progressed": loop_progressed.is_set()}

    async def ticker() -> None:
        await asyncio.sleep(0.01)
        loop_progressed.set()

    store = GraphStore(tmp_path / "sync-handler.sqlite3")
    executor = GraphExecutor(store, handlers={"blocking": blocking})
    tick = asyncio.create_task(ticker())

    run = await executor.execute(graph, run_id="sync-handler-run")

    await tick
    assert loop_progressed.is_set()
    assert run.node_outputs["blocking"] == {"loop_progressed": True}
    await store.close()


@pytest.mark.asyncio
async def test_executor_serializes_overlapping_write_sets_but_not_disjoint_ones(
    tmp_path,
) -> None:
    graph = GraphDefinition(
        graph_id="writes",
        nodes=(
            GraphNodeSpec(
                id="source",
                kind="write",
                mutates_workspace=True,
                write_set=("src",),
                cacheable=False,
            ),
            GraphNodeSpec(
                id="component",
                kind="write",
                mutates_workspace=True,
                write_set=("src/components/Button.tsx",),
                cacheable=False,
            ),
            GraphNodeSpec(
                id="tests",
                kind="write",
                mutates_workspace=True,
                write_set=("tests",),
                cacheable=False,
            ),
        ),
    )
    active: set[str] = set()
    forbidden_overlaps: list[frozenset[str]] = []
    max_active = 0

    async def write(context: NodeContext):
        nonlocal max_active
        pair = frozenset({context.node.id, "source"})
        if context.node.id == "component" and "source" in active:
            forbidden_overlaps.append(pair)
        if context.node.id == "source" and "component" in active:
            forbidden_overlaps.append(frozenset({"source", "component"}))
        active.add(context.node.id)
        max_active = max(max_active, len(active))
        await asyncio.sleep(0.03)
        active.remove(context.node.id)
        return context.node.id

    store = GraphStore(tmp_path / "writes.sqlite3")
    executor = GraphExecutor(store, handlers={"write": write}, max_concurrency=3)

    run = await executor.execute(graph, run_id="write-run")

    assert run.status is NodeStatus.SUCCEEDED
    assert forbidden_overlaps == []
    assert max_active == 2
    await store.close()


@pytest.mark.asyncio
async def test_executor_retries_and_reuses_only_cacheable_nodes(tmp_path) -> None:
    graph = GraphDefinition(
        graph_id="cache-and-retry",
        nodes=(
            GraphNodeSpec(
                id="discover",
                kind="discover",
                max_retries=1,
                cacheable=True,
            ),
            GraphNodeSpec(
                id="package",
                kind="package",
                deps=("discover",),
                cacheable=False,
            ),
        ),
    )
    calls = {"discover": 0, "package": 0}

    async def discover(_context: NodeContext):
        calls["discover"] += 1
        if calls["discover"] == 1:
            raise RuntimeError("transient provider error")
        return NodeResult(
            output={"patterns": ["durable-graph"]},
            evidence=EvidenceBundle(facts={"provider": "test"}),
        )

    async def package(context: NodeContext):
        calls["package"] += 1
        return {"source": context.upstream_outputs["discover"]}

    store = GraphStore(tmp_path / "cache-executor.sqlite3")
    executor = GraphExecutor(
        store,
        handlers={"discover": discover, "package": package},
    )

    first = await executor.execute(
        graph,
        run_id="cache-first",
        inputs={"brief": "same"},
        toolchain={"python": "3.13"},
        prompt={"version": 1},
    )
    second = await executor.execute(
        graph,
        run_id="cache-second",
        inputs={"brief": "same"},
        toolchain={"python": "3.13"},
        prompt={"version": 1},
    )

    assert first.status is NodeStatus.SUCCEEDED
    assert [attempt.status for attempt in first.attempts if attempt.node_id == "discover"] == [
        NodeStatus.FAILED,
        NodeStatus.SUCCEEDED,
    ]
    assert second.node_statuses == {
        "discover": NodeStatus.CACHED,
        "package": NodeStatus.SUCCEEDED,
    }
    assert second.node_outputs == first.node_outputs
    assert calls == {"discover": 2, "package": 2}
    await store.close()


@pytest.mark.asyncio
async def test_executor_cancellation_is_persisted_and_stops_dependents(tmp_path) -> None:
    graph = GraphDefinition(
        graph_id="cancel",
        nodes=(
            GraphNodeSpec(id="slow", kind="slow", cacheable=False),
            GraphNodeSpec(
                id="after",
                kind="after",
                deps=("slow",),
                cacheable=False,
            ),
        ),
    )
    started = asyncio.Event()
    after_called = False

    async def slow(_context: NodeContext):
        started.set()
        await asyncio.Event().wait()

    async def after(_context: NodeContext):
        nonlocal after_called
        after_called = True

    store = GraphStore(tmp_path / "cancel.sqlite3")
    executor = GraphExecutor(
        store,
        handlers={"slow": slow, "after": after},
    )
    task = asyncio.create_task(executor.execute(graph, run_id="cancel-run"))
    await asyncio.wait_for(started.wait(), timeout=1)

    await executor.cancel("cancel-run")
    run = await asyncio.wait_for(task, timeout=1)

    assert run.status is NodeStatus.CANCELLED
    assert run.node_statuses == {
        "slow": NodeStatus.CANCELLED,
        "after": NodeStatus.CANCELLED,
    }
    assert after_called is False
    persisted = await store.load_run(run.run_id)
    assert persisted is not None
    assert persisted.status is NodeStatus.CANCELLED
    assert persisted.attempts[-1].status is NodeStatus.CANCELLED
    await store.close()


@pytest.mark.asyncio
async def test_executor_resume_keeps_succeeded_output_and_restarts_interrupted_node(
    tmp_path,
) -> None:
    graph = GraphDefinition(
        graph_id="resume",
        nodes=(
            GraphNodeSpec(id="done", kind="done", cacheable=False),
            GraphNodeSpec(
                id="interrupted",
                kind="work",
                deps=("done",),
                cacheable=False,
            ),
            GraphNodeSpec(
                id="after",
                kind="after",
                deps=("interrupted",),
                cacheable=False,
            ),
        ),
    )
    store = GraphStore(tmp_path / "resume.sqlite3")
    await store.initialize()
    await store.save_run(GraphRun(run_id="resume-run", graph=graph))
    await store.set_run_status("resume-run", NodeStatus.RUNNING)
    await store.set_node_status(
        "resume-run",
        "done",
        NodeStatus.SUCCEEDED,
        output={"stable": True},
    )
    await store.set_node_status("resume-run", "interrupted", NodeStatus.RUNNING)
    await store.save_attempt(
        NodeAttempt(
            run_id="resume-run",
            node_id="interrupted",
            attempt=1,
            status=NodeStatus.RUNNING,
        )
    )
    called: list[str] = []

    async def should_not_run(_context: NodeContext):
        called.append("done")
        raise AssertionError("succeeded node was executed again")

    async def handler(context: NodeContext):
        called.append(context.node.id)
        return {"upstream": context.upstream_outputs}

    executor = GraphExecutor(
        store,
        handlers={"done": should_not_run, "work": handler, "after": handler},
    )

    run = await executor.resume("resume-run")

    assert run.status is NodeStatus.SUCCEEDED
    assert called == ["interrupted", "after"]
    assert run.node_outputs["done"] == {"stable": True}
    interrupted_attempts = [
        attempt for attempt in run.attempts if attempt.node_id == "interrupted"
    ]
    assert [attempt.attempt for attempt in interrupted_attempts] == [1, 2]
    assert [attempt.status for attempt in interrupted_attempts] == [
        NodeStatus.FAILED,
        NodeStatus.SUCCEEDED,
    ]
    await store.close()


def test_graph_definition_returns_only_selected_descendants_in_topological_order() -> None:
    graph = GraphDefinition(
        graph_id="descendants",
        nodes=(
            GraphNodeSpec(id="source", kind="step"),
            GraphNodeSpec(id="selected", kind="step", deps=("source",)),
            GraphNodeSpec(id="descendant", kind="step", deps=("selected",)),
            GraphNodeSpec(id="unrelated", kind="step"),
        ),
    )

    assert graph.descendants("selected") == ("selected", "descendant")
    assert graph.descendants("selected", include_self=False) == ("descendant",)


@pytest.mark.asyncio
async def test_executor_forks_completed_run_and_reruns_only_descendants_with_proof_comparison(
    tmp_path,
) -> None:
    graph = GraphDefinition(
        graph_id="selective-rerun",
        nodes=(
            GraphNodeSpec(id="source", kind="step", cacheable=False),
            GraphNodeSpec(
                id="selected",
                kind="step",
                deps=("source",),
                cacheable=True,
            ),
            GraphNodeSpec(
                id="verify",
                kind="step",
                deps=("selected",),
                cacheable=True,
            ),
            GraphNodeSpec(id="unrelated", kind="step", cacheable=False),
        ),
    )
    calls: dict[str, int] = {}

    async def step(context: NodeContext) -> NodeResult:
        calls[context.node.id] = calls.get(context.node.id, 0) + 1
        revision = calls[context.node.id]
        return NodeResult(
            output={"node": context.node.id, "revision": revision},
            evidence=EvidenceBundle(
                facts={"node": context.node.id, "revision": revision}
            ),
        )

    store = GraphStore(tmp_path / "selective-rerun.sqlite3")
    executor = GraphExecutor(store, handlers={"step": step})
    source = await executor.execute(graph, run_id="source-run")

    result = await executor.rerun_descendants(
        source.run_id,
        "selected",
        run_id="selected-rerun",
    )

    assert result.rerun.status is NodeStatus.SUCCEEDED
    assert result.rerun.node_statuses == {
        "source": NodeStatus.SUCCEEDED,
        "selected": NodeStatus.SUCCEEDED,
        "verify": NodeStatus.SUCCEEDED,
        "unrelated": NodeStatus.SUCCEEDED,
    }
    assert calls == {"source": 1, "selected": 2, "verify": 2, "unrelated": 1}
    assert [attempt.node_id for attempt in result.rerun.attempts] == [
        "selected",
        "verify",
    ]
    assert result.rerun.node_outputs["source"] == source.node_outputs["source"]
    assert result.rerun.node_outputs["unrelated"] == source.node_outputs["unrelated"]
    assert result.comparison.rerun_nodes == ("selected", "verify")
    assert result.comparison.outcome == "changed"
    assert result.comparison.promotion_status == "review_required"
    persisted = await store.load_rerun_comparison(result.rerun.run_id)
    assert persisted is not None
    assert persisted["comparison_id"] == result.comparison.comparison_id
    assert persisted["baseline_evidence"] == result.comparison.baseline_evidence
    await store.close()


@pytest.mark.asyncio
async def test_executor_runs_bounded_dynamic_specialists_with_inherited_routing(
    tmp_path,
) -> None:
    graph = GraphDefinition(
        graph_id="specialists",
        nodes=(GraphNodeSpec(id="prepare", kind="prepare"),),
    )
    specialists = DynamicSpecialistSubgraph(
        parent_node_id="prepare",
        specialists=(
            GraphNodeSpec(
                id="accessibility",
                kind="specialist",
                mutates_workspace=True,
                write_set=("src/a11y",),
            ),
            GraphNodeSpec(
                id="performance",
                kind="specialist",
                mutates_workspace=True,
                write_set=("src/perf",),
            ),
        ),
        max_concurrency=1,
    )
    active = 0
    max_active = 0
    observed_routing: list[RoutingSnapshot] = []

    async def prepare(_context: NodeContext) -> dict[str, bool]:
        return {"prepared": True}

    async def specialist(context: NodeContext) -> NodeResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        observed_routing.append(context.routing)
        await asyncio.sleep(0.02)
        active -= 1
        return NodeResult(
            output={"specialist": context.node.id},
            evidence=EvidenceBundle(facts={"specialist": context.node.id}),
        )

    routing = RoutingSnapshot(
        backend="codex_cli",
        model="gpt-5.6",
        profile="coding",
    )
    store = GraphStore(tmp_path / "specialists.sqlite3")
    executor = GraphExecutor(
        store,
        handlers={"prepare": prepare, "specialist": specialist},
        max_concurrency=2,
    )

    run = await executor.execute(
        graph,
        run_id="specialist-run",
        routing=routing,
        dynamic_specialists=specialists,
    )

    dynamic_children = tuple(
        node.id for node in run.graph.nodes if node.concurrency_group == specialists.subgraph_id
    )
    assert run.status is NodeStatus.SUCCEEDED
    assert len(run.graph.nodes) == 4
    assert len(dynamic_children) == 2
    assert max_active == 1
    assert observed_routing == [routing, routing]
    assert run.inputs["_dynamic_specialist_subgraph"]["routing"] == "inherit-parent-run"
    assert set(run.node_outputs[specialists.join_node_id]) == set(dynamic_children)
    loaded = await store.load_run(run.run_id)
    assert loaded is not None
    assert loaded.graph == run.graph
    await store.close()


def test_dynamic_specialist_subgraph_rejects_overlapping_workspace_ownership() -> None:
    with pytest.raises(ValueError, match="overlap"):
        DynamicSpecialistSubgraph(
            parent_node_id="prepare",
            specialists=(
                GraphNodeSpec(
                    id="one",
                    kind="specialist",
                    mutates_workspace=True,
                    write_set=("src",),
                ),
                GraphNodeSpec(
                    id="two",
                    kind="specialist",
                    mutates_workspace=True,
                    write_set=("src/components",),
                ),
            ),
        )