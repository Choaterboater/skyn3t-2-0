"""Durable product-intelligence preflight for Studio builds.

The preflight is intentionally separate from source generation. It turns the
brief into a versioned product contract, inspects the local proof toolchain,
and optionally researches active similar GitHub projects. Research may add
provenance-backed backlog ideas, but it never changes current requirements.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.config.settings import Settings
from skyn3t.studio.github_research import GitHubResearchClient
from skyn3t.studio.graph_runtime import (
    EvidenceBundle,
    GraphDefinition,
    GraphExecutor,
    GraphNodeSpec,
    GraphRun,
    GraphStore,
    NodeContext,
    NodeResult,
    RoutingSnapshot,
)
from skyn3t.studio.lab_tools import LabToolchainReport, inspect_lab_toolchain
from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord
from skyn3t.studio.similarity_scout import SimilarityReport, SimilarityScout


@dataclass(frozen=True, slots=True)
class BuildIntelligenceResult:
    product: ProductSpecV1
    research: SimilarityReport
    toolchain: LabToolchainReport
    graph: dict[str, Any]


def _graph_summary(run: GraphRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "graph_id": run.graph.graph_id,
        "graph_version": run.graph.version,
        "status": run.status.value,
        "nodes": {
            node.id: run.node_statuses[node.id].value
            for node in run.graph.nodes
        },
        "attempts": [
            {
                "node": attempt.node_id,
                "attempt": attempt.attempt,
                "status": attempt.status.value,
                "error": attempt.error,
                "facts": dict(attempt.evidence.facts),
            }
            for attempt in run.attempts
        ],
    }


def _toolchain_from_dict(value: dict[str, Any]) -> LabToolchainReport:
    from skyn3t.studio.lab_tools import ToolCheck

    checks = {
        str(name): ToolCheck(
            name=str(raw.get("name") or name),
            installed=bool(raw.get("installed")),
            ready=bool(raw.get("ready")),
            required=bool(raw.get("required")),
            detail=str(raw.get("detail") or ""),
        )
        for name, raw in dict(value.get("checks") or {}).items()
        if isinstance(raw, dict)
    }
    return LabToolchainReport(stack=str(value.get("stack") or ""), checks=checks)


async def prepare_build_intelligence(
    *,
    settings: Settings,
    build_id: str,
    slug: str,
    brief: str,
    stack: str,
    personas: Sequence[str] = (),
    source_product_spec: ProductSpecV1 | Mapping[str, Any] | None = None,
    github_client: Any | None = None,
    toolchain_inspector: Callable[..., LabToolchainReport] = inspect_lab_toolchain,
    clock: Callable[[], datetime] | None = None,
) -> BuildIntelligenceResult:
    """Run the real, persisted preflight DAG and return typed outputs."""

    selected_clock = clock or (lambda: datetime.now(UTC))
    if isinstance(source_product_spec, ProductSpecV1):
        source_product = ProductSpecV1.from_dict(source_product_spec.to_dict())
    elif isinstance(source_product_spec, Mapping):
        source_product = ProductSpecV1.from_dict(source_product_spec)
    elif source_product_spec is None:
        source_product = None
    else:
        raise TypeError("source_product_spec must be a ProductSpecV1 or mapping")
    graph = GraphDefinition(
        graph_id="studio-build-preflight",
        version="1",
        nodes=(
            GraphNodeSpec(
                id="product_contract",
                kind="product_contract",
                cacheable=False,
            ),
            GraphNodeSpec(
                id="toolchain",
                kind="toolchain",
                cacheable=False,
            ),
            GraphNodeSpec(
                id="similarity_research",
                kind="similarity_research",
                deps=("product_contract",),
                cacheable=False,
            ),
        ),
    )

    def product_contract(_context: NodeContext) -> NodeResult:
        product = (
            ProductSpecV1.from_dict(source_product.to_dict())
            if source_product is not None
            else ProductSpecV1(
                project_id=slug,
                goal=brief,
                personas=[
                    str(item).strip()
                    for item in personas
                    if str(item).strip()
                ],
                requirements=[RequirementRecord(text=brief, source="brief")],
                architecture_decisions=[f"Selected generated-app stack: {stack}"],
            )
        )
        return NodeResult(
            output=product.to_dict(),
            evidence=EvidenceBundle(
                facts={
                    "schema_version": product.schema_version,
                    "requirements": len(product.requirements),
                }
            ),
        )

    def toolchain(_context: NodeContext) -> NodeResult:
        report = toolchain_inspector(stack=stack)
        return NodeResult(
            output=report.to_dict(),
            evidence=EvidenceBundle(
                facts={
                    "ready": report.ready,
                    "missing_required": report.missing_required,
                }
            ),
        )

    async def similarity(context: NodeContext) -> NodeResult:
        product = ProductSpecV1.from_dict(
            dict(context.upstream_outputs["product_contract"])
        )
        if not bool(getattr(settings, "github_similarity_research", False)):
            report = SimilarityReport(
                status="unavailable",
                queries=[],
                sources=[],
                backlog=[],
                retrieved_at=selected_clock().isoformat(),
                error="GitHub similarity research is disabled",
            )
        else:
            client = github_client or GitHubResearchClient(
                token=str(getattr(settings, "github_token", "") or ""),
                max_results=max(
                    8,
                    int(getattr(settings, "github_similarity_max_repos", 8)),
                ),
            )
            scout = SimilarityScout(
                client,
                cache_path=Path(settings.data_dir)
                / "similarity"
                / f"{slug}.json",
                max_results=int(
                    getattr(settings, "github_similarity_max_repos", 8)
                ),
                clock=selected_clock,
            )
            report = await scout.research(
                brief=product.goal or brief,
                stack=stack,
                requirements=product.requirements,
            )
            product = product.record_research(
                sources=report.research_sources,
                backlog=report.backlog,
                base_version=product.version,
                provenance={
                    "build_id": build_id,
                    "queries": list(report.queries),
                    "requirements_modified": False,
                },
            )
        return NodeResult(
            output={
                "report": report.to_dict(),
                "product": product.to_dict(),
            },
            evidence=EvidenceBundle(
                facts={
                    "status": report.status,
                    "sources": len(report.sources),
                    "backlog": len(report.backlog),
                    "requirements_modified": False,
                }
            ),
        )

    run_id = f"{build_id}-preflight"
    run_inputs = {
        "build_id": build_id,
        "slug": slug,
        "brief": brief,
        "stack": stack,
        "personas": list(personas),
        "source_product_spec": (
            source_product.to_dict() if source_product is not None else None
        ),
        "research_enabled": bool(
            getattr(settings, "github_similarity_research", False)
        ),
    }
    toolchain_snapshot = {"schema": 1}
    prompt_snapshot = {"brief": brief, "stack": stack}
    store = GraphStore(Path(settings.data_dir) / "build_graphs.sqlite3")
    executor = GraphExecutor(
        store,
        handlers={
            "product_contract": product_contract,
            "toolchain": toolchain,
            "similarity_research": similarity,
        },
        max_concurrency=max(
            1,
            int(getattr(settings, "build_graph_max_concurrency", 4)),
        ),
    )
    try:
        existing = await store.load_run(run_id)
        if existing is not None:
            if existing.graph != graph:
                raise ValueError(f"preflight run {run_id!r} has an incompatible graph")
            if existing.inputs != run_inputs:
                raise ValueError(f"preflight run {run_id!r} has incompatible inputs")
            run = await executor.resume(
                run_id,
                toolchain=toolchain_snapshot,
                prompt=prompt_snapshot,
            )
        else:
            run = await executor.execute(
                graph,
                run_id=run_id,
                inputs=run_inputs,
                routing=RoutingSnapshot(
                    backend=str(getattr(settings, "llm_backend", "") or ""),
                    model=str(getattr(settings, "preferred_model", "") or ""),
                    profile="preflight",
                ),
                toolchain=toolchain_snapshot,
                prompt=prompt_snapshot,
            )
    finally:
        await store.close()

    research_output = dict(run.node_outputs["similarity_research"])
    return BuildIntelligenceResult(
        product=ProductSpecV1.from_dict(dict(research_output["product"])),
        research=SimilarityReport.from_dict(dict(research_output["report"])),
        toolchain=_toolchain_from_dict(dict(run.node_outputs["toolchain"])),
        graph=_graph_summary(run),
    )
