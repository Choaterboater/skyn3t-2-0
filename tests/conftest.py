"""Test-session defaults.

Pin the LLM backend to the deterministic offline ``stub`` so the suite never
shells out to a locally-installed CLI (claude/kimi/copilot) or the network —
tests stay fast, hermetic, and reproducible regardless of what's on the host.
"""

import os

import pytest

os.environ["SKYN3T_LLM_BACKEND"] = "stub"

try:  # settings may have been imported+cached already
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
except Exception:  # pragma: no cover
    pass


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Point every test's ``data_dir`` at a unique temp dir.

    Subsystems persist to ``data_dir`` (cortex proposals.jsonl, model_tournament,
    tuning/prompt overrides, the SQLite db, snapshots). Without isolation those
    files leak ACROSS tests (shared dedup/tournament state) and INTO the real
    ``./data`` — a test once wrote fake cortex proposals there that the live
    server would then load. Tests that pass an explicit ``Settings(data_dir=...)``
    still win (init args outrank env), so this only redirects the default.
    """
    from skyn3t.config import settings as settings_mod

    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    settings_mod.get_settings.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()
