"""Gate posture: does a gate's finding BLOCK the verdict, or just get recorded?

Guards the contract that made lab posture necessary: a personal lab was
returning no_go on delivered, running apps because Docker was absent, a regex
matched a placeholder key, or a heuristic flagged a platform-skipped test.
Posture must soften exactly those, and nothing that proves the app is broken.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.studio.gate_posture import (
    _ALWAYS_BLOCKING,
    _RELEASE_ONLY,
    KNOWN_GATES,
    GatePosture,
)


def test_lab_posture_never_blocks_a_heuristic_gate():
    posture = GatePosture.from_settings(Settings(build_posture="lab"))

    assert posture.blocks("security") is False
    assert posture.blocks("web_polish") is False
    assert posture.blocks("reward_hacking") is False
    assert posture.blocks("proof_ladder") is False
    assert posture.blocks("product_quality") is False
    assert posture.blocks("generated_tests") is False


def test_lab_posture_still_blocks_a_broken_delivery():
    posture = GatePosture.from_settings(Settings(build_posture="lab"))

    for gate in sorted(_ALWAYS_BLOCKING):
        assert posture.blocks(gate) is True, gate


def test_a_failed_native_build_still_blocks_via_proof():
    """"native_build" is not its own gate name, by design.

    proof_run keeps a failed npm/swift/wheel build blocking even under lab
    posture, and it reaches the verdict as the "proof" signal. Naming it here
    too would be a classification the runner never emits — the drift test in
    tests/test_gate_name_drift.py fails if this set gains such a name.
    """
    posture = GatePosture.from_settings(Settings(build_posture="lab"))

    assert "native_build" not in _ALWAYS_BLOCKING
    assert posture.blocks("proof") is True


def test_release_posture_blocks_everything():
    posture = GatePosture.from_settings(Settings(build_posture="release"))

    for gate in sorted(KNOWN_GATES):
        assert posture.blocks(gate) is True, gate


def test_blocking_gates_escape_hatch_forces_named_gates_hard():
    posture = GatePosture.from_settings(
        Settings(build_posture="lab", blocking_gates="security, web_polish")
    )

    assert posture.blocks("security") is True
    assert posture.blocks("web_polish") is True
    assert posture.blocks("proof_ladder") is False


def test_unknown_posture_value_falls_back_to_release():
    # Fail safe, not fail open: a typo must not silently disable every gate.
    posture = GatePosture(posture="lab").from_settings(
        SimpleNamespace(build_posture="labb", blocking_gates="")
    )

    assert posture.posture == "release"
    assert posture.blocks("security") is True


def test_settings_like_object_without_the_field_defaults_to_release():
    # The migration guarantee: the ~11 SimpleNamespace settings fakes in this
    # suite predate build_posture and must keep 2.0 blocking behaviour.
    posture = GatePosture.from_settings(SimpleNamespace())

    assert posture.posture == "release"
    assert posture.blocks("security") is True
    assert posture.blocks("proof_ladder") is True


def test_settings_field_itself_defaults_to_lab(monkeypatch):
    # The product decision, distinct from the getattr compatibility floor above.
    # conftest pins SKYN3T_BUILD_POSTURE=release for the suite, so clear it to
    # observe the field's own default the way a real build would.
    monkeypatch.delenv("SKYN3T_BUILD_POSTURE", raising=False)

    assert Settings().build_posture == "lab"
    assert GatePosture.from_settings(Settings()).posture == "lab"


def test_gate_classification_sets_are_disjoint():
    assert not (_ALWAYS_BLOCKING & _RELEASE_ONLY)


def test_unknown_gate_name_blocks_only_in_release():
    # An unclassified name must not be silently non-blocking in release.
    assert GatePosture(posture="release").blocks("something_new") is True
    assert GatePosture(posture="lab").blocks("something_new") is False


def test_blank_and_none_gate_names_are_tolerated():
    posture = GatePosture(posture="lab")

    assert posture.blocks("") is False
    assert posture.blocks(None) is False  # type: ignore[arg-type]
