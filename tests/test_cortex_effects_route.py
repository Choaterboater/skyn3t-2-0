"""2A: the /cortex/effects route surfaces cortex's real effects to the dashboard.

Backend-agnostic handler test (no FastAPI / running server needed) — mirrors the
other route handler tests.
"""

from __future__ import annotations

import pytest

from skyn3t.config.settings import Settings

pytest.importorskip("skyn3t.web.deps")
from skyn3t.web.deps import AppState  # noqa: E402
from skyn3t.web.routes import cortex_effects_payload  # noqa: E402


async def test_cortex_effects_payload_reports_three_effects(tmp_path):
    data_dir = tmp_path / "data"

    from skyn3t.intelligence.model_tournament import ModelTournament

    t = ModelTournament(data_dir / "model_tournament.json")
    t.record_win(t.bucket_key("cheap", "code"), winner="vendor/coder:free", losers=[])

    from skyn3t.cortex.prompt_store import persist_prompt_override

    persist_prompt_override(data_dir, "reviewer", "Be stricter.")

    from skyn3t.cortex.tuning_store import persist_overrides

    persist_overrides(data_dir, {"best_of_n": 3})

    state = AppState(settings=Settings(
        data_dir=data_dir, projects_dir=tmp_path / "p", logs_dir=tmp_path / "l",
    ))
    payload = await cortex_effects_payload(state)

    assert "vendor/coder:free" in str(payload["leaderboard"])
    assert payload["prompts"].get("reviewer") == "Be stricter."
    assert payload["tuning"].get("best_of_n") == 3


async def test_cortex_effects_payload_empty_is_well_formed(tmp_path):
    state = AppState(settings=Settings(
        data_dir=tmp_path / "data", projects_dir=tmp_path / "p", logs_dir=tmp_path / "l",
    ))
    payload = await cortex_effects_payload(state)
    assert payload == {"leaderboard": {}, "tuning": {}, "prompts": {}}
