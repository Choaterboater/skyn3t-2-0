"""Review-only Cortex graph history and selected-rerun API contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyn3t.config.settings import Settings
from skyn3t.studio.build_intelligence import prepare_build_intelligence
from skyn3t.studio.lab_tools import LabToolchainReport, ToolCheck

pytest.importorskip("skyn3t.web.deps")
from skyn3t.web import routes as routes_module  # noqa: E402
from skyn3t.web.deps import AppState  # noqa: E402
from skyn3t.web.routes import (  # noqa: E402
    build_router,
    cortex_graph_reviews_payload,
    cortex_graph_runs_payload,
    decide_cortex_graph_review_payload,
    queue_cortex_graph_review_build_payload,
    rerun_cortex_graph_payload,
)


def _toolchain(*, stack: str = "") -> LabToolchainReport:
    return LabToolchainReport(
        stack=stack,
        checks={
            "docker": ToolCheck(
                name="docker",
                installed=True,
                ready=True,
                required=True,
                detail="test",
            )
        },
    )


async def test_cortex_graph_payload_lists_evidence_and_human_rerun(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        projects_dir=tmp_path / "projects",
        logs_dir=tmp_path / "logs",
        github_similarity_research=False,
    )
    source = await prepare_build_intelligence(
        settings=settings,
        build_id="cortex-graph-build",
        slug="cortex-graph",
        brief="Build a durable graph review surface",
        stack="python_cli",
        toolchain_inspector=_toolchain,
    )
    state = AppState(settings=settings)

    listed = await cortex_graph_runs_payload(state)

    assert listed["available"] is True
    assert listed["review_only"] is True
    assert len(listed["runs"]) == 1
    row = listed["runs"][0]
    assert row["run_id"] == source.graph["run_id"]
    assert row["build"] == {
        "build_id": "cortex-graph-build",
        "slug": "cortex-graph",
        "stack": "python_cli",
    }
    assert row["rerunnable_nodes"] == [
        "product_contract",
        "toolchain",
        "similarity_research",
    ]
    assert "brief" not in row["build"]

    rerun = await rerun_cortex_graph_payload(
        state,
        source_run_id=source.graph["run_id"],
        from_node_id="similarity_research",
    )

    assert rerun["review_only"] is True
    assert rerun["comparison"]["promotion_status"] == "review_required"
    after = await cortex_graph_runs_payload(state)
    rerun_row = next(item for item in after["runs"] if item["run_id"] == rerun["graph"]["run_id"])
    assert rerun_row["rerun"]["source_run_id"] == source.graph["run_id"]
    assert rerun_row["comparison"]["rerun_run_id"] == rerun["graph"]["run_id"]
    assert "baseline_evidence" in rerun_row["comparison"]


async def test_cortex_graph_review_inbox_records_one_immutable_decision_and_explicit_build(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        projects_dir=tmp_path / "projects",
        logs_dir=tmp_path / "logs",
        github_similarity_research=False,
    )
    source = await prepare_build_intelligence(
        settings=settings,
        build_id="review-inbox-build",
        slug="review-inbox",
        brief="Build a graph review inbox",
        stack="python_cli",
        toolchain_inspector=_toolchain,
    )
    state = AppState(settings=settings)
    rerun = await rerun_cortex_graph_payload(
        state,
        source_run_id=source.graph["run_id"],
        from_node_id="similarity_research",
    )
    comparison_id = rerun["comparison"]["comparison_id"]

    before = await cortex_graph_reviews_payload(state)
    assert before["review_only"] is True
    assert before["auto_promotion"] is False
    assert before["pending_count"] == 1
    assert before["reviews"][0]["decision"] is None

    receipt = await decide_cortex_graph_review_payload(
        state,
        comparison_id=comparison_id,
        decision="keep",
        note="Equivalent evidence is a useful baseline.",
    )
    assert receipt["review_only"] is True
    assert receipt["auto_promotion"] is False
    assert receipt["decision"]["promotion"] == "none"
    assert receipt["decision"]["comparison_id"] == comparison_id
    assert receipt["decision"]["note"] == "Equivalent evidence is a useful baseline."

    with pytest.raises(ValueError, match="already recorded"):
        await decide_cortex_graph_review_payload(
            state,
            comparison_id=comparison_id,
            decision="reject",
        )

    async def fake_submit_build(_state, *, brief, stack="", **_kwargs):
        assert brief == "Run a normal Studio build using this verified baseline."
        assert stack == "python_cli"
        return {"build_id": "follow-up-build", "status": "queued"}

    monkeypatch.setattr(routes_module, "submit_build", fake_submit_build)
    queued = await queue_cortex_graph_review_build_payload(
        state,
        comparison_id=comparison_id,
        brief="Run a normal Studio build using this verified baseline.",
    )
    assert queued["review_only"] is True
    assert queued["auto_promotion"] is False
    assert queued["build"]["build_id"] == "follow-up-build"
    assert queued["request"]["brief_sha256"]
    assert "brief" not in queued["request"]

    after = await cortex_graph_reviews_payload(state)
    reviewed = after["reviews"][0]
    assert after["pending_count"] == 0
    assert reviewed["decision"]["decision"] == "keep"
    assert reviewed["decision"]["baseline_digest"] == rerun["comparison"]["baseline_digest"]
    assert reviewed["build_dispatch"]["build_id"] == "follow-up-build"

    with pytest.raises(ValueError, match="already queued"):
        await queue_cortex_graph_review_build_payload(
            state,
            comparison_id=comparison_id,
            brief="Try a duplicate normal build.",
        )


def test_cortex_graph_api_routes_are_exposed(tmp_path) -> None:
    state = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path / "data"))
    routes = {
        (route.path, ",".join(sorted(route.methods or []))) for route in build_router(state).routes
    }

    assert ("/api/cortex/graphs", "GET") in routes
    assert ("/api/cortex/graphs/{run_id}/rerun", "POST") in routes
    assert ("/api/cortex/graph-reviews", "GET") in routes
    assert ("/api/cortex/graph-reviews/{comparison_id}/decide", "POST") in routes
    assert ("/api/cortex/graph-reviews/{comparison_id}/build", "POST") in routes
