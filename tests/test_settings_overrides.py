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


def test_corrupt_override_value_is_dropped_not_fatal(tmp_path):
    # The allow-list filters KEYS; a hand-edited file with a bad VALUE used to
    # raise ValidationError out of every get_settings() caller. It must cost
    # only the one override.
    _write_override(tmp_path, {"best_of_n": "not-an-int", "critic_enabled": False})
    s = Settings(_env_file=None)
    assert s.best_of_n == 2  # bad value dropped -> field default
    assert s.critic_enabled is False  # sibling override still applies


def test_out_of_range_override_value_is_dropped_not_fatal(tmp_path):
    # Constraint violations (best_of_n has ge=1, le=8) are dropped too, not
    # just wrong types.
    _write_override(tmp_path, {"best_of_n": 999})
    assert Settings(_env_file=None).best_of_n == 2


def test_override_read_follows_skyn3t_data_dir(tmp_path, monkeypatch):
    # The writers persist to settings.data_dir; the reader must follow the
    # same root. Reading only REPO_ROOT/data meant SKYN3T_DATA_DIR installs
    # showed tuning as applied while the engine never saw it (audit M2).
    alt = tmp_path / "alt-data"
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(alt))
    p = alt / "cortex" / "settings_overrides.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"critic_enabled": False}), encoding="utf-8")
    assert Settings(_env_file=None).critic_enabled is False


def _write_override_at(root, data: dict):
    p = root / "cortex" / "settings_overrides.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def test_override_read_follows_dotenv_skyn3t_data_dir(tmp_path, monkeypatch):
    # SKYN3T_DATA_DIR set ONLY in .env — the flagship configuration surface
    # (.env.example documents it). The writers persist to settings.data_dir
    # (the .env root), so the reader must follow it too; honoring only OS env
    # meant .env installs wrote tuning to one root and read from another.
    monkeypatch.delenv("SKYN3T_DATA_DIR", raising=False)  # conftest pins it suite-wide
    alt = tmp_path / "dotenv-data"
    _write_override_at(alt, {"critic_enabled": False})
    env_file = tmp_path / ".env"
    env_file.write_text(f"SKYN3T_DATA_DIR={alt.as_posix()}\n", encoding="utf-8")
    s = Settings(_env_file=env_file)
    assert s.data_dir == alt  # the root status/writers use...
    assert s.critic_enabled is False  # ...is the root the reader followed


def test_os_env_outranks_dotenv_for_override_read_root(tmp_path, monkeypatch):
    # Same precedence as pydantic gives the data_dir field itself: OS env
    # first, then .env — the read root must never diverge from where
    # settings.data_dir will actually point.
    env_root = tmp_path / "env-data"
    dotenv_root = tmp_path / "dotenv-data"
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(env_root))
    _write_override_at(env_root, {"critic_enabled": False})
    _write_override_at(dotenv_root, {"best_of_n": 5})
    env_file = tmp_path / ".env"
    env_file.write_text(f"SKYN3T_DATA_DIR={dotenv_root.as_posix()}\n", encoding="utf-8")
    s = Settings(_env_file=env_file)
    assert s.critic_enabled is False  # OS-env root was read
    assert s.best_of_n == 2  # the shadowed .env root was not
