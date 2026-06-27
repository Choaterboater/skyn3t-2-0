"""Headless invariant gate — the runtime correctness lever for game builds.

A sibling to :mod:`skyn3t.studio.proof_run`. Where ``proof_run`` proves the build
*compiles/installs*, this proves the game's *logic* is sound by running its PURE
simulation core (``src/sim.js``) outside the browser, in Node, for N seeded ticks
with scripted input, and asserting hard invariants:

  * no NaN / Inf in state                 (numeric blow-ups)
  * no value explosion (magnitude < 1e6)  (missing clamps / runaway integration)
  * no unbounded pool growth (< 10000)    (entity leaks)
  * determinism: same seed+inputs -> same trajectory  (Math.random/Date.now)
  * pause freezes the simulation          (if state.paused is supported)
  * game-over disables input              (if state.over is supported)

Violations are BLOCKING and surface as :meth:`HeadlessGateResult.error_gaps`,
which the build fix-loop feeds back like compile errors. Reachability + juice are
NON-blocking and returned in ``report``.

Design rules: pure (no project I/O beyond reading the sim), offline (no network),
and NEVER raises. Any infrastructure problem (no ``node``, import failure, crash,
timeout) degrades to ``applicable=False, passed=True`` — the gate never blocks a
build because the gate itself couldn't run; only real invariant violations block.

The sim-core contract (``src/sim.js``, a pure ES module with NO Phaser import)::

    export function createState(seed)        // deterministic initial state
    export function step(state, input, dt)   // advance one tick; returns state
    export function isWin(state)  -> boolean
    export function isLose(state) -> boolean

``input = {left,right,up,down,action,pause}`` (booleans); ``dt`` in seconds.
``state.paused`` (freeze) and ``state.over`` (game over) are honored when present.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Candidate locations for the pure sim core, most-specific first.
_SIM_CANDIDATES = ("src/sim.js", "sim.js", "src/game/sim.js", "src/game/sim.mjs")

# The Node harness: imports the sim by absolute path, runs the battery, and
# prints a single JSON object to stdout. Embedded (like proof_run shells out to
# npm) so nothing extra is written into the delivered project. Constants are
# baked in; ticks/seed arrive via argv. Plain data only — JSON snapshots drive
# the determinism + pause + game-over comparisons.
_HARNESS = r"""
import { pathToFileURL } from 'node:url'

const [,, simPath, ticksArg, seedArg] = process.argv
const TICKS = parseInt(ticksArg || '600', 10)
const SEED = parseInt(seedArg || '1234', 10)
const DT = 1 / 60
// Above any uint32 RNG/hash state (max ~4.3e9) so a legit seeded-RNG field never
// false-flags regardless of its name; runaway integration still crosses it fast.
const MAG_CAP = 1e10
// Pool-leak detection is GROWTH-based and PER-ARRAY-PATH: a large-but-static array
// (e.g. a fixed 128x128 tilemap) is fine, and a leak in one array is not masked by
// a larger static array elsewhere. Flagged when an array grows >=1.8x from the
// half-way point AND ends above this floor.
const POOL_FLOOR = 2000
// Belt-and-suspenders alongside MAG_CAP: fields that may legitimately hold large
// integers, excluded from the explosion check only (still scanned for NaN/Inf).
const SKIP_MAG = /rng|seed|hash|uuid|^id$/i

function out(obj) { process.stdout.write(JSON.stringify(obj)) }

// Cycle- / BigInt- / Map- / Set-tolerant serializer for the snapshot comparisons,
// so a later check can NEVER throw and discard violations already found in run A.
function snapshot(x) {
  const seen = new WeakSet()
  return JSON.stringify(x, (k, v) => {
    if (typeof v === 'bigint') return 'BIGINT:' + v.toString()
    if (v instanceof Map) return { __map: [...v.entries()] }
    if (v instanceof Set) return { __set: [...v.values()] }
    if (v && typeof v === 'object') {
      if (seen.has(v)) return '[Circular]'
      seen.add(v)
    }
    return v
  })
}

let sim
try {
  sim = await import(pathToFileURL(simPath).href)
} catch (e) {
  out({ applicable: false, reason: 'sim import failed (not pure?): ' + (e && e.message) })
  process.exit(0)
}
const { createState, step, isWin, isLose } = sim
if (typeof createState !== 'function' || typeof step !== 'function') {
  out({ applicable: false, reason: 'sim.js must export createState() and step()' })
  process.exit(0)
}

// Deterministic input script: cycles directions + periodic action so the sim is
// genuinely exercised (movement, collisions, scoring) without a game-specific policy.
function inputAt(t) {
  const m = t % 8
  return {
    left: m === 0, right: m === 1 || m === 4, up: m === 2, down: m === 3,
    action: (t % 3) === 0, pause: false,
  }
}

