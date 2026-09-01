from __future__ import annotations

from datetime import UTC, datetime

import pytest

from skyn3t.config.settings import Settings
from skyn3t.studio.build_intelligence import (
    prepare_build_intelligence,
    rerun_build_intelligence,
)
from skyn3t.studio.graph_runtime import (
    GraphDefinition,
    GraphNodeSpec,
    GraphRun,
    GraphStore,
    NodeAttempt,
    NodeStatus,
)
from skyn3t.studio.lab_tools import LabToolchainReport, ToolCheck


class _GitHub:
    def __init__(self) -> None:
        self.searches: list[str] = []

    async def search_repositories(self, query: str):
        self.searches.append(query)
        return [
            {
                "full_name": "example/deep-task-board",
                "html_url": "https://github.com/example/deep-task-board",
                "description": "A focused task board with keyboard workflows",
                "license": {"spdx_id": "MIT"},
                "stargazers_count": 400,
                "pushed_at": "2026-07-20T00:00:00Z",
                "default_branch": "main",
            }
        ]

    async def inspect_repository(self, repository):
        return {
            "readme": "# Deep task board\n## Keyboard command palette\n",
            "manifests": {"package.json": {"dependencies": {"zustand": "^5"}}},
        }


def _toolchain(*, stack: str = "") -> LabToolchainReport:
    return LabToolchainReport(
        stack=stack,
        checks={
            "docker": ToolCheck(
                name="docker",
                installed=True,
                ready=True,
                required=True,
                detail="27.0",
            ),
            "playwright": ToolCheck(
                name="playwright",
                installed=True,
                ready=True,
                required=True,
                detail="1.61",
            ),
            "maestro": ToolCheck(
                name="maestro",
                installed=False,
                ready=False,
                required=False,
                detail="not required",
            ),
        },
    )


async def test_build_intelligence_runs_real_dag_and_keeps_research_in_backlog(tmp_path):
    github = _GitHub()
    settings = Settings(
        data_dir=tmp_path / "data",
        github_similarity_research=True,
        github_similarity_max_repos=8,
    )

    result = await prepare_build_intelligence(
        settings=settings,
        build_id="build-123",
        slug="task-board",
        brief="Build a deep React task board for a product team",
        stack="react",
        personas=["product team"],
        github_client=github,
        toolchain_inspector=_toolchain,
        clock=lambda: datetime(2026, 7, 25, tzinfo=UTC),
    )

    assert result.graph["status"] == "succeeded"
    assert result.graph["nodes"] == {
        "product_contract": "succeeded",
        "similarity_research": "succeeded",
        "toolchain": "succeeded",
    }
    assert result.product.project_id == "task-board"
    assert [item.text for item in result.product.requirements] == [
        "Build a deep React task board for a product team"
    ]
    assert result.product.version == 2
    assert result.research.status == "ok"
    assert result.research.requirements_modified is False
    assert result.product.backlog
    assert result.toolchain.ready is True
    assert github.searches
    assert (tmp_path / "data" / "build_graphs.sqlite3").is_file()


async def test_build_intelligence_explicitly_records_disabled_research(tmp_path):
    github = _GitHub()
    settings = Settings(
        data_dir=tmp_path / "data",
        github_similarity_research=False,
    )

    result = await prepare_build_intelligence(
        settings=settings,
        build_id="build-456",
        slug="offline-tool",
        brief="Build an offline Python tool",
        stack="python_cli",
        personas=[],
        github_client=github,
        toolchain_inspector=_toolchain,
    )

    assert result.graph["status"] == "succeeded"
    assert result.research.status == "unavailable"
    assert result.research.error == "GitHub similarity research is disabled"
    assert result.product.version == 1
    assert github.searches == []


def _preflight_graph() -> GraphDefinition:
    return GraphDefinition(
        graph_id="studio-build-preflight",
        version="1",
        nodes=(
            GraphNodeSpec(
                id="product_contract",
                kind="product_contract",
                cacheable=False,
            ),
            GraphNodeSpec(id="toolchain", kind="toolchain", cacheable=False),
            GraphNodeSpec(
                id="similarity_research",
                kind="similarity_research",
                deps=("product_contract",),
                cacheable=False,
            ),
        ),
    )


