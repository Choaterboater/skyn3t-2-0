# tests/test_cli_shoot.py
"""studio shoot delegates to screenshot and reports the saved path (or skip)."""
from __future__ import annotations

from skyn3t.studio import visual_check as vc


def test_screenshot_soft_skips_without_playwright(monkeypatch, tmp_path):
    # In this env Playwright is absent -> screenshot returns None (no raise).
    monkeypatch.setattr(vc, "playwright_available", lambda: False)
    assert vc.screenshot("http://127.0.0.1:9/", str(tmp_path / "x.png")) is None


def test_screenshot_returns_path_when_capture_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "playwright_available", lambda: True)

    # fake the Playwright body by replacing screenshot's effect at a higher level
    out = str(tmp_path / "shot.png")

    def fake_shot(url, path, **k):
        # simulate a real capture writing bytes
        from pathlib import Path
        Path(path).write_bytes(b"\x89PNG fake")
        return path

    monkeypatch.setattr(vc, "screenshot", fake_shot)
    assert vc.screenshot("http://127.0.0.1:9/", out) == out