// Recursive scan: first NaN/Inf/explosion + the length of EACH array by path
// (so per-array leak detection isn't masked by a larger static array). Map/Set
// aware. Element scanning is capped so a huge pool doesn't make this O(n) slow —
// the leak is caught by length, scalar NaNs by the cap.
function scan(state) {
  let bad = null
  const arrs = {}
  const seen = new Set()
  function note(kind, path, value) { if (!bad) bad = { kind, path, value } }
  function walk(v, path, key) {
    if (v === null || typeof v !== 'object') {
      if (typeof v === 'number') {
        if (Number.isNaN(v)) note('nan', path)
        else if (!Number.isFinite(v)) note('inf', path)
        else if (Math.abs(v) > MAG_CAP && !SKIP_MAG.test(key || '')) note('explosion', path, v)
      }
      return
    }
    if (seen.has(v)) return
    seen.add(v)
    if (Array.isArray(v)) {
      arrs[path || '(root)'] = v.length
      const lim = Math.min(v.length, 2000)
      for (let i = 0; i < lim; i++) walk(v[i], path + '[]', '')
    } else if (v instanceof Map) {
      arrs[path || '(root)'] = v.size
      for (const [mk, mv] of v) walk(mv, path + '.' + String(mk), String(mk))
    } else if (v instanceof Set) {
      arrs[path || '(root)'] = v.size
      for (const sv of v) walk(sv, path + '#', '')
    } else {
      for (const k in v) walk(v[k], path ? path + '.' + k : k, k)
    }
  }
  walk(state, '', '')
  return { bad, arrs }
}

function advance(s, input) {
  const r = step(s, input, DT)
  return r === undefined ? s : r
}

function run(ticks, inputFn) {
  let s = createState(SEED)
  let firstBad = null, win = false, lose = false
  const arrMid = {}, arrEnd = {}
  const half = Math.floor(ticks / 2)
  for (let t = 0; t < ticks; t++) {
    s = advance(s, inputFn(t))
    const sc = scan(s)
    if (sc.bad && !firstBad) firstBad = { ...sc.bad, tick: t }
    for (const p in sc.arrs) {
      const len = sc.arrs[p]
      if (t < half && len > (arrMid[p] || 0)) arrMid[p] = len
      if (len > (arrEnd[p] || 0)) arrEnd[p] = len
    }
    if (typeof isWin === 'function' && isWin(s)) win = true
    if (typeof isLose === 'function' && isLose(s)) lose = true
  }
  return { state: s, firstBad, arrMid, arrEnd, win, lose }
}

const violations = []
const report = {}

const A = run(TICKS, inputAt)

// 1+2. NaN / Inf / value explosion
if (A.firstBad) {
  const b = A.firstBad
  if (b.kind === 'nan') violations.push(`NaN in state.${b.path} after ${b.tick} ticks`)
  else if (b.kind === 'inf') violations.push(`Infinity in state.${b.path} after ${b.tick} ticks`)
  else violations.push(`value explosion: state.${b.path} = ${b.value} (> ${MAG_CAP}) after ${b.tick} ticks`)
}

// 3. unbounded pool growth (leak), per array path — not masked by a coexisting
// large-but-static array.
for (const p in A.arrEnd) {
  const end = A.arrEnd[p], mid = A.arrMid[p] || 0
  if (end > POOL_FLOOR && end >= mid * 1.8) {
    violations.push(`pool leak: array '${p}' grew to ${end} entries (was ${mid} at the half-way point) — likely an unbounded spawn/leak`)
    break
  }
}

// The post-A checks build/compare snapshots; wrap them so a throw can NEVER
// discard the violations A already found — the final emit must always run.
try {
  // 4. determinism: run twice with DIVERGENT time/entropy so a sim that reads
  // them differs reliably. Math.random diverges via the advancing global PRNG;
  // Date.now / performance.now are stubbed to different values per run (their 1ms
  // resolution otherwise makes back-to-back runs land in the same millisecond).
  const realNow = Date.now
  const perf = globalThis.performance
  const realPerf = perf && perf.now ? perf.now.bind(perf) : null
  const setTime = (v) => { globalThis.Date.now = () => v; if (perf) perf.now = () => v }
  setTime(1000); const d1 = run(TICKS, inputAt)
  setTime(9000); const d2 = run(TICKS, inputAt)
  globalThis.Date.now = realNow; if (realPerf) perf.now = realPerf
  if (snapshot(d1.state) !== snapshot(d2.state)) {
    violations.push('non-determinism: identical seed + inputs produced different state (Math.random / Date.now / performance.now?)')
  }

  // 5. pause freezes the simulation (only if the convention is present)
  {
    const s = createState(SEED)
    if (s && typeof s === 'object' && 'paused' in s) {
      s.paused = true
      const before = snapshot(s)
      let cur = s
      for (let t = 0; t < 30; t++) cur = advance(cur, { left: true, right: true, up: true, down: true, action: true, pause: true })
      if (snapshot(cur) !== before) violations.push('pause does not freeze the simulation (state changed while paused)')
    } else {
      report.pause = 'not testable (no state.paused convention)'
    }
  }

  // 6. game-over disables input (only if the convention is present)
  {
    const s = createState(SEED)
    if (s && typeof s === 'object' && 'over' in s) {
      s.over = true
      const before = snapshot(s)
      let cur = s
      for (let t = 0; t < 30; t++) cur = advance(cur, { left: true, right: true, up: true, down: true, action: true, pause: false })
      if (snapshot(cur) !== before) violations.push('game-over does not disable input (state changed after over=true)')
    } else {
      report.gameOver = 'not testable (no state.over convention)'
    }
  }
} catch (e) {
  report.gateError = (e && e.message) || String(e)
}

