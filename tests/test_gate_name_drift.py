"""Every gate name the runner uses must be classified in gate_posture.

gate_posture.py exports KNOWN_GATES and its docstring promises exactly this
test. Without it the failure is silent by construction: GatePosture.blocks()
returns ``self.is_release`` for any unrecognised name, so a typo'd gate name in
a _gate_outcome call is indistinguishable from a passing gate under the default
``lab`` posture — an _ALWAYS_BLOCKING gate could quietly lose its veto.

This scans the runner's SOURCE rather than exercising it, because the gate call
sites live deep inside a build and there is no cheap way to reach them all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from skyn3t.studio.gate_posture import _ALWAYS_BLOCKING, KNOWN_GATES, GatePosture

_RUNNER = Path(__file__).resolve().parents[1] / "skyn3t" / "studio" / "runner.py"


def _gate_names_in_runner() -> set[str]:
    """Gate-name string literals the runner hands to _gate_outcome.

    Two shapes: direct calls ``self._gate_outcome(manifest, "security", ...)``
    and the ``_signals`` tuple-of-tuples folded over near the final verdict.
    """
    tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
    names: set[str] = set()

    for node in ast.walk(tree):
        # Direct call sites: the gate name is the 2nd positional argument.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_gate_outcome"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value)

        # The folded signal table: _signals = (("proof", ok, reason), ...).
        # Handle BOTH Assign and AnnAssign — the real declaration carries a type
        # annotation, and an Assign-only scan silently found nothing.
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.AnnAssign):
                targets = [node.target.id] if isinstance(node.target, ast.Name) else []
            else:
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_signals" in targets and isinstance(node.value, ast.Tuple):
                for element in node.value.elts:
                    if (
                        isinstance(element, ast.Tuple)
                        and element.elts
                        and isinstance(element.elts[0], ast.Constant)
                        and isinstance(element.elts[0].value, str)
                    ):
                        names.add(element.elts[0].value)
    return names


def test_scanner_actually_finds_the_call_sites():
    """Guard the guard: a scanner that silently finds nothing proves nothing."""
    found = _gate_names_in_runner()

    assert len(found) >= 10, f"only found {sorted(found)} — scanner is broken"
    # Sanity: names we know are wired.
    assert {"proof", "security", "web_polish", "proof_ladder"} <= found


def test_every_runner_gate_name_is_classified():
    unclassified = sorted(_gate_names_in_runner() - KNOWN_GATES)

    assert not unclassified, (
        f"runner uses gate name(s) not classified in gate_posture: {unclassified}. "
        "An unclassified name silently inherits release-only semantics, so under "
        "the default lab posture it would never block."
    )


def test_delivery_integrity_gates_are_reachable_from_the_runner():
    """The always-blocking set must not drift into being unreferenced.

    If a gate is declared always-blocking but the runner never emits it, the
    guarantee is theatre.
    """
    found = _gate_names_in_runner()
    missing = sorted(_ALWAYS_BLOCKING - found)

    assert not missing, (
        f"declared always-blocking but never emitted by the runner: {missing}"
    )


@pytest.mark.parametrize("gate", sorted(_ALWAYS_BLOCKING))
def test_always_blocking_gates_block_in_lab(gate):
    assert GatePosture(posture="lab").blocks(gate) is True
