from __future__ import annotations

from skyn3t.studio.flow_canvas import FLOW_CANVAS_FILENAME, read_flow_canvas, write_flow_canvas
from skyn3t.studio.planner import BuildPlan
from skyn3t.studio.stages import StageSpec


def _workflow_plan() -> BuildPlan:
    return BuildPlan(
        slug="briefing",
        brief="Build an agent workflow that sends daily briefings",
        stack="workflow",
        stages=[
            StageSpec("architect", "architect", "architecture"),
            StageSpec("code", "code", "codegen"),
            StageSpec("review", "reviewer", "review"),
        ],
        checklist=["main.py", "workflow_core.py"],
    )


def test_write_flow_canvas_exports_editable_workflow_graph(tmp_path):
    flow = write_flow_canvas(tmp_path, _workflow_plan())

    assert flow["schema"] == "skyn3t.flow_canvas.v1"
    assert flow["stack"] == "workflow"
    assert [node["id"] for node in flow["nodes"]] == [
        "start", "stage:architect", "stage:code", "stage:review", "deliver",
    ]
    assert flow["edges"][0] == {"source": "start", "target": "stage:architect"}
    assert (tmp_path / FLOW_CANVAS_FILENAME).exists()
    assert read_flow_canvas(tmp_path) == flow


def test_read_flow_canvas_rejects_wrong_schema(tmp_path):
    (tmp_path / FLOW_CANVAS_FILENAME).write_text('{"schema":"other","nodes":[],"edges":[]}')

    assert read_flow_canvas(tmp_path) is None
