"""Headless invariant gate — runs a game's pure sim core (src/sim.js) in Node
for N seeded ticks and asserts hard invariants (roadmap #5).

The gate is a sibling to ``proof_run``: objective, offline, never raises. These
tests pin the blocking battery with MINIMAL bad fixtures — each sim.js trips
exactly one check — plus the known-good scaffold sim as the control, the
skip-when-absent path, and the fix-loop feedback contract.

Node-requiring tests are skipped when ``node`` is unavailable; the pure-Python
contract tests always run.
"""

from __future__ import annotations

import shutil

import pytest

from skyn3t.studio.headless_gate import HeadlessGateResult, run_headless_gate

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _write(root, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


# A minimal sim.js trips exactly ONE blocking check. Helpers below.
_GOODISH = (
    "export function createState(seed){ return {x:0, score:0, hp:3, rng:seed>>>0, "
    "paused:false, over:false} }\n"
    "function rnd(s){ s.rng = (s.rng*1664525 + 1013904223)>>>0; return s.rng/4294967296 }\n"
    "export function step(s, input, dt){\n"
    "  if (s.paused || s.over) return s\n"
    "  if (input.right) s.x += 60*dt\n"
    "  if (input.left)  s.x -= 60*dt\n"
    "  if (input.action) s.score += 1\n"
    "  if (s.score >= 1000000) s.score = 1000000\n"
    "  return s\n"
    "}\n"
    "export function isWin(s){ return s.score >= 10 }\n"
    "export function isLose(s){ return s.hp <= 0 }\n"
)

_NAN = (
    "export function createState(seed){ return {x:0, rng:seed} }\n"
    "export function step(s){ s.x = s.x + (0/0); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

_EXPLODE = (
    "export function createState(seed){ return {x:1, rng:seed} }\n"
    "export function step(s){ s.x = s.x * 2; return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

_POOL_LEAK = (
    "export function createState(seed){ return {items:[], rng:seed} }\n"
    "export function step(s){ for(let i=0;i<100;i++) s.items.push(1); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

_BIG_STATIC = (  # a large-but-STATIC array (e.g. a fixed tilemap) — NOT a leak
    "export function createState(seed){ const tiles=[]; for(let i=0;i<12000;i++) tiles.push(0); "
    "return {tiles, score:0, rng:seed} }\n"
    "export function step(s){ return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

_NONDETERMINISTIC = (
    "export function createState(seed){ return {x:0} }\n"
    "export function step(s){ s.x = Math.random(); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

_PAUSE_IGNORER = (
    "export function createState(seed){ return {x:0, paused:false, rng:seed} }\n"
    "export function step(s){ s.x += 1; return s }\n"  # ignores paused
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

_GAMEOVER_IGNORER = (
    "export function createState(seed){ return {x:0, over:false, rng:seed} }\n"
    "export function step(s, input){ if(input.action) s.x += 1; return s }\n"  # ignores over
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A real NaN violation in state that ALSO contains a circular reference. The
# determinism snapshot must not throw on the cycle and discard the found NaN.
_NAN_WITH_CYCLE = (
    "export function createState(seed){ const p={name:'root',kids:[],hp:0/0}; "
    "const c={name:'c',parent:p}; p.kids.push(c); return {seed,p,t:0} }\n"
    "export function step(s){ s.t += 1; return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A genuine pool leak hidden behind a LARGER static array — per-array-path leak
# detection must catch the growing 'pool' despite the bigger static 'tiles'.
_MASKED_LEAK = (
    "export function createState(seed){ return {tiles:new Array(8000).fill(0), pool:[], rng:seed} }\n"
    "export function step(s){ for(let i=0;i<20;i++) s.pool.push(1); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# State held in a Map — the scan must walk Map values (not just plain objects),
# else a NaN inside it is invisible to the whole battery.
_MAP_NAN = (
    "export function createState(seed){ return {entities:new Map(), rng:seed} }\n"
    "export function step(s){ s.entities.set('a', {x: 0/0}); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A legit uint32 seeded-RNG field whose NAME doesn't match SKIP_MAG. The raised
# magnitude cap must not false-flag it as an explosion.
_RNG_NAMED = (
    "export function createState(seed){ return {randomState:(seed>>>0)||1, x:0} }\n"
    "export function step(s){ s.randomState=(s.randomState*1664525+1013904223)>>>0; "
    "s.x=s.randomState%800; return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A sim file that exists but does NOT export the contract — attributable to the
# build, so it must BLOCK (not degrade-open like a genuine infra failure).
_NO_EXPORTS = "export const value = 42\n"

# A sim that is not pure (imports Phaser) — import fails in Node -> blocks.
_IMPURE = (
    "import Phaser from 'phaser'\n"
    "export function createState(seed){ return {} }\n"
    "export function step(s){ return s }\n"
)


# ---- pure-Python contract (no node) --------------------------------------
def test_gate_skips_gracefully_when_no_sim(tmp_path):
    res = run_headless_gate(tmp_path)
    assert isinstance(res, HeadlessGateResult)
    # No sim core → not applicable, but NOT a failure (un-gated, not failed).
    assert res.applicable is False
    assert res.passed is True
    assert res.violations == []


def test_error_gaps_one_feedback_line_per_violation():
    res = HeadlessGateResult(
        applicable=True,
        passed=False,
        violations=["NaN in state.x after 3 ticks", "pool leak: items reached 10001"],
        report={},
        detail={},
    )
    gaps = res.error_gaps()
    assert len(gaps) == 2
    assert all(isinstance(g, str) and g for g in gaps)
    # The original violation text is carried into the fix-loop feedback.
    assert any("NaN" in g for g in gaps)
    assert any("pool leak" in g for g in gaps)


def test_passing_result_has_no_error_gaps():
    res = HeadlessGateResult(
        applicable=True, passed=True, violations=[], report={}, detail={}
    )
    assert res.error_gaps() == []


# ---- node-backed: known-good control -------------------------------------
@requires_node
def test_gate_passes_scaffold_sim(tmp_path):
    from skyn3t.agents._scaffold import scaffold_for

    _write(tmp_path, scaffold_for("phaser", "dino-run", "a dino jump game"))
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is True, res.violations


@requires_node
def test_gate_passes_minimal_good_sim(tmp_path):
    _write(tmp_path, {"src/sim.js": _GOODISH})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is True, res.violations


@requires_node
def test_gate_does_not_flag_large_static_array(tmp_path):
    # A fixed 12k-element tilemap is large but NOT growing — the leak check is
    # growth-based, so this must PASS (regression guard against an absolute cap).
    _write(tmp_path, {"src/sim.js": _BIG_STATIC})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is True, res.violations


# ---- node-backed: each bad fixture trips its own check -------------------
@requires_node
@pytest.mark.parametrize(
    "sim, marker",
    [
        (_NAN, "nan"),
        (_EXPLODE, "explos"),
        (_POOL_LEAK, "pool"),
        (_NONDETERMINISTIC, "determinism"),
        (_PAUSE_IGNORER, "pause"),
        (_GAMEOVER_IGNORER, "game-over"),
    ],
)
def test_gate_flags_bad_sim(tmp_path, sim, marker):
    _write(tmp_path, {"src/sim.js": sim})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is False, "bad sim should fail the gate"
    blob = " ".join(res.violations).lower()
    assert marker in blob, f"expected a '{marker}' violation, got: {res.violations}"


@requires_node
def test_gate_violations_feed_fix_loop(tmp_path):
    _write(tmp_path, {"src/sim.js": _NAN})
    res = run_headless_gate(tmp_path, ticks=120)
    assert res.passed is False
    gaps = res.error_gaps()
    assert gaps and all(isinstance(g, str) for g in gaps)


# ---- node-backed: adversarial-review regressions -------------------------
@requires_node
def test_gate_keeps_violation_when_a_later_check_would_throw(tmp_path):
    # A NaN found early must NOT be discarded if a later snapshot hits a cycle
    # (the snapshot serializer is cycle-safe; the battery is try/wrapped).
    _write(tmp_path, {"src/sim.js": _NAN_WITH_CYCLE})
    res = run_headless_gate(tmp_path, ticks=120)
    assert res.applicable is True, res.detail
    assert res.passed is False
    assert any("nan" in v.lower() for v in res.violations), res.violations


@requires_node
def test_gate_catches_leak_masked_by_larger_static_array(tmp_path):
    _write(tmp_path, {"src/sim.js": _MASKED_LEAK})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.passed is False
    assert any("pool" in v.lower() for v in res.violations), res.violations


@requires_node
def test_gate_scans_inside_map_state(tmp_path):
    _write(tmp_path, {"src/sim.js": _MAP_NAN})
    res = run_headless_gate(tmp_path, ticks=120)
    assert res.passed is False
    assert any("nan" in v.lower() for v in res.violations), res.violations


@requires_node
def test_gate_does_not_flag_named_rng_field(tmp_path):
    # A uint32 RNG field named 'randomState' (misses SKIP_MAG) must not be a
    # false explosion — the raised magnitude cap covers any uint32.
    _write(tmp_path, {"src/sim.js": _RNG_NAMED})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is True, res.violations


@requires_node
def test_broken_sim_blocks_rather_than_skips(tmp_path):
    # A sim file present but missing the contract is attributable -> blocks.
    _write(tmp_path, {"src/sim.js": _NO_EXPORTS})
    res = run_headless_gate(tmp_path)
    assert res.applicable is True
    assert res.passed is False
    assert res.violations


@requires_node
def test_impure_sim_blocks(tmp_path):
    # A sim that imports Phaser is not pure — import fails in Node -> blocks.
    _write(tmp_path, {"src/sim.js": _IMPURE})
    res = run_headless_gate(tmp_path)
    assert res.applicable is True
    assert res.passed is False
