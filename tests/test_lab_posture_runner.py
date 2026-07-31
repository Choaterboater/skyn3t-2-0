"""Lab posture at the runner level: findings are recorded, not vetoes.

The complaint this encodes: a personal lab was returning no_go on delivered,
running apps because a regex matched a placeholder API key or an emoji appeared
in a heading. Under "lab" those record into the gate_findings ledger, cap the
score so the learning loop is not trained that a flagged build is a 95, and let
the build complete. Under "release" they block exactly as 2.0 did.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.gate_posture import GatePosture
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner

# A page thin enough to fail the structural polish checks deterministically
# (no heading, no action, no styling). Emoji alone no longer fail web_polish —
# they are an advisory warning — so the posture test pins a real failure.
_THIN_PAGE = "<html><body>hello</body></html>"


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


def _findings(manifest, gate: str) -> list[dict]:
    return [f for f in manifest.extra.get("gate_findings", []) if f["gate"] == gate]


def test_security_finding_records_but_does_not_block_in_lab(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    (project / "config.js").write_text(
        'const api_key = "REPLACE_WITH_YOUR_KEY_HERE";\n', encoding="utf-8"
    )
    runner = _runner(tmp_path, build_posture="lab")
    manifest = BuildManifest(slug="app", brief="brief", stack="static")

    score, verdict = runner._run_security_gate(
        manifest, str(project), SimpleNamespace(stack="static"), 91.0, "go",
        posture=GatePosture(posture="lab"),
    )

    assert verdict == "go"
    assert score == 74.0  # capped, not clamped to the failed-delivery band
    assert manifest.extra["security_check"]["ok"] is False  # evidence preserved
    assert manifest.extra["security_gate"]  # legacy key still written
    assert _findings(manifest, "security") == [
        {
            "gate": "security",
            "status": "failed",
            "reason": manifest.extra["security_gate"],
            "blocked": False,
            "posture": "lab",
        }
    ]


def test_security_finding_blocks_in_release(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    (project / "config.js").write_text(
        'const api_key = "REPLACE_WITH_YOUR_KEY_HERE";\n', encoding="utf-8"
    )
    runner = _runner(tmp_path, build_posture="release")
    manifest = BuildManifest(slug="app", brief="brief", stack="static")

    score, verdict = runner._run_security_gate(
        manifest, str(project), SimpleNamespace(stack="static"), 91.0, "go",
        posture=GatePosture(posture="release"),
    )

    assert verdict == "no_go"
    assert score <= 49.0
    assert _findings(manifest, "security")[0]["blocked"] is True


def test_web_polish_finding_is_advisory_in_lab_and_blocking_in_release(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    (project / "index.html").write_text(_THIN_PAGE, encoding="utf-8")

    lab = _runner(tmp_path, build_posture="lab")
    lab_manifest = BuildManifest(slug="app", brief="brief", stack="static")
    _, lab_verdict = lab._run_web_polish_gate(
        lab_manifest, str(project), SimpleNamespace(stack="static"), 91.0, "go",
        posture=GatePosture(posture="lab"),
    )

    release = _runner(tmp_path, build_posture="release")
    rel_manifest = BuildManifest(slug="app", brief="brief", stack="static")
    _, rel_verdict = release._run_web_polish_gate(
        rel_manifest, str(project), SimpleNamespace(stack="static"), 91.0, "go",
        posture=GatePosture(posture="release"),
    )

    assert lab_verdict == "go"
    assert rel_verdict == "no_go"
    # Same finding recorded either way — posture changes the consequence only.
    assert lab_manifest.extra["web_polish"] == rel_manifest.extra["web_polish"]


def test_blocking_gates_escape_hatch_restores_one_gate_in_lab(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    (project / "config.js").write_text(
        'const api_key = "REPLACE_WITH_YOUR_KEY_HERE";\n', encoding="utf-8"
    )
    runner = _runner(tmp_path, build_posture="lab", blocking_gates="security")
    manifest = BuildManifest(slug="app", brief="brief", stack="static")

    _, verdict = runner._run_security_gate(
        manifest, str(project), SimpleNamespace(stack="static"), 91.0, "go",
        posture=GatePosture(posture="lab", forced_blocking=frozenset({"security"})),
    )

    assert verdict == "no_go"


def test_gate_outcome_records_skipped_without_blocking_in_any_posture(tmp_path):
    runner = _runner(tmp_path, build_posture="release")
    manifest = BuildManifest(slug="app", brief="brief", stack="static")

    verdict = runner._gate_outcome(
        manifest, "proof_ladder", None, "go", "docker unavailable",
        posture=GatePosture(posture="release"),
    )

    assert verdict == "go"
    finding = _findings(manifest, "proof_ladder")[0]
    assert finding["status"] == "skipped"
    assert finding["blocked"] is False


def test_gate_outcome_is_a_noop_for_a_passing_gate(tmp_path):
    runner = _runner(tmp_path, build_posture="release")
    manifest = BuildManifest(slug="app", brief="brief", stack="static")

    verdict = runner._gate_outcome(manifest, "security", True, "go", "")

    assert verdict == "go"
    assert "gate_findings" not in manifest.extra


def test_gate_outcome_never_upgrades_an_existing_no_go(tmp_path):
    runner = _runner(tmp_path, build_posture="lab")
    manifest = BuildManifest(slug="app", brief="brief", stack="static")

    verdict = runner._gate_outcome(
        manifest, "security", False, "no_go", "x", posture=GatePosture(posture="lab")
    )

    assert verdict == "no_go"


def test_gate_posture_and_lab_policy_are_independent_axes():
    from skyn3t.studio.lab_policy import LabAutonomyPolicy

    # Softening the verdict must not soften irreversible external actions.
    assert GatePosture(posture="lab").blocks("security") is False
    assert LabAutonomyPolicy(enabled=True).approval_required("remote_deploy") is True
    assert LabAutonomyPolicy(enabled=True).approval_required("write_secret") is True
