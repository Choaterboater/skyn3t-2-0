"""Persisted tuning overrides: write/read an allow-listed, never-raising store."""

from __future__ import annotations

from skyn3t.cortex.tuning_store import load_overrides, overrides_path, persist_overrides


def test_persist_and_load_roundtrip(tmp_path):
    out = persist_overrides(tmp_path, {"critic_enabled": False})
    assert out == {"critic_enabled": False}
    assert overrides_path(tmp_path).exists()
    assert load_overrides(tmp_path) == {"critic_enabled": False}


def test_persist_filters_non_allowlisted(tmp_path):
    # data_dir / autonomy / secrets must never persist, even if "applied".
    out = persist_overrides(tmp_path, {"critic_enabled": True, "data_dir": "/x", "autonomous_builds": True})
    assert out == {"critic_enabled": True}
    assert load_overrides(tmp_path) == {"critic_enabled": True}


def test_persist_empty_is_noop(tmp_path):
    assert persist_overrides(tmp_path, {}) == {}
    assert not overrides_path(tmp_path).exists()


def test_load_missing_is_empty(tmp_path):
    assert load_overrides(tmp_path) == {}


def test_persist_never_raises_on_bad_dir(tmp_path):
    # Make the 'cortex' path a FILE so mkdir(parents) fails -> must not raise.
    (tmp_path / "cortex").write_text("not a dir")
    assert persist_overrides(tmp_path, {"critic_enabled": False}) == {}
