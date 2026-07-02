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
def _no_cli_vision(request, monkeypatch):
    """Neuter the CLI vision fallback for the whole suite.

    ``visual_check._make_cli_vision_fn`` spawns a REAL ``claude -p`` whenever the
    CLI is on PATH and no OpenRouter key is set — so any test path reaching an
    actual screenshot+judge (qa_playtest / game_visual / liveness) silently burns
    CLI quota on a dev machine with claude installed. Tests that exercise the CLI
    path itself opt out with ``@pytest.mark.real_cli_vision`` — those stub
    ``shutil.which``/``subprocess`` themselves, so nothing real is ever spawned.
    """
    if request.node.get_closest_marker("real_cli_vision"):
        yield
        return
    from skyn3t.studio import visual_check

    monkeypatch.setattr(visual_check, "_make_cli_vision_fn", lambda settings: None)
    yield


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
    # Don't read the developer's real repo .env during tests. Settings hard-codes
    # env_file=REPO_ROOT/.env, so a locally-configured secret (replicate/github
    # token, …) otherwise bleeds in and breaks tests that assert on the DEFAULT
    # ("not configured"). Tests get config from explicit kwargs + os.environ only.
    monkeypatch.setitem(settings_mod.Settings.model_config, "env_file", None)
    # LLM keys included: a shell-exported SKYN3T_OPENROUTER_API_KEY would flip
    # every test's LLMClient from "stub" to a REAL paid backend (the vision-CLI
    # cousin of this leak is fenced by _no_cli_vision above). Tests that need a
    # key set it explicitly via monkeypatch/kwargs.
    for _var in ("SKYN3T_REPLICATE_API_TOKEN", "SKYN3T_REPLICATE_MODEL",
                 "SKYN3T_GITHUB_TOKEN",
                 "SKYN3T_OPENROUTER_API_KEY", "SKYN3T_ANTHROPIC_API_KEY",
                 "SKYN3T_OPENAI_API_KEY", "SKYN3T_KIMI_API_KEY"):
        monkeypatch.delenv(_var, raising=False)
    settings_mod.get_settings.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()
