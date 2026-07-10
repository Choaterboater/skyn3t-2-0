# tests/test_cli_liveness.py
"""The `skyn3t studio liveness` command is registered and its helper is wired to
the (already-tested) liveness loop."""
from __future__ import annotations

import inspect
import io
from types import SimpleNamespace

import pytest
import typer


def test_liveness_command_registered():
    from skyn3t.cli.main import studio_app

    names = {c.name or (c.callback and c.callback.__name__) for c in studio_app.registered_commands}
    assert "liveness" in names


def test_run_liveness_cli_is_async():
    from skyn3t.cli import main as cli_main

    assert hasattr(cli_main, "_run_liveness_cli")
    assert inspect.iscoroutinefunction(cli_main._run_liveness_cli)


def _outcome(*, passed=True, visual_total=1, visual_failed=0, visual_skipped=0):
    visual = None
    if visual_total:
        visual = {"matches": visual_failed == 0, "skipped": False}
    elif visual_skipped:
        visual = {"matches": None, "skipped": True}
    report = SimpleNamespace(
        ok=1,
        total=1,
        health=1.0,
        visual_total=visual_total,
        visual_failed=visual_failed,
        visual_skipped=visual_skipped,
        visual_artifact_dir="evidence",
        visual_report_path="visual-proof.json",
        results=[SimpleNamespace(
            ok=True,
            method="GET",
            path="/",
            status=200,
            visual=visual,
        )],
    )
    return SimpleNamespace(passed=passed, skipped=False, report=report, rounds=1, reason="")


def test_liveness_cli_forwards_custom_evidence_dir(monkeypatch, tmp_path):
    from skyn3t.cli import main as cli_main

    seen = {}

    async def fake(project, *, max_rounds, evidence_dir=""):
        seen.update(project=project, max_rounds=max_rounds, evidence_dir=evidence_dir)
        return _outcome()

    monkeypatch.setattr(cli_main, "_run_liveness_cli", fake)
    cli_main.studio_liveness("demo", 3, str(tmp_path), False)

    assert seen == {
        "project": "demo",
        "max_rounds": 3,
        "evidence_dir": str(tmp_path),
    }


def test_liveness_cli_default_reports_visual_skip_without_calling_it_passed(monkeypatch):
    from skyn3t.cli import main as cli_main

    async def fake(*args, **kwargs):
        return _outcome(visual_total=0, visual_skipped=1)

    monkeypatch.setattr(cli_main, "_run_liveness_cli", fake)
    cli_main.studio_liveness("demo", 1, "", False)


def test_liveness_cli_require_visual_rejects_browser_skip(monkeypatch):
    from skyn3t.cli import main as cli_main

    async def fake(*args, **kwargs):
        return _outcome(visual_total=0, visual_skipped=1)

    monkeypatch.setattr(cli_main, "_run_liveness_cli", fake)
    with pytest.raises(typer.Exit) as exc:
        cli_main.studio_liveness("demo", 1, "", True)

    assert exc.value.exit_code == 3


def test_console_degrades_unencodable_windows_glyphs_instead_of_crashing(monkeypatch):
    from skyn3t.cli import main as cli_main

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(cli_main.sys, "stdout", stream)

    cli_main._console().print("route passed \u2714")
    stream.flush()

    assert b"route passed ?" in raw.getvalue()
