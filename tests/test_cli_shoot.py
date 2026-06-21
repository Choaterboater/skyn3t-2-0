# tests/test_cli_shoot.py
"""studio shoot delegates to screenshot and reports the saved path (or skip)."""
from __future__ import annotations

import pytest
import typer
import skyn3t.studio.visual_check as vc
from skyn3t.cli.main import studio_shoot


def test_screenshot_soft_skips_without_playwright(monkeypatch, tmp_path):
    # In this env Playwright is absent -> screenshot returns None (no raise).
    monkeypatch.setattr(vc, "playwright_available", lambda: False)
    assert vc.screenshot("http://127.0.0.1:9/", str(tmp_path / "x.png")) is None


def test_shoot_command_exits_1_without_playwright(monkeypatch):
    monkeypatch.setattr(vc, "playwright_available", lambda: False)
    with pytest.raises(typer.Exit) as e:
        studio_shoot("http://127.0.0.1:9/", "")
    assert e.value.exit_code == 1


def test_shoot_command_success_prints_path(monkeypatch, tmp_path):
    out = str(tmp_path / "shot.png")
    monkeypatch.setattr(vc, "playwright_available", lambda: True)
    monkeypatch.setattr(vc, "screenshot", lambda url, path, **k: out)
    # success path must NOT raise typer.Exit
    studio_shoot("http://127.0.0.1:9/", out)


def test_shoot_command_exits_2_on_capture_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "playwright_available", lambda: True)
    monkeypatch.setattr(vc, "screenshot", lambda url, path, **k: None)
    with pytest.raises(typer.Exit) as e:
        studio_shoot("http://127.0.0.1:9/", str(tmp_path / "x.png"))
    assert e.value.exit_code == 2
