"""Integration: the game visual repair loop against the REAL node headless gate and a
REAL filesystem rollback (the unit tests in test_game_visual_loop.py stub both).

These prove the do-no-harm guarantee end-to-end on the machinery that stubs can't:
- the loop obtains a real verdict from the Node sim harness (run via asyncio.to_thread);
- a visually-"better" repair that BREAKS sim correctness is caught by the real gate and
  the whole tree is restored byte-for-byte.

Node-requiring; skipped when ``node`` is unavailable. The vision JUDGE is still stubbed so
the control flow is deterministic (no network / no flaky vision sampling).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from skyn3t.studio.game_visual_check import GameVisualVerdict
from skyn3t.studio.game_visual_loop import repair_game_visual
from skyn3t.studio.headless_gate import run_headless_gate

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

# A pure, deterministic, finite sim core the headless gate runs to a PASS (reads input,
# win reachable via score, honours paused/over).
GOOD_SIM = """
export function createState(seed = 1) {
  let s = (seed >>> 0) || 1;
  return { x: 0, score: 0, over: false, paused: false, _s: s };
}
export function step(state, input = {}, dt = 1 / 60) {
  if (state.paused || state.over) return state;
  if (input.left) state.x -= 60 * dt;
  if (input.right) state.x += 60 * dt;
  if (input.action) state.score += 1;
  state.x += 60 * dt;
  if (state.score >= 5) state.over = true;
  return state;
}
"""

# Same exports, but step() forces a NaN at runtime — the real gate must flag it.
NAN_SIM = """
export function createState(seed = 1) {
  return { x: 0, t: 0, over: false, paused: false };
}
export function step(state, input = {}, dt = 1 / 60) {
  state.t += 1;
  state.x = (state.x * 0) / 0; // NaN
  return state;
}
"""


def _scaffold(root: Path, sim: str) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "sim.js").write_text(sim)
    (root / "src" / "main.js").write_text(
        "// entry\nimport { createState, step } from './sim.js';\n")
    (root / "src" / "renderer.js").write_text("// draw\n")
    (root / "package.json").write_text(
        '{"name":"itest","scripts":{"dev":"vite","build":"vite build"}}')
    (root / "index.html").write_text("<html><body><div id=app></div></body></html>")


class _Judge:
    def __init__(self, verdicts):
        self._v = list(verdicts)
        self.calls = 0

    async def __call__(self, p):
        v = self._v[min(self.calls, len(self._v) - 1)]
        self.calls += 1
        return v


def _v(pop=True, read=True, issues=None):
    return GameVisualVerdict(populated=pop, entities_readable_size=read,
                             issues=list(issues or []))


def _real_gate(p):
    # the production wiring offloads the Node subprocess off the event loop
    return asyncio.to_thread(run_headless_gate, p)


def _run(root: Path, *, sim: str, improver_writes: str, judge_seq, attempts=2):
    _scaffold(root, sim)
    base_gate = run_headless_gate(root)
    assert base_gate.applicable and base_gate.passed, "fixture sim should pass the gate"
    original = (root / "src" / "sim.js").read_text()

    async def improver(gaps, files):
        (root / "src" / "sim.js").write_text(improver_writes)
        return True

    res = asyncio.run(repair_game_visual(
        root, settings=object(), gate=base_gate, judge=_Judge(judge_seq),
        run_improver=improver, run_gate=_real_gate, build_check=None, attempts=attempts,
    ))
    return res, original, (root / "src" / "sim.js").read_text()


@requires_node
def test_real_gate_keeps_a_correct_visual_repair(tmp_path: Path):
    # empty -> good, and the edited sim still PASSES the real node gate => kept.
    res, _orig, final_sim = _run(
        tmp_path, sim=GOOD_SIM, improver_writes=GOOD_SIM + "\n// more entities spawned\n",
        judge_seq=[_v(pop=False, issues=["empty play field"]), _v()])
    assert res.repaired is True
    assert "more entities spawned" in final_sim
    assert res.rounds[0].gate_ok is True and res.rounds[0].accepted is True


@requires_node
def test_real_gate_rolls_back_a_correctness_breaking_repair(tmp_path: Path):
    # The repair LOOKS better (empty -> good) but injects a NaN. The REAL gate must
    # catch it and the tree must be restored byte-for-byte.
    res, original, final_sim = _run(
        tmp_path, sim=GOOD_SIM, improver_writes=NAN_SIM,
        judge_seq=[_v(pop=False, issues=["empty"]), _v()])
    assert res.repaired is False
    assert res.rounds[0].gate_ok is False  # the real node gate flagged the NaN
    assert final_sim == original           # byte-for-byte rollback


@requires_node
def test_rolls_back_a_non_improving_repair_on_a_real_tree(tmp_path: Path):
    # Still empty after the edit => not improved => rolled back (real filesystem).
    res, original, final_sim = _run(
        tmp_path, sim=GOOD_SIM, improver_writes=GOOD_SIM + "\n// noop\n",
        judge_seq=[_v(pop=False, issues=["empty"]), _v(pop=False, issues=["empty"])])
    assert res.repaired is False
    assert final_sim == original
