"""Persisted Cortex tuning is read by Settings, but env / init kwargs still win."""

from __future__ import annotations

import json

import pytest

import skyn3t.config.settings as settings_mod
from skyn3t.config.settings import Settings


@pytest.fixture(autouse=True)
def _isolate_overrides(tmp_path, monkeypatch):
    """Point REPO_ROOT at a tmp dir so the settings source reads tmp, never the
    real repo data dir — and the real overrides file is never touched."""
    monkeypatch.setattr(settings_mod, "REPO_ROOT", tmp_path)
    # Ensure no SKYN3T_CRITIC_ENABLED leaks in from the environment.
    monkeypatch.delenv("SKYN3T_CRITIC_ENABLED", raising=False)
    yield


def _write_override(tmp_path, data: dict):
    p = tmp_path / "data" / "cortex" / "settings_overrides.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def test_persisted_override_applies_over_default(tmp_path):
    _write_override(tmp_path, {"critic_enabled": False})
    assert Settings(_env_file=None).critic_enabled is False


def test_os_env_wins_over_override(tmp_path, monkeypatch):
    _write_override(tmp_path, {"critic_enabled": False})
    monkeypatch.setenv("SKYN3T_CRITIC_ENABLED", "1")
    assert Settings(_env_file=None).critic_enabled is True


def test_init_kwarg_wins_over_override(tmp_path):
    _write_override(tmp_path, {"critic_enabled": False})
    assert Settings(_env_file=None, critic_enabled=True).critic_enabled is True


def test_no_override_file_is_noop(tmp_path):
    # No file written -> construction succeeds with defaults, no raise.
    assert Settings(_env_file=None).critic_enabled is True


def test_non_allowlisted_override_is_ignored(tmp_path):
    # A non-allow-listed key on disk is filtered by load_overrides -> not applied.
    _write_override(tmp_path, {"autonomous_builds": True})
    assert Settings(_env_file=None).autonomous_builds is False
