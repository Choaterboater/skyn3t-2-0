"""2A: `skyn3t cortex status` makes the closed loops visible.

It surfaces the three effects cortex now has — the learned-router leaderboard
(fed by real builds), persisted tuning overrides, and persisted prompt overrides
— so an operator can see the autonomy layer actually moving the needle.
"""

from __future__ import annotations

from typer.testing import CliRunner

from skyn3t.cli.main import app

runner = CliRunner()


def _isolate(monkeypatch, tmp_path) -> None:
    from skyn3t.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SKYN3T_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SKYN3T_PROJECTS_DIR", str(tmp_path / "projects"))


def test_cortex_status_runs_clean_when_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["cortex", "status"])
    assert result.exit_code == 0, result.output


def test_cortex_status_shows_all_three_effects(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    data_dir = tmp_path / "data"

    # Seed the three effect sources as a real build/approval would.
    from skyn3t.intelligence.model_tournament import ModelTournament

    t = ModelTournament(data_dir / "model_tournament.json")
    t.record_win(t.bucket_key("cheap", "code"), winner="vendor/coder:free", losers=[])

    from skyn3t.cortex.prompt_store import persist_prompt_override

    persist_prompt_override(data_dir, "reviewer", "Be stricter about error handling.")

    from skyn3t.cortex.tuning_store import persist_overrides

    persist_overrides(data_dir, {"best_of_n": 3})

    result = runner.invoke(app, ["cortex", "status"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "vendor/coder:free" in out          # leaderboard
    assert "reviewer" in out                    # prompt override
    assert "best_of_n" in out                   # tuning override
