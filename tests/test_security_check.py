from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.security_check import check_security


def test_security_check_flags_bundled_secret(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.js").write_text(
        "export const apiKey = 'sk-live-1234567890abcdef';\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "react")

    assert verdict["ok"] is False
    assert any("secret" in issue.lower() for issue in verdict["issues"])


def test_security_check_skips_artifact_stack(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")

    verdict = check_security(tmp_path, "python_cli")

    assert verdict["skipped"] is True


def test_security_gate_downgrades_critical_findings(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.js").write_text("eval(req.query.code)\n", encoding="utf-8")
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"),
        memory=None,
    )
    man = BuildManifest(slug="x", brief="api", stack="express")

    score, verdict = runner._run_security_gate(
        man, str(tmp_path), SimpleNamespace(stack="express"), 91.0, "go"
    )

    assert verdict == "no_go"
    assert score == 49.0
    assert man.extra["security_check"]["ok"] is False
