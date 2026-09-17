"""Failed repair evidence controls durable incident retries."""

from __future__ import annotations

from dataclasses import replace

import pytest

from skyn3t.cortex.lab_autopilot import AutopilotIncident, LabAutopilot


def _report(controller: LabAutopilot, evidence: str = "") -> AutopilotIncident:
    return controller.report_incident(
        scope="skyn3t",
        category="test_failure",
        summary="Repair regression",
        evidence=evidence,
    )


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [("  trace  ", "trace"), (" \t\n", ""), (" " * 2100 + "trace", "trace")],
    ids=["trimmed", "blank", "trim-before-bound"],
)
def test_new_incident_normalizes_evidence_before_bounding(tmp_path, evidence, expected):
    controller = LabAutopilot(tmp_path, enabled=True)
    incident = _report(controller, evidence)
    assert incident.evidence == expected
    assert LabAutopilot(tmp_path).incidents[0].evidence == expected


@pytest.mark.parametrize("evidence", ["", "   ", "same failure", "same failure   "])
def test_failed_repair_stays_quarantined_across_reports_and_restart(tmp_path, evidence):
    controller = LabAutopilot(tmp_path, enabled=True)
    incident = _report(controller, "same failure")
    run = controller.next_run()
    assert run is not None
    finished = controller.finish(run.run_id, succeeded=False, proof_summary="tests failed")
    assert finished.status == "quarantined"
    assert incident.status == "quarantined"

    for _ in range(3):
        controller = LabAutopilot(tmp_path)
        reported = _report(controller, evidence)
        assert reported.incident_id == incident.incident_id
        assert reported.status == "quarantined"
        assert reported.evidence == "same failure"
        assert controller.next_run() is None
        assert controller.payload()["open_incidents"] == []
        assert len(controller.runs) == 1
        assert len(controller.incidents) == 1

    assert LabAutopilot(tmp_path).incidents[0].occurrences == 4
    assert LabAutopilot(tmp_path).runs[0].proof_summary == "tests failed"


@pytest.mark.parametrize("succeeded", [True, False])
def test_finish_updates_all_matching_legacy_incident_records(tmp_path, succeeded):
    controller = LabAutopilot(tmp_path, enabled=True)
    incident = _report(controller, "same failure")
    controller.incidents.append(replace(incident))
    run = controller.next_run()
    assert run is not None
    controller.finish(run.run_id, succeeded=succeeded)
    expected = "resolved" if succeeded else "quarantined"
    reloaded = LabAutopilot(tmp_path)
    assert [item.status for item in reloaded.incidents] == [expected, expected]
    assert reloaded.next_run() is None


@pytest.mark.parametrize("original_evidence", ["", "same failure"])
def test_changed_nonempty_evidence_reopens_one_incident_and_queues_one_retry(
    tmp_path, original_evidence
):
    controller = LabAutopilot(tmp_path, enabled=True)
    incident = _report(controller, original_evidence)
    first_run = controller.next_run()
    assert first_run is not None
    controller.finish(first_run.run_id, succeeded=False)

    controller = LabAutopilot(tmp_path)
    reopened = _report(controller, "  different failure  ")
    assert reopened.incident_id == incident.incident_id
    assert reopened.status == "open"
    assert reopened.evidence == "different failure"
    assert reopened.occurrences == 2
    assert len(controller.incidents) == 1

    controller = LabAutopilot(tmp_path)
    retry = controller.next_run()
    assert retry is not None
    assert retry.run_id != first_run.run_id
    assert retry.incident_id == incident.incident_id
    assert controller.next_run().run_id == retry.run_id
    assert len(controller.runs) == 2
    controller.finish(retry.run_id, succeeded=False)
    assert _report(controller, "different failure").status == "quarantined"
    assert LabAutopilot(tmp_path).next_run() is None


def test_evidence_is_bounded_before_comparing_for_a_retry(tmp_path):
    controller = LabAutopilot(tmp_path, enabled=True)
    incident = _report(controller, "x" * 2000 + "first tail")
    assert incident.evidence == "x" * 2000
    run = controller.next_run()
    assert run is not None
    controller.finish(run.run_id, succeeded=False)

    reported = _report(controller, "x" * 2000 + "different tail")
    assert reported.status == "quarantined"
    assert reported.evidence == "x" * 2000
    assert controller.next_run() is None
    assert len(controller.incidents) == 1


def test_whitespace_at_the_evidence_bound_does_not_create_a_retry_after_restart(tmp_path):
    controller = LabAutopilot(tmp_path, enabled=True)
    evidence = "failure" + " " * 2000 + "truncated tail"
    incident = _report(controller, evidence)
    run = controller.next_run()
    assert run is not None
    controller.finish(run.run_id, succeeded=False)

    controller = LabAutopilot(tmp_path)
    reported = _report(controller, evidence)
    assert reported.incident_id == incident.incident_id
    assert reported.status == "quarantined"
    assert controller.next_run() is None


@pytest.mark.parametrize("evidence", ["", "same failure", "new failure"])
def test_new_report_reopens_resolved_incident_without_duplicate_records(tmp_path, evidence):
    controller = LabAutopilot(tmp_path, enabled=True)
    incident = _report(controller, "same failure")
    run = controller.next_run()
    assert run is not None
    controller.finish(run.run_id, succeeded=True)
    assert incident.status == "resolved"

    controller = LabAutopilot(tmp_path)
    reported = _report(controller, evidence)
    assert reported.incident_id == incident.incident_id
    assert reported.status == "open"
    assert reported.occurrences == 2
    assert len(controller.incidents) == 1
    retry = LabAutopilot(tmp_path).next_run()
    assert retry is not None and retry.incident_id == incident.incident_id
    assert retry.run_id != run.run_id


def test_quarantine_does_not_block_unrelated_queued_work_or_bypass_disabled_state(tmp_path):
    controller = LabAutopilot(tmp_path, enabled=True)
    incident = _report(controller, "same failure")
    research = controller.queue_research(summary="Investigate a different failure")
    experiment = controller.queue_skill_experiment(summary="Try a skill", skills=["repair"])
    repair = controller.next_run()
    assert repair is not None and repair.incident_id == incident.incident_id
    controller.finish(repair.run_id, succeeded=False)

    controller = LabAutopilot(tmp_path)
    next_run = controller.next_run()
    assert next_run is not None and next_run.run_id == research.run_id
    controller.finish(next_run.run_id, succeeded=False)
    controller.set_enabled(False)
    assert _report(controller, "changed evidence").status == "open"
    assert LabAutopilot(tmp_path).next_run() is None

    controller.set_enabled(True)
    retry = controller.next_run()
    assert retry is not None and retry.incident_id == incident.incident_id
    controller.finish(retry.run_id, succeeded=True)
    next_run = controller.next_run()
    assert next_run is not None and next_run.run_id == experiment.run_id