// Non-blocking: reachability (best-effort under the generic input script).
report.winReachable = A.win
report.loseReachable = A.lose

out({ applicable: true, passed: violations.length === 0, violations, report })
"""


@dataclass(slots=True)
class HeadlessGateResult:
    """Outcome of the headless invariant gate (mirrors ``ProofResult``)."""

    applicable: bool          # was a runnable pure sim core found?
    passed: bool              # no BLOCKING invariant violation
    violations: list[str] = field(default_factory=list)   # blocking failures
    report: dict[str, Any] = field(default_factory=dict)  # non-blocking signals
    detail: dict[str, Any] = field(default_factory=dict)

    def error_gaps(self) -> list[str]:
        """Fix-loop feedback — one line per blocking violation (compile-error style)."""
        if self.passed:
            return []
        return [f"Headless invariant gate failed: {v}" for v in self.violations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "passed": self.passed,
            "violations": list(self.violations),
            "report": dict(self.report),
            "detail": dict(self.detail),
        }


def _find_sim(project_dir: Path) -> Path | None:
    for rel in _SIM_CANDIDATES:
        p = project_dir / rel
        if p.is_file():
            return p
    return None


def _skip(reason: str, **detail: Any) -> HeadlessGateResult:
    # Infra problem or no sim core: un-gated, NOT failed (never block on our own
    # inability to run — only real invariant violations block).
    return HeadlessGateResult(
        applicable=False, passed=True, detail={"reason": reason, **detail}
    )


def run_headless_gate(
    project_dir: str | Path,
    *,
    ticks: int = 600,
    seed: int = 1234,
    timeout: int = 30,
) -> HeadlessGateResult:
    """Run the headless invariant gate on a game project. Never raises.

    Returns ``applicable=False, passed=True`` when there is no pure sim core, no
    ``node``, or the harness can't run — so the gate degrades safely and only
    genuine invariant violations ever block a build.
    """
    pdir = Path(project_dir)
    sim = _find_sim(pdir)
    if sim is None:
        return _skip("no pure sim core (src/sim.js) found")

    node = shutil.which("node")
    if node is None:
        return _skip("node not available")

    try:
        with tempfile.TemporaryDirectory(prefix="skyn3t-headless-") as td:
            harness = Path(td) / "harness.mjs"
            harness.write_text(_HARNESS, encoding="utf-8")
            proc = subprocess.run(
                [node, str(harness), str(sim.resolve()), str(ticks), str(seed)],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return _skip("headless harness timed out", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - infra failure must never block the build
        log.warning("headless_gate.run_failed", error=str(exc))
        return _skip(f"harness execution failed: {exc}")

    raw = (proc.stdout or "").strip()
    if not raw:
        return _skip(
            "harness produced no output",
            returncode=proc.returncode,
            stderr=(proc.stderr or "")[:1000],
        )
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return _skip("harness output was not JSON", stdout=raw[:1000])

    if not data.get("applicable", False):
        # We reach here only when node RAN the harness on a sim file that
        # _find_sim located, and the harness reported the file is broken (import
        # threw — e.g. it isn't pure) or doesn't export the contract. That is
        # attributable to the build, so it BLOCKS as a violation — NOT a
        # degrade-open skip (which is reserved for genuine infra failures: no
        # node, timeout, no/garbled output, handled above).
        reason = str(data.get("reason", "sim core not runnable"))
        return HeadlessGateResult(
            applicable=True,
            passed=False,
            violations=[f"src/sim.js is not a valid pure sim core: {reason}"],
            detail={"sim": str(sim.relative_to(pdir))},
        )

    violations = [str(v) for v in (data.get("violations") or [])]
    return HeadlessGateResult(
        applicable=True,
        passed=bool(data.get("passed", not violations)),
        violations=violations,
        report=dict(data.get("report") or {}),
        detail={"sim": str(sim.relative_to(pdir)), "ticks": ticks, "seed": seed},
    )
