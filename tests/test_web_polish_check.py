from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.web_polish_check import check_web_polish


def test_web_polish_flags_thin_page(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>hello</body></html>", encoding="utf-8")

    verdict = check_web_polish(tmp_path, "static")

    assert verdict["ok"] is False
    assert len(verdict["issues"]) >= 2


def test_web_polish_accepts_structured_page(tmp_path):
    (tmp_path / "index.html").write_text(
        "<main class='grid hero'><h1>Planner</h1><a href='/start'>Start</a></main>",
        encoding="utf-8",
    )

    assert check_web_polish(tmp_path, "static")["ok"] is True


def test_web_polish_gate_downgrades_thin_ui(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>hello</body></html>", encoding="utf-8")
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"),
        memory=None,
    )
    man = BuildManifest(slug="x", brief="site", stack="static")

    score, verdict = runner._run_web_polish_gate(
        man, str(tmp_path), SimpleNamespace(stack="static"), 88.0, "go"
    )

    assert verdict == "no_go"
    assert score == 49.0
    assert man.extra["web_polish"]["ok"] is False
