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

import asyncio
import shutil

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.headless_gate import HeadlessGateResult, run_headless_gate
from skyn3t.studio.planner import Planner
from skyn3t.studio.runner import StudioRunner

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

# A bounded array that first appears AFTER the half-way point (lazy level load).
# Its baseline must be its first-sighting size, not 0, so it is NOT a false leak.
_LATE_STATIC_ARRAY = (
    "export function createState(seed){ return {t:0, rng:seed} }\n"
    "export function step(s){ s.t += 1; if(s.t===200 && !s.grid){ s.grid=new Array(5000).fill(0) } return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# State holds a Map with a reference cycle AND is non-deterministic. The snapshot
# must tolerate the Map cycle (not stack-overflow) so the determinism check still
# RUNS and catches the Math.random.
_MAP_CYCLE_NONDET = (
    "export function createState(seed){ const m=new Map(); m.set('self', m); return {m, x:0} }\n"
    "export function step(s){ s.x = Math.random(); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A leak in a non-last sibling of an array-of-arrays — must not be masked by the
# last-write-wins collapse of the shared '[]' path key.
_NESTED_LEAK = (
    "export function createState(seed){ return {lanes:[[], [0,0,0]], rng:seed} }\n"
    "export function step(s){ for(let i=0;i<20;i++) s.lanes[0].push(1); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# An array declared empty at start then BULK-LOADED once (lazy level load) to a
# bounded size — NOT a leak. Baseline = first non-zero size, so it must PASS.
_LAZY_BULK_LOAD = (
    "export function createState(seed){ return {grid:[], t:0, rng:seed} }\n"
    "export function step(s){ s.t+=1; if(s.t===200 && s.grid.length===0){ "
    "for(let i=0;i<5000;i++) s.grid.push(0) } return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A leaking array under a state field literally named 'constructor' — must still
# be caught (null-proto maps, so the inherited 'constructor' doesn't mask it).
_PROTO_KEY_LEAK = (
    "export function createState(seed){ return {constructor:[], rng:seed} }\n"
    "export function step(s){ for(let i=0;i<100;i++) s.constructor.push(1); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A real NaN violation in a sim that ALSO writes to stdout (a ubiquitous debug
# log). Parsing the sim's stdout as the verdict would corrupt the JSON and discard
# the NaN — the result must travel a private channel the sim can't pollute.
_STDOUT_NOISE_NAN = (
    "export function createState(seed){ return {x:0, rng:seed} }\n"
    "export function step(s){ console.log('dbg tick'); s.x += 0/0; return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A perfectly good sim that also logs — must still pass (the log must NOT make the
# gate degrade open or block).
_STDOUT_NOISE_GOOD = (
    "export function createState(seed){ return {x:0, score:0, rng:seed>>>0} }\n"
    "export function step(s, input, dt){ console.log('frame'); "
    "if(input.right) s.x += 60*dt; if(input.action) s.score += 1; return s }\n"
    "export function isWin(s){ return s.score >= 10 }\n"
    "export function isLose(s){ return false }\n"
)

# A sim that THROWS at runtime (null deref). The most basic runtime defect — it
# must BLOCK (attributable to the build), not crash the harness into a degrade-open.
_THROWS_AT_RUNTIME = (
    "export function createState(seed){ return {x:0, e:null, rng:seed} }\n"
    "export function step(s){ s.x += s.e.value; return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A sim that HANGS (infinite loop). With a short timeout it must be attributed to
# the build and BLOCK — a 600-tick pure sim has no business taking seconds.
_HANGS_AT_RUNTIME = (
    "export function createState(seed){ return {x:0, rng:seed} }\n"
    "export function step(s){ while(true){} return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# Non-determinism sourced from the Date CONSTRUCTOR (not Date.now). The
# determinism check must stub the constructor too, else this escapes to a 'go'.
_DATE_CTOR_NONDET = (
    "export function createState(seed){ return {x:0} }\n"
    "export function step(s){ s.x = (new Date().getTime()) % 7; return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A sim that calls BARE Date() (legal — real Date() returns a string, never
# throws) AND is non-deterministic. A class-based Date stub throws on bare Date()
# and that throw, swallowed by the determinism try, silently disables the WHOLE
# battery — so the Date.now non-determinism escapes. The stub must be callable.
_BARE_DATE_NONDET = (
    "export function createState(seed){ return {x:0, label:''} }\n"
    "export function step(s){ s.label = Date(); s.x = Date.now() % 7; return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)

# A deterministic sim that constructs Date WITH args (a fixed date). The time stub
# must preserve multi-arg construction so this stays a clean pass (guard against
# the stub breaking legitimate Date use).
_DATE_ARGS_OK = (
    "export function createState(seed){ return {y:0, rng:seed} }\n"
    "export function step(s){ s.y = new Date(2020, 0, 1).getFullYear(); return s }\n"
    "export function isWin(s){ return false }\n"
    "export function isLose(s){ return false }\n"
)


# ---- pure-Python contract (no node) --------------------------------------
def test_gate_skips_gracefully_when_no_sim(tmp_path):
    res = run_headless_gate(tmp_path)
    assert isinstance(res, HeadlessGateResult)
    # No sim core → not applicable, but NOT a failure (un-gated, not failed).
    assert res.applicable is False
    assert res.passed is True
    assert res.violations == []


def test_find_sim_locates_nested_sim_core(tmp_path):
    # FIX A: a game that splits its sim into src/sim/sim.js must be FOUND, not
    # reported as "no pure sim core". A verifier's error REASON is part of its
    # contract — a misleading reason sends the fix-loop chasing the wrong defect.
    from skyn3t.studio.headless_gate import _find_sim

    _write(tmp_path, {"src/sim/sim.js": _GOODISH})
    assert _find_sim(tmp_path) == tmp_path / "src/sim/sim.js"


def test_find_sim_locates_sim_index(tmp_path):
    # A sim folder with an index entry is just as valid a split.
    from skyn3t.studio.headless_gate import _find_sim

    _write(tmp_path, {"src/sim/index.js": _GOODISH})
    assert _find_sim(tmp_path) == tmp_path / "src/sim/index.js"


def test_find_sim_prefers_canonical_over_nested(tmp_path):
    # When both exist, the canonical src/sim.js wins (most-specific-first order).
    from skyn3t.studio.headless_gate import _find_sim

    _write(tmp_path, {"src/sim.js": _GOODISH, "src/sim/sim.js": _GOODISH})
    assert _find_sim(tmp_path) == tmp_path / "src/sim.js"


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
def test_gate_runs_nested_sim_core(tmp_path):
    # FIX A end-to-end: a located nested sim is GATED (applicable), not skipped
    # open — so the gate stays load-bearing for games that split src/sim/.
    _write(tmp_path, {"src/sim/sim.js": _GOODISH})
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


# ---- node-backed: fix-round-2 regressions (re-review of the harness rewrite) --
@requires_node
def test_gate_does_not_flag_late_spawned_bounded_array(tmp_path):
    # An array that first appears after the midpoint at a bounded size is NOT a
    # leak — its baseline is the first-sighting size, not 0. (False-positive guard.)
    _write(tmp_path, {"src/sim.js": _LATE_STATIC_ARRAY})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is True, res.violations


@requires_node
def test_gate_handles_map_cycle_and_still_runs_determinism(tmp_path):
    # A Map reference cycle must not stack-overflow the snapshot and silently skip
    # the determinism check — the Math.random must still be caught.
    _write(tmp_path, {"src/sim.js": _MAP_CYCLE_NONDET})
    res = run_headless_gate(tmp_path, ticks=120)
    assert res.passed is False
    assert any("determinism" in v.lower() for v in res.violations), res.violations


@requires_node
def test_gate_catches_leak_in_nested_array_sibling(tmp_path):
    # A leak in a non-last inner array (array-of-arrays) must not be masked by the
    # last-write-wins collapse of the shared '[]' path key.
    _write(tmp_path, {"src/sim.js": _NESTED_LEAK})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.passed is False
    assert any("pool" in v.lower() for v in res.violations), res.violations


@requires_node
def test_gate_does_not_flag_lazy_bulk_loaded_array(tmp_path):
    # An array empty at start then bulk-loaded once to a bounded size is NOT a
    # leak (baseline = first non-zero size). Blocking-false-positive guard.
    _write(tmp_path, {"src/sim.js": _LAZY_BULK_LOAD})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is True, res.violations


@requires_node
def test_gate_catches_leak_under_prototype_named_field(tmp_path):
    # A leak in a field named 'constructor' must still be caught — the per-path
    # maps are null-proto so the inherited key doesn't mask it.
    _write(tmp_path, {"src/sim.js": _PROTO_KEY_LEAK})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.passed is False
    assert any("pool" in v.lower() for v in res.violations), res.violations


# ---- node-backed: result-channel + crash/hang seals (debug-team findings) ----
@requires_node
def test_sim_stdout_noise_does_not_discard_violation(tmp_path):
    # A sim that logs to stdout must NOT corrupt the verdict channel: the NaN it
    # also produces must still be caught (the result travels a private file, not
    # the sim-polluted stdout).
    _write(tmp_path, {"src/sim.js": _STDOUT_NOISE_NAN})
    res = run_headless_gate(tmp_path, ticks=120)
    assert res.applicable is True, res.detail
    assert res.passed is False, "a logging sim's NaN must not be discarded"
    assert any("nan" in v.lower() for v in res.violations), res.violations


@requires_node
def test_good_sim_that_logs_still_passes(tmp_path):
    # Control: a correct sim that happens to log must remain a clean pass, not
    # degrade open or block on the stdout noise.
    _write(tmp_path, {"src/sim.js": _STDOUT_NOISE_GOOD})
    res = run_headless_gate(tmp_path, ticks=120)
    assert res.applicable is True, res.detail
    assert res.passed is True, res.violations


@requires_node
def test_sim_that_throws_at_runtime_blocks(tmp_path):
    # A located sim that crashes at runtime is attributable to the build — it must
    # BLOCK (applicable=True, passed=False), not degrade open on empty output.
    _write(tmp_path, {"src/sim.js": _THROWS_AT_RUNTIME})
    res = run_headless_gate(tmp_path, ticks=120)
    assert res.applicable is True, res.detail
    assert res.passed is False, "a sim that throws must block, not silently pass"
    assert res.violations


@requires_node
def test_sim_that_hangs_blocks_not_degrades_open(tmp_path):
    # A located sim that hangs past the timeout is attributable to the build — it
    # must BLOCK, not degrade open (a 600-tick pure sim cannot legitimately hang).
    _write(tmp_path, {"src/sim.js": _HANGS_AT_RUNTIME})
    res = run_headless_gate(tmp_path, ticks=600, timeout=3)
    assert res.applicable is True, res.detail
    assert res.passed is False, "a hung sim must block, not silently pass"
    assert res.violations


@requires_node
def test_nondeterminism_via_date_constructor_is_caught(tmp_path):
    # Non-determinism from `new Date()` (not just Date.now) must be caught — the
    # determinism check stubs the Date constructor to a divergent epoch per run.
    _write(tmp_path, {"src/sim.js": _DATE_CTOR_NONDET})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is False
    assert any("determinism" in v.lower() for v in res.violations), res.violations


@requires_node
def test_bare_date_call_does_not_disable_determinism_battery(tmp_path):
    # Regression guard (re-review finding): a sim that calls bare Date() must NOT
    # throw inside the time stub and silently swallow the determinism/pause/
    # game-over battery — the Date.now non-determinism must still be caught.
    _write(tmp_path, {"src/sim.js": _BARE_DATE_NONDET})
    res = run_headless_gate(tmp_path, ticks=300)
    assert res.applicable is True, res.detail
    assert res.passed is False, "bare Date() must not disable the determinism check"
    assert any("determinism" in v.lower() for v in res.violations), res.violations


@requires_node
def test_date_with_args_construction_still_works(tmp_path):
    # Guard: the time stub must preserve multi-arg Date construction — a fixed
    # `new Date(2020,0,1)` is deterministic and must remain a clean pass.
    _write(tmp_path, {"src/sim.js": _DATE_ARGS_OK})
    res = run_headless_gate(tmp_path, ticks=120)
    assert res.applicable is True, res.detail
    assert res.passed is True, res.violations


# ---- runner policy: SEAL the gate on a game stack with no pure sim core ------
# The gate (run_headless_gate) degrades open when there's no src/sim.js — correct
# for a pure verifier. The POLICY that a game stack MUST expose a sim core (so the
# gate actually bites) lives in the runner: a missing core drives the repair loop
# to extract one, and an unrepaired game blocks instead of silently passing.
def _runner():
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def _phaser_plan():
    plan = Planner(Settings()).plan("a dino jump game", "dino")
    plan.stack = "phaser"
    return plan


class _M:
    build_id = "b"
    slug = "dino"
    brief = "a dino jump game"

    def __init__(self):
        self.files = []
        self.extra = {}


_MISSING = HeadlessGateResult(
    applicable=False, passed=True,
    detail={"reason": "no pure sim core (src/sim.js) found"},
)
_INFRA = HeadlessGateResult(
    applicable=False, passed=True, detail={"reason": "node not available"},
)


def test_headless_gate_blocks_policy():
    g = StudioRunner._headless_gate_blocks
    # game stack, NO sim core, AFTER a repair attempt -> BLOCKS (the sealed hole).
    assert g(_MISSING, "phaser", repair_attempted=True)[0] is True
    # no repair was possible (no improver) -> do-no-harm, never block on our own
    # inability.
    assert g(_MISSING, "phaser", repair_attempted=False) == (False, None)
    # a genuine infra skip (no node) -> never blocks, even after a repair attempt.
    assert g(_INFRA, "phaser", repair_attempted=True) == (False, None)
    # a non-game stack is never gated by the headless gate.
    assert g(_MISSING, "nextjs", repair_attempted=True) == (False, None)
    # a passing applicable gate -> fine.
    ok = HeadlessGateResult(applicable=True, passed=True)
    assert g(ok, "phaser", repair_attempted=True) == (False, None)
    # an applicable gate with real violations -> blocks (repair_attempted moot).
    bad = HeadlessGateResult(applicable=True, passed=False, violations=["NaN in state.x"])
    assert g(bad, "phaser", repair_attempted=False)[0] is True
    # gate disabled / None -> never blocks.
    assert g(None, "phaser", repair_attempted=True) == (False, None)


async def test_missing_sim_core_drives_repair_then_blocks_when_unfixed(tmp_path, monkeypatch):
    # THE BUG: a game with logic in main.js (no src/sim.js) made the gate skip and
    # the improver loop never ran -> a silent verdict pass with ZERO invariant
    # verification. It must now drive repair and, if still unfixed, block.
    runner = _runner()
    monkeypatch.setattr(runner, "_has_capability", lambda cap: True)
    seen = {"n": 0, "gaps": None}

    async def fake_submit(task):
        seen["n"] += 1
        seen["gaps"] = task.payload.get("gaps")
        return None  # the improver fails to extract a sim core

    monkeypatch.setattr(runner.orchestrator, "submit", fake_submit)
    monkeypatch.setattr(
        "skyn3t.studio.headless_gate.run_headless_gate", lambda *a, **k: _MISSING
    )

    gate = await runner._headless_gate_pass(_M(), _phaser_plan(), str(tmp_path), "cid", {})
    assert seen["n"] >= 1, "a missing sim core must DRIVE the repair loop"
    assert seen["gaps"] and any("sim.js" in g for g in seen["gaps"]), seen["gaps"]
    assert gate.passed is False, "an unrepaired game stack must block the verdict"


async def test_missing_sim_core_repaired_by_improver_passes(tmp_path, monkeypatch):
    # Once the improver extracts a valid sim core, the gate runs and passes — no
    # block. This proves the loop converges, not just blocks.
    runner = _runner()
    monkeypatch.setattr(runner, "_has_capability", lambda cap: True)
    state = {"fixed": False}
    good = HeadlessGateResult(applicable=True, passed=True, detail={"sim": "src/sim.js"})
    monkeypatch.setattr(
        "skyn3t.studio.headless_gate.run_headless_gate",
        lambda *a, **k: good if state["fixed"] else _MISSING,
    )

    async def fake_submit(task):
        state["fixed"] = True  # the improver extracts a valid sim core
        return None

    monkeypatch.setattr(runner.orchestrator, "submit", fake_submit)

    gate = await runner._headless_gate_pass(_M(), _phaser_plan(), str(tmp_path), "cid", {})
    assert gate.applicable is True and gate.passed is True


async def test_no_improver_capability_keeps_missing_core_nonblocking(tmp_path, monkeypatch):
    # do-no-harm guard: with NO improver to add a sim core, a game without one must
    # NOT no_go — degrade open with a visible note (never block on inability).
    runner = _runner()
    monkeypatch.setattr(runner, "_has_capability", lambda cap: False)
    monkeypatch.setattr(
        "skyn3t.studio.headless_gate.run_headless_gate", lambda *a, **k: _MISSING
    )
    m = _M()
    gate = await runner._headless_gate_pass(m, _phaser_plan(), str(tmp_path), "cid", {})
    assert gate.passed is True
    assert "headless_gate_note" in m.extra


async def test_transient_improver_crash_does_not_block_missing_core(tmp_path, monkeypatch):
    # do-no-harm: a flaky improver (submit raises / stage timeout) that NEVER
    # actually ran must not be credited as a repair attempt — a sim-less game must
    # degrade open, not no_go. (repair_attempted counts COMPLETED submits, not the
    # pre-increment loop counter.)
    runner = _runner()
    monkeypatch.setattr(runner, "_has_capability", lambda cap: True)

    async def boom(task):
        raise asyncio.TimeoutError("improver timed out")

    monkeypatch.setattr(runner.orchestrator, "submit", boom)
    monkeypatch.setattr(
        "skyn3t.studio.headless_gate.run_headless_gate", lambda *a, **k: _MISSING
    )
    m = _M()
    gate = await runner._headless_gate_pass(m, _phaser_plan(), str(tmp_path), "cid", {})
    assert gate.passed is True, "a never-run improver must not block (do-no-harm)"
    assert "headless_gate_note" in m.extra
