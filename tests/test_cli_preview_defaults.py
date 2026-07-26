from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from skyn3t.cli import main as cli_main


class _FakePreviewSupervisor:
    instances: list[_FakePreviewSupervisor] = []

    def __init__(self) -> None:
        self.started: list[tuple[Path, str, dict]] = []
        self.stopped: list[object] = []
        self.__class__.instances.append(self)

    async def start(self, project_dir, stack="", **kwargs):
        self.started.append((Path(project_dir), stack, kwargs))
        return SimpleNamespace(
            status="running",
            url="http://127.0.0.1:9876",
            detail={},
            pid=None,
            log_path=None,
        )

    async def stop(self, app):
        await asyncio.sleep(0)
        self.stopped.append(app)


class _HostRunnerMustNotBeConstructed:
    def __init__(self, *_args, **_kwargs):
        raise AssertionError("CLI preview must not execute generated code on host")


class _FakeImproveEngine:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


async def _fake_spine(tmp_path: Path) -> dict:
    return {
        "settings": SimpleNamespace(projects_dir=tmp_path),
        "event_bus": object(),
        "orchestrator": object(),
        "memory": object(),
    }


def _wire_cli_dependencies(tmp_path: Path, monkeypatch) -> None:
    import skyn3t.studio.app_runner as app_runner
    import skyn3t.studio.improve as improve
    import skyn3t.studio.preview_supervisor as preview_supervisor
    import skyn3t.studio.visual_check as visual_check

    _FakePreviewSupervisor.instances.clear()
    monkeypatch.setattr(app_runner, "AppRunner", _HostRunnerMustNotBeConstructed)
    monkeypatch.setattr(
        preview_supervisor,
        "PreviewSupervisor",
        _FakePreviewSupervisor,
    )
    monkeypatch.setattr(improve, "ImproveEngine", _FakeImproveEngine)
    monkeypatch.setattr(visual_check, "VisualChecker", lambda **_kwargs: object())
    monkeypatch.setattr(visual_check, "make_vision_fn", lambda _settings: None)
    monkeypatch.setattr(cli_main, "_assemble_spine", lambda: _fake_spine(tmp_path))
    monkeypatch.setattr(
        cli_main,
        "_build_intelligence",
        lambda *_args, **_kwargs: (None, None, None, None),
    )


def test_cli_visual_and_liveness_use_docker_supervisor(tmp_path, monkeypatch):
    import skyn3t.studio.liveness as liveness
    import skyn3t.studio.visual_loop as visual_loop

    _wire_cli_dependencies(tmp_path, monkeypatch)
    captured: list[object] = []

    async def fake_visual(*_args, **kwargs):
        captured.append(kwargs["app_runner"])
        return "visual"

    async def fake_liveness(*_args, **kwargs):
        captured.append(kwargs["app_runner"])
        return "liveness"

    monkeypatch.setattr(visual_loop, "visual_self_improve", fake_visual)
    monkeypatch.setattr(liveness, "liveness_self_improve", fake_liveness)

    visual = asyncio.run(
        cli_main._run_visual(str(tmp_path), goal="polish", max_rounds=1)
    )
    live = asyncio.run(
        cli_main._run_liveness_cli(str(tmp_path), max_rounds=1)
    )

    assert visual == "visual"
    assert live == "liveness"
    assert len(captured) == 2
    assert all(isinstance(runner, _FakePreviewSupervisor) for runner in captured)


def test_cli_serve_uses_docker_supervisor_and_awaits_stop(tmp_path, monkeypatch):
    import time

    _wire_cli_dependencies(tmp_path, monkeypatch)
    (tmp_path / "index.html").write_text("<h1>Preview</h1>", encoding="utf-8")
    monkeypatch.setattr(
        cli_main,
        "_console",
        lambda: SimpleNamespace(print=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    cli_main.studio_serve(str(tmp_path), port=0)

    assert len(_FakePreviewSupervisor.instances) == 1
    runner = _FakePreviewSupervisor.instances[0]
    assert runner.started == [(tmp_path, "", {"port": None})]
    assert len(runner.stopped) == 1
