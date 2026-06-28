"""Gameplay specialist checks (roadmap #8) — behavioral, conservative, advisory.

These review a generated game's pure sim for a class of defect the headless invariant
gate (NaN/pool-leak/determinism/pause/game-over) does not. They feed the fix-loop as
GUIDANCE and never hard-block a build — so a (very unlikely) false positive can at
worst cost a bounded repair pass, never a false ``no_go``. Each check never raises.

First check: **input-wiring**. A game whose pure sim does not RESPOND to any input is
uncontrollable — a real cheap-model defect. This is checked by RUNNING the sim (via
the headless gate's Node harness), not by parsing its text: the harness holds each
contract control (left/right/up/down/action) and reports which ones change the
trajectory vs a no-input baseline. Run-don't-parse is deliberate — the previous
regex parser was repeatedly fooled by real generated code (a doc-comment that
mentioned ``step(...)``, a renamed param, an off-contract field name). Behavior can't
be fooled that way: a control that moves the game is wired; one that doesn't isn't.

When the sim couldn't be RUN (no pure core, no ``node``, a runtime throw before the
probe) the check degrades open (no gap) — a controllable game is never false-flagged.
"""

from __future__ import annotations

from pathlib import Path


def check_input_wiring(project_dir: str | Path, *, gate=None) -> str | None:
    """Return a fix-loop gap string if the game's pure sim does not RESPOND to any
    input control, else ``None``.

    Behavioral (run-don't-parse): it consults the headless gate's ``inputResponsive``
    signal, produced by holding each contract control and watching whether the sim's
    trajectory diverges from a no-input baseline. Pass an already-computed ``gate``
    (:class:`~skyn3t.studio.headless_gate.HeadlessGateResult`) to reuse its single sim
    run; otherwise the gate is run here.

    Degrades open (``None``) whenever the sim could not be run to a verdict — no pure
    sim core, no ``node``, or a runtime throw before the probe — so a controllable
    game is never false-flagged. Never raises.
    """
    try:
        if gate is None:
            from skyn3t.studio.headless_gate import run_headless_gate

            gate = run_headless_gate(project_dir)
        # No runnable sim core / no node / couldn't launch -> can't judge -> open.
        if not getattr(gate, "applicable", False):
            return None
        report = getattr(gate, "report", None) or {}
        # The probe didn't run (e.g. the sim threw before section 7) -> degrade open.
        if "inputResponsive" not in report:
            return None
        # At least one contract control changes the simulation -> controllable.
        if report.get("inputResponsive"):
            return None
    except Exception:  # noqa: BLE001 - a checker must never break a build
        return None
    return (
        "the game IGNORES player input — holding each control (left/right/up/down/"
        "action) leaves its pure simulation's trajectory identical to no input, so "
        "the game is uncontrollable. In step(state, input, dt), read the input "
        "booleans (input.left / input.right / input.up / input.down / input.action) "
        "and move/act on them."
    )
