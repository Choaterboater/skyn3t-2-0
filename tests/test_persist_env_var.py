"""_persist_env_var upserts the ACTIVE assignment without touching comments.

Bug: it stripped a leading '#' before reading the key, so a commented line like
`# SKYN3T_GITHUB_TOKEN=old` matched the key and got overwritten/uncommented —
destroying the comment and producing duplicate active lines.
"""

from __future__ import annotations

import skyn3t.config.settings as smod
from skyn3t.web import routes
from skyn3t.web.routes import _persist_env_var


def test_commented_line_is_not_treated_as_the_active_key(tmp_path, monkeypatch):
    monkeypatch.setattr(smod, "REPO_ROOT", tmp_path)
    env = tmp_path / ".env"
    env.write_text(
        "# SKYN3T_FOO=commented-note\n"
        "SKYN3T_FOO=active\n"
        "SKYN3T_BAR=keep\n"
    )
    _persist_env_var("SKYN3T_FOO", "new")
    lines = env.read_text().splitlines()
    # the comment must survive verbatim (not be uncommented/overwritten)
    assert "# SKYN3T_FOO=commented-note" in lines
    # the active assignment is updated exactly once (no duplicates)
    assert lines.count("SKYN3T_FOO=new") == 1
    assert "SKYN3T_FOO=active" not in lines
    # unrelated keys are untouched
    assert "SKYN3T_BAR=keep" in lines


def test_appends_when_key_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(smod, "REPO_ROOT", tmp_path)
    env = tmp_path / ".env"
    env.write_text("SKYN3T_BAR=keep\n")
    _persist_env_var("SKYN3T_NEW", "v")
    lines = env.read_text().splitlines()
    assert "SKYN3T_NEW=v" in lines
    assert "SKYN3T_BAR=keep" in lines


def test_commented_key_absent_active_is_appended_not_uncommented(tmp_path, monkeypatch):
    # Only a commented form exists -> the active value must be APPENDED, leaving
    # the comment intact (previously it silently uncommented the existing line).
    monkeypatch.setattr(smod, "REPO_ROOT", tmp_path)
    env = tmp_path / ".env"
    env.write_text("# SKYN3T_TOKEN=disabled\n")
    _persist_env_var("SKYN3T_TOKEN", "live")
    lines = env.read_text().splitlines()
    assert "# SKYN3T_TOKEN=disabled" in lines
    assert "SKYN3T_TOKEN=live" in lines


def test_persist_env_var_writes_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(smod, "REPO_ROOT", tmp_path)
    calls = []

    def fake_atomic(path, text):
        calls.append((path, text))
        path.write_text(text, encoding="utf-8")
        return path

    monkeypatch.setattr(routes, "atomic_write_text", fake_atomic)

    _persist_env_var("SKYN3T_ATOMIC", "yes")

    assert calls
    assert calls[0][0] == tmp_path / ".env"
    assert "SKYN3T_ATOMIC=yes" in calls[0][1]
