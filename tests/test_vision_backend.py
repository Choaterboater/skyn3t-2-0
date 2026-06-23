# tests/test_vision_backend.py
from types import SimpleNamespace

from skyn3t.studio import visual_check as vc


def test_openrouter_preferred_when_key():
    fn = vc.make_vision_fn(SimpleNamespace(
        openrouter_api_key="sk-or", vision_model="", cli_llm_provider="claude"))
    assert fn is not None  # an OpenRouter fn


def test_cli_fn_when_no_key_but_cli_present(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which",
                        lambda p: "/usr/bin/claude" if p == "claude" else None)
    fn = vc.make_vision_fn(SimpleNamespace(
        openrouter_api_key="", vision_model="", cli_llm_provider="claude"))
    assert fn is not None


def test_none_when_no_key_and_no_cli(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda p: None)
    fn = vc.make_vision_fn(SimpleNamespace(
        openrouter_api_key="", vision_model="", cli_llm_provider="claude"))
    assert fn is None


def test_cli_fn_passes_image_path_and_returns_stdout(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which",
                        lambda p: "/usr/bin/kimi" if p == "kimi" else None)
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return SimpleNamespace(stdout='{"matches": true}', returncode=0)

    monkeypatch.setattr(vc.subprocess, "run", fake_run)
    fn = vc.make_vision_fn(SimpleNamespace(
        openrouter_api_key="", vision_model="", cli_llm_provider="kimi"))
    out = fn("/tmp/shot.png", "judge it")
    assert "/tmp/shot.png" in " ".join(captured["argv"])
    assert out == '{"matches": true}'
