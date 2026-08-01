"""Build posture — whether a gate BLOCKS the verdict or merely REPORTS.

This is a personal lab. The verification machinery is valuable as *evidence*;
it is not valuable as a row of veto points that turn a delivered, running app
into ``no_go`` because Docker is not installed, a regex matched a placeholder
API key, or a heuristic decided a platform-skipped test was reward hacking.

Posture separates two concerns that were previously fused into one boolean per
gate:

* ``<gate>_enabled`` — does the gate RUN?  (unchanged, still per-gate)
* :class:`GatePosture` — does its finding FLIP ``manifest.verdict``?  (new)

Design rules encoded here:

1. A gate that **could not run** never blocks, in either posture. That is
   :class:`skyn3t.studio.gate_verdict.GateVerdict`'s ``skipped`` contract
   ("a could-not-run gate must never false-flag") generalised to every gate,
   including the ones that never adopted it.
2. A gate that proves the **delivery is broken** blocks in every posture. An
   app that cannot boot, has unresolved imports, or delivered nothing is not
   "a bit judgy" — it is not a build. See ``_ALWAYS_BLOCKING``.
3. Everything else — heuristics, taste rules, environment-dependent probes,
   and routing/policy preferences — records its finding, dampens the score,
   feeds the fix loop, and lets the build complete. See ``_RELEASE_ONLY``.

Sibling of :class:`skyn3t.studio.lab_policy.LabAutonomyPolicy`, and
deliberately orthogonal to it: that one governs *side effects* (approvals,
budget, irreversible external actions such as remote deploys); this one governs
*the verdict*. Blocking a build and preventing an irreversible action are
different axes. Neither may weaken the other, and neither consults the other.

Import has zero side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Gates proving the DELIVERY IS BROKEN. Blocking in EVERY posture.
_ALWAYS_BLOCKING = frozenset(
    {
        "proof",  # ProofResult.passed — syntax, boot, imports, native build
        "delivery",  # nothing was delivered
        "entrypoint",  # no runnable entrypoint
        "final_consistency",  # a later stage broke imports/exports
    }
)
# Deliberately NOT a member: "native_build". A failed npm/swift/wheel build is
# enforced inside proof_run (it stays blocking there even under lab posture) and
# reaches the verdict as the "proof" signal, so a separate name here would be a
# classification the runner never emits — see
# tests/test_gate_name_drift.py::test_delivery_integrity_gates_are_reachable_from_the_runner,
# which fails if this set drifts away from the names actually in use.

# Gates that are heuristics, taste rules, environment-dependent probes, or
# policy preferences. Release-only: they record in lab, block in release.
_RELEASE_ONLY = frozenset(
    {
        "ai_native",
        "checklist",
        "code_degraded",
        "critic",
        # Release-only by CLASSIFICATION, but forced blocking in every posture
        # while the game_quality_gates_verdict master switch is ON (see
        # ``from_settings``) — that switch is documented as a hard no_go on a
        # REAL, non-skipped visual/playtest failure.
        "game_quality",
        "generated_tests",
        "headless_gate",
        "intent",
        "liveness",
        "native_llm",
        "product_quality",
        "proof_ladder",
        "requirement_trace",
        "reward_hacking",
        "ruff",
        "scaffold_stub",
        "security",
        "substance",
        # A reachability heuristic inside proof_run: it must recognise an entry
        # convention, so an unfamiliar project shape makes it accuse a working
        # app. Declared here so the drift test sees the classification.
        "unwired_components",
        "verify_boot",
        "verify_build",
        "visual_self_heal",
        "web_polish",
    }
)

#: Every gate name the runner may pass to ``GatePosture.blocks``. Exported so a
#: drift test can assert the runner never invents an unclassified name.
KNOWN_GATES = _ALWAYS_BLOCKING | _RELEASE_ONLY

# Classified above for posture bookkeeping, but the runner never passes these
# names to ``blocks`` — their findings fold into other signals (``verify_boot``
# and ``reward_hacking`` -> ``verify_build``; ``ruff`` / ``unwired_components``
# / ``generated_tests`` -> ``proof``; ``checklist`` -> the acceptance contract).
# A ``blocking_gates`` entry naming one of them silently does nothing, so
# ``from_settings`` warns loudly instead of accepting it quietly.
_NEVER_EMITTED = frozenset(
    {
        "checklist",
        "generated_tests",
        "reward_hacking",
        "ruff",
        "unwired_components",
        "verify_boot",
    }
)

_VALID_POSTURES = frozenset({"lab", "release"})


@dataclass(frozen=True, slots=True)
class GatePosture:
    """Resolved once per build and threaded, never re-read mid-build.

    Threading (rather than re-reading ``settings``) matches ``LabAutonomyPolicy``
    and means a live settings edit cannot change the rules underneath a build
    that is already running.
    """

    posture: str = "lab"
    forced_blocking: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, settings: Any) -> GatePosture:
        """Resolve from a settings object.

        Note the deliberate asymmetry: the ``Settings`` FIELD defaults to
        ``"lab"`` (the product decision — this is a lab), but this ``getattr``
        defaults to ``"release"`` (the compatibility floor). A settings-*like*
        object that predates the field — every ``SimpleNamespace`` fake in the
        test suite — therefore keeps the historical blocking behaviour and
        cannot be silently softened by this change.
        """
        raw = str(getattr(settings, "build_posture", "release") or "release").strip().lower()
        posture = raw if raw in _VALID_POSTURES else "release"
        forced = frozenset(
            part.strip().lower()
            for part in str(getattr(settings, "blocking_gates", "") or "").split(",")
            if part.strip()
        )
        noop = forced & _NEVER_EMITTED
        if noop:
            log.warning(
                "gate_posture.blocking_gates_noop",
                gates=sorted(noop),
                hint="the runner never emits these gate names; their findings "
                "fold into other gates, so the entries block nothing",
            )
        # The documented game-quality master switch
        # (``settings.game_quality_gates_verdict``, default ON) promises a hard
        # no_go on a REAL visual/playtest failure. It must outrank the posture
        # default, exactly like an entry in ``blocking_gates``. Skipped checks
        # still never block: the runner's ``_game_quality_gate_ok`` returns
        # ok=True for skipped/gates-off before ``blocks`` is ever consulted.
        # ``getattr`` defaults to False so settings-like fakes without the
        # field are untouched.
        if bool(getattr(settings, "game_quality_gates_verdict", False)):
            forced = forced | {"game_quality"}
        return cls(posture=posture, forced_blocking=forced)

    @property
    def is_release(self) -> bool:
        return self.posture == "release"

    def blocks(self, gate: str) -> bool:
        """Whether a FAILED finding from ``gate`` should flip the verdict.

        A gate that could not run must not be routed here at all — callers pass
        ``ok=None`` to the runner's recorder for that case.
        """
        name = str(gate or "").strip().lower()
        if name in _ALWAYS_BLOCKING:
            return True
        if name in self.forced_blocking:
            return True
        return self.is_release
