# tests/test_vision_backend.py
from types import SimpleNamespace

import pytest

from skyn3t.studio import visual_check as vc


def test_openrouter_preferred_when_key():
    fn = vc.make_vision_fn(SimpleNamespace(
        openrouter_api_key="sk-or", vision_model="", cli_llm_provider="claude"))
    assert fn is not None  # an OpenRouter fn


@pytest.mark.real_cli_vision
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


# ── make_click_vision_fn: pixel-grounding (click-target localization) needs precise
# spatial accuracy. Measured live against the same screenshot: the local claude CLI
# located a menu card's click target within 1px (179,276 vs the true ~180,275); the
# default cheap OpenRouter vision model (gpt-4o-mini) was off by ~230px and landed on
# the wrong (locked) card. So unlike make_vision_fn (OpenRouter-preferred — fine for
# true/false image judgments), make_click_vision_fn prefers the CLI when available, only
# falling back to OpenRouter if no vision-capable CLI is on PATH.

@pytest.mark.real_cli_vision
def test_click_vision_prefers_cli_even_with_a_key(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which",
                        lambda p: "/usr/bin/claude" if p == "claude" else None)
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return SimpleNamespace(stdout='{"in_play": false, "x": 1, "y": 2}', returncode=0)

    monkeypatch.setattr(vc.subprocess, "run", fake_run)
    fn = vc.make_click_vision_fn(SimpleNamespace(
        openrouter_api_key="sk-or", vision_model="", cli_llm_provider="claude"))
    fn("/tmp/shot.png", "where do I click?")
    assert "claude" in captured["argv"][0]


def test_click_vision_falls_back_to_openrouter_when_no_cli(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda p: None)
    fn = vc.make_click_vision_fn(SimpleNamespace(
        openrouter_api_key="sk-or", vision_model="", cli_llm_provider="claude"))
    assert fn is not None  # the OpenRouter fn (no CLI on PATH)


def test_click_vision_none_when_no_key_and_no_cli(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda p: None)
    fn = vc.make_click_vision_fn(SimpleNamespace(
        openrouter_api_key="", vision_model="", cli_llm_provider="claude"))
    assert fn is None


@pytest.mark.real_cli_vision
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


@pytest.mark.real_cli_vision
def test_cli_fn_runs_with_cwd_set_to_the_images_own_directory(monkeypatch):
    # A `claude -p --setting-sources project` subprocess scopes its file-read
    # permission to ITS OWN cwd ("the project"). Screenshots live in a system temp
    # dir (e.g. /var/folders/.../T/skyn3t-qa-shot-xxx.png), nowhere near the skyn3t
    # repo the calling process happens to be running from — without an explicit cwd
    # the CLI refuses the read ("outside the project directory") and the vision call
    # silently degrades to an unparseable refusal string. Root cause of
    # make_click_vision_fn appearing to "find no click target" against a real
    # screenshot despite the SAME prompt working fine when run by hand from the
    # image's own directory.
    monkeypatch.setattr(vc.shutil, "which",
                        lambda p: "/usr/bin/claude" if p == "claude" else None)
    captured = {}

    def fake_run(argv, **kw):
        captured["kw"] = kw
        return SimpleNamespace(stdout='{"matches": true}', returncode=0)

    monkeypatch.setattr(vc.subprocess, "run", fake_run)
    fn = vc.make_vision_fn(SimpleNamespace(
        openrouter_api_key="", vision_model="", cli_llm_provider="claude"))
    fn("/var/folders/xx/T/skyn3t-qa-shot-abc123.png", "judge it")
    assert captured["kw"].get("cwd") == "/var/folders/xx/T"


# ── Suite hermeticity: the CLI fallback is a QUOTA LEAK inside the test suite — any
# test path that reaches a real screenshot+judge with default settings (empty
# OpenRouter key) would spawn a REAL `claude -p` on a dev machine with the CLI on
# PATH. The autouse `_no_cli_vision` fixture (conftest.py) neuters the CLI factory
# for every test; the CLI-path tests above opt back in with
# @pytest.mark.real_cli_vision (they stub shutil.which/subprocess themselves, so
# nothing real is ever spawned).

def test_suite_default_never_builds_a_cli_vision_backend(monkeypatch):
    # Simulate the leak condition: claude IS on PATH, no OpenRouter key.
    monkeypatch.setattr(vc.shutil, "which",
                        lambda p: "/usr/bin/claude" if p == "claude" else None)
    s = SimpleNamespace(openrouter_api_key="", vision_model="", cli_llm_provider="claude")
    assert vc.make_vision_fn(s) is None
    assert vc.make_click_vision_fn(s) is None