async def test_build_intelligence_resumes_interrupted_run_without_rerunning_success(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        github_similarity_research=False,
    )
    run_id = "build-resume-preflight"
    inputs = {
        "build_id": "build-resume",
        "slug": "resume-tool",
        "brief": "Build a resumable Python tool",
        "stack": "python_cli",
        "personas": [],
        "source_product_spec": None,
        "research_enabled": False,
    }
    store = GraphStore(settings.data_dir / "build_graphs.sqlite3")
    run = GraphRun(run_id=run_id, graph=_preflight_graph(), inputs=inputs)
    await store.save_run(run)
    await store.set_run_status(run_id, NodeStatus.RUNNING)
    toolchain_output = _toolchain(stack="python_cli").to_dict()
    await store.set_node_status(
        run_id,
        "toolchain",
        NodeStatus.SUCCEEDED,
        output=toolchain_output,
    )
    await store.save_attempt(
        NodeAttempt(
            run_id=run_id,
            node_id="toolchain",
            attempt=1,
            status=NodeStatus.SUCCEEDED,
            output=toolchain_output,
        )
    )
    await store.set_node_status(run_id, "product_contract", NodeStatus.RUNNING)
    await store.save_attempt(
        NodeAttempt(
            run_id=run_id,
            node_id="product_contract",
            attempt=1,
            status=NodeStatus.RUNNING,
        )
    )
    await store.close()

    toolchain_calls = 0

    def must_not_rerun_toolchain(*, stack: str = "") -> LabToolchainReport:
        nonlocal toolchain_calls
        toolchain_calls += 1
        raise AssertionError(f"completed toolchain node reran for {stack}")

    result = await prepare_build_intelligence(
        settings=settings,
        build_id="build-resume",
        slug="resume-tool",
        brief="Build a resumable Python tool",
        stack="python_cli",
        personas=[],
        toolchain_inspector=must_not_rerun_toolchain,
    )

    assert result.graph["status"] == "succeeded"
    assert toolchain_calls == 0
    toolchain_attempts = [
        attempt for attempt in result.graph["attempts"] if attempt["node"] == "toolchain"
    ]
    product_attempts = [
        attempt
        for attempt in result.graph["attempts"]
        if attempt["node"] == "product_contract"
    ]
    assert [attempt["attempt"] for attempt in toolchain_attempts] == [1]
    assert [attempt["status"] for attempt in product_attempts] == ["failed", "succeeded"]
    assert [attempt["attempt"] for attempt in product_attempts] == [1, 2]


async def test_build_intelligence_rejects_existing_run_with_different_inputs(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        github_similarity_research=False,
    )
    await prepare_build_intelligence(
        settings=settings,
        build_id="build-input-contract",
        slug="input-contract",
        brief="Build the original Python tool",
        stack="python_cli",
        toolchain_inspector=_toolchain,
    )

    with pytest.raises(ValueError, match="incompatible inputs"):
        await prepare_build_intelligence(
            settings=settings,
            build_id="build-input-contract",
            slug="input-contract",
            brief="Build a different Python tool",
            stack="python_cli",
            toolchain_inspector=_toolchain,
        )


async def test_build_intelligence_rejects_existing_run_with_different_graph(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        github_similarity_research=False,
    )
    store = GraphStore(settings.data_dir / "build_graphs.sqlite3")
    await store.save_run(
        GraphRun(
            run_id="build-graph-contract-preflight",
            graph=GraphDefinition(
                graph_id="legacy-preflight",
                nodes=(GraphNodeSpec(id="legacy", kind="legacy"),),
            ),
            inputs={
                "build_id": "build-graph-contract",
                "slug": "graph-contract",
                "brief": "Build a Python tool",
                "stack": "python_cli",
                "personas": [],
                "source_product_spec": None,
                "research_enabled": False,
            },
        )
    )
    await store.close()

    with pytest.raises(ValueError, match="incompatible graph"):
        await prepare_build_intelligence(
            settings=settings,
            build_id="build-graph-contract",
            slug="graph-contract",
            brief="Build a Python tool",
            stack="python_cli",
            toolchain_inspector=_toolchain,
        )


async def test_build_intelligence_rerun_reexecutes_only_human_selected_branch(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        github_similarity_research=False,
    )
    source = await prepare_build_intelligence(
        settings=settings,
        build_id="build-rerun",
        slug="rerun-tool",
        brief="Build a reviewable Python tool",
        stack="python_cli",
        toolchain_inspector=_toolchain,
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )

    def must_not_rerun_toolchain(*, stack: str = "") -> LabToolchainReport:
        raise AssertionError(f"toolchain ancestor reran for {stack}")

    result = await rerun_build_intelligence(
        settings=settings,
        source_run_id=source.graph["run_id"],
        from_node_id="similarity_research",
        toolchain_inspector=must_not_rerun_toolchain,
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert result["review_only"] is True
    assert result["graph"]["status"] == "succeeded"
    assert result["graph"]["nodes"] == {
        "product_contract": "succeeded",
        "toolchain": "succeeded",
        "similarity_research": "succeeded",
    }
    assert [row["node"] for row in result["graph"]["attempts"]] == ["similarity_research"]
    assert result["comparison"]["source_run_id"] == source.graph["run_id"]
    assert result["comparison"]["rerun_nodes"] == ["similarity_research"]
    assert result["comparison"]["outcome"] == "equivalent"
    assert result["comparison"]["promotion_status"] == "review_required"
