# tests/test_recovery_surfacing.py
"""assemble_app_state() must SURFACE the RecoveryResult that
RecoveryManager.restore_and_announce() already extracts from the last
checkpoint, not discard it. This exercises the factored-out capture helper
(``_surface_recovery_result``) plus the read-only ``/api/recovery`` payload
that lets a human/dashboard see what was recovered.
"""
from __future__ import annotations

from skyn3t.cli.main import _surface_recovery_result
from skyn3t.core.events import EventBus
from skyn3t.persistence.checkpoint import CheckpointManager
from skyn3t.persistence.recovery import RecoveryManager
from skyn3t.web import routes
from skyn3t.web.deps import AppState


def _state(**kw) -> AppState:
    return AppState(event_bus=EventBus(), **kw)


# ---------------------------------------------------------------------------
# _surface_recovery_result — the factored capture logic used by
# assemble_app_state() (which is otherwise hard to unit test: it wires the
# full spine, studio, cortex, and messaging stack).
# ---------------------------------------------------------------------------
def test_surface_recovery_result_puts_a_real_checkpoints_facets_on_appstate(tmp_path):
    # Seed a real checkpoint (no mocking) with recognisable files/task facets.
    mgr = CheckpointManager(_dir=tmp_path)
    mgr.save(
        "latest",
        event_bus=EventBus(),
        state={
            "files": ["a.py", "b.py"],
            "worktree_dir": "/tmp/w",
            "build_id": "b42",
            "stage": "verify",
        },
    )
    result = RecoveryManager(mgr).restore(event_bus=EventBus())
    assert result.restored is True  # sanity: the checkpoint really has facets

    state = _state()
    _surface_recovery_result(state, result)

    assert state.recovered_checkpoint is result
    assert state.recovered_checkpoint.task_state["build_id"] == "b42"
    assert state.recovered_checkpoint.files_state["files"] == ["a.py", "b.py"]


def test_surface_recovery_result_leaves_appstate_field_none_when_nothing_restored(tmp_path):
    mgr = CheckpointManager(_dir=tmp_path / "empty")
    result = RecoveryManager(mgr).restore(event_bus=EventBus())
    assert result.restored is False  # sanity: no checkpoint on disk

    state = _state()
    _surface_recovery_result(state, result)
    assert state.recovered_checkpoint is None

    # A raised/best-effort recovery attempt passes None through unharmed.
    _surface_recovery_result(state, None)
    assert state.recovered_checkpoint is None


# ---------------------------------------------------------------------------
# /api/recovery — read-only exposure of whatever landed on AppState.
# ---------------------------------------------------------------------------
async def test_recovery_payload_reports_not_restored_with_no_checkpoint():
    state = _state()
    assert state.recovered_checkpoint is None
    payload = await routes.recovery_payload(state)
    assert payload == {"restored": False}


async def test_recovery_payload_exposes_a_seeded_checkpoints_facets(tmp_path):
    mgr = CheckpointManager(_dir=tmp_path)
    mgr.save(
        "latest",
        event_bus=EventBus(),
        state={
            "files": ["a.py"],
            "worktree_dir": "/tmp/w2",
            "build_id": "b7",
            "stage": "code",
        },
    )
    result = RecoveryManager(mgr).restore(event_bus=EventBus())

    state = _state()
    state.recovered_checkpoint = result

    payload = await routes.recovery_payload(state)
    assert payload["restored"] is True
    assert payload["task_state"]["build_id"] == "b7"
    assert payload["files_state"]["files"] == ["a.py"]
    # Never mutates or reshapes what RecoveryResult already produces.
    assert payload == result.to_dict()
