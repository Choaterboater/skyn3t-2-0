"""The /gates route: every end-of-build gate GUI-toggleable, registry-driven.

Gate kill-switches existed only as env/settings.json fields — against the
recorded config-via-GUI rule, and the curated settings_payload list had
silently drifted (it never learned about mcp/rag/workflow/cli gates). The new
surface reads core.stacks.GATES directly, so a future gate appears in the GUI
with zero route changes, and the toggle is ALLOWLISTED against the registry —
it can never setattr an arbitrary settings field.

Backend-agnostic handler tests (no FastAPI server), mirroring the siblings.
"""

from __future__ import annotations

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.stacks import GATES

pytest.importorskip("skyn3t.web.deps")
from skyn3t.web.deps import AppState  # noqa: E402
from skyn3t.web.routes import gates_payload, set_gate_enabled  # noqa: E402


def _make_state(tmp_path):
    return AppState(settings=Settings(
        data_dir=tmp_path / "data", projects_dir=tmp_path / "p", logs_dir=tmp_path / "l",
    ))


async def test_gates_payload_lists_every_registry_gate(tmp_path):
    payload = await gates_payload(_make_state(tmp_path))
    listed = {g["gate"] for g in payload["gates"]}
    assert listed == {spec.name for spec in GATES}
    for g in payload["gates"]:
        assert isinstance(g["enabled"], bool)
        assert g["stacks"], g
        assert g["flag"].endswith("_enabled")


async def test_toggle_by_gate_name_updates_live_settings(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    # Don't let the test write the real .env: persist=False exercises the
    # settings + env layers only.
    payload = await set_gate_enabled(state, "rag_check", False, persist=False)
    assert state.settings.rag_check_enabled is False
    entry = next(g for g in payload["gates"] if g["gate"] == "rag_check")
    assert entry["enabled"] is False
    payload = await set_gate_enabled(state, "rag_check_enabled", True, persist=False)
    assert state.settings.rag_check_enabled is True

    payload = await set_gate_enabled(state, "cli_playtest", False, persist=False)
    assert state.settings.cli_playtest_enabled is False
    entry = next(g for g in payload["gates"] if g["gate"] == "cli_playtest")
    assert entry["enabled"] is False


async def test_unknown_gate_is_rejected_not_setattred(tmp_path):
    state = _make_state(tmp_path)
    with pytest.raises(ValueError, match="unknown gate"):
        await set_gate_enabled(state, "llm_backend", False, persist=False)
    with pytest.raises(ValueError, match="unknown gate"):
        await set_gate_enabled(state, "__proto__", True, persist=False)
