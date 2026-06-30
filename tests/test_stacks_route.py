"""The /stacks route surfaces the supported real-builder stacks to the dashboard.

Backend-agnostic handler test (no FastAPI / running server needed) — mirrors the
other route handler tests. The stack picker in the build console reads this.
"""

from __future__ import annotations

import pytest

from skyn3t.config.settings import Settings

pytest.importorskip("skyn3t.web.deps")
from skyn3t.web.deps import AppState  # noqa: E402
from skyn3t.web.routes import set_build_metadata_overrides, stacks_payload  # noqa: E402


def _make_state(tmp_path):
    return AppState(settings=Settings(
        data_dir=tmp_path / "data", projects_dir=tmp_path / "p", logs_dir=tmp_path / "l",
    ))


async def test_stacks_payload_lists_all_real_builder_stacks(tmp_path):
    payload = await stacks_payload(_make_state(tmp_path))
    stacks = payload["stacks"]
    ids = [s["id"] for s in stacks]
    for expected in ("react", "react_native", "fastapi", "express", "static", "python"):
        assert expected in ids
    # Every stack carries a non-empty, human-readable description.
    for s in stacks:
        assert s["id"]
        assert isinstance(s["description"], str) and s["description"].strip()


async def test_stacks_payload_preserves_source_order(tmp_path):
    from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS

    payload = await stacks_payload(_make_state(tmp_path))
    ids = [s["id"] for s in payload["stacks"]]
    assert ids == list(REAL_BUILDER_STACKS.keys())


async def test_build_metadata_overrides_update_live_settings(tmp_path):
    state = _make_state(tmp_path)
    payload = await set_build_metadata_overrides(
        state, app_type="data viz", engine="browser_native", persist=False
    )
    assert payload == {
        "app_type_override": "data_viz",
        "engine_override": "browser_native",
    }
    assert state.settings.app_type_override == "data_viz"
    assert state.settings.engine_override == "browser_native"
