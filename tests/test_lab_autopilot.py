"""Durable local-autopilot contracts."""

from __future__ import annotations

import pytest

from skyn3t.config.settings import Settings
from skyn3t.cortex.lab_autopilot import LabAutopilot

pytest.importorskip("skyn3t.web.deps")
from skyn3t.web.deps import AppState  # noqa: E402
from skyn3t.web.routes import (  # noqa: E402
    cortex_autopilot_payload,
    report_cortex_autopilot_incident,
    set_cortex_autopilot,
    tick_cortex_autopilot,
)


def test_autopilot_deduplicates_incidents_and_persists_receipts(tmp_path) -> None:
    controller = LabAutopilot(tmp_path, enabled=True)
    first = controller.report_incident(
        scope="skyn3t",
        category="test_failure",
        summary="Cortex route contract failed",
    )
    again = controller.report_incident(
        scope="skyn3t",
        category="test_failure",
        summary="Cortex route contract failed",
        evidence="pytest output",
    )

    assert first.incident_id == again.incident_id
    assert again.occurrences == 2
    run = controller.next_run()
    assert run is not None and run.kind == "repair" and run.status == "queued"
    finished = controller.finish(run.run_id, succeeded=True, proof_summary="focused checks passed")
    assert finished.status == "succeeded"
    assert LabAutopilot(tmp_path).incidents[0].status == "resolved"


async def test_route_autopilot_enables_local_lab_and_prioritizes_repairs(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        projects_dir=tmp_path / "projects",
        logs_dir=tmp_path / "logs",
    )
    state = AppState(settings=settings)

    before = await cortex_autopilot_payload(state)
    assert before["enabled"] is False
    changed = await set_cortex_autopilot(state, enabled=True, persist=False)
    assert changed["enabled"] is True
    assert state.settings.lab_autonomy is True
    assert state.settings.cortex_candidate_auto_merge is True
    assert changed["remote_push"] is False

    await report_cortex_autopilot_incident(
        state,
        scope="active-project",
        category="ui_build_failure",
        summary="Vite build failed",
    )
    tick = await tick_cortex_autopilot(state)
    assert tick["run"]["kind"] == "repair"
    assert tick["run"]["summary"] == "Vite build failed"
    assert tick["local_only"] is True
