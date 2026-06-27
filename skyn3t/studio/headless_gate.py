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
and NEVER raises. GENUINE infra problems (no ``node``, couldn't launch the
harness) degrade to ``applicable=False, passed=True`` — the gate never blocks a
build because *it* couldn't run. But once a sim FILE is located, any failure to
verify it cleanly — a runtime throw, a hang/timeout, a crash, or a corrupted
result — is attributable to the BUILD and BLOCKS (``applicable=True,
passed=False``). The harness writes its verdict to a PRIVATE file, so the sim's
own ``console.log`` can never corrupt the result and silently discard violations.

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

# Candidate locations for the pure sim core, most-specific first. Games that split
# their logic into a src/sim/ folder are just as valid as a flat src/sim.js —
# missing these made the gate report the MISLEADING "no pure sim core" (a false
# negative) instead of verifying a sim that was really there. A verifier's error
# reason is part of its contract, so the canonical flat paths stay FIRST (they win
# when both exist) and the nested src/sim/ forms follow, ahead of the src/game/*
# fallbacks. Both .js and .mjs are covered for each nested name.
_SIM_CANDIDATES = (
    "src/sim.js", "sim.js",
    "src/sim/sim.js", "src/sim/sim.mjs", "src/sim/index.js", "src/sim/index.mjs",
    "src/game/sim.js", "src/game/sim.mjs", "src/game/sim/sim.js",
)

# The Node harness: imports the sim by absolute path, runs the battery, and
# prints a single JSON object to stdout. Embedded (like proof_run shells out to
# npm) so nothing extra is written into the delivered project. Constants are
# baked in; ticks/seed arrive via argv. Plain data only — JSON snapshots drive
# the determinism + pause + game-over comparisons.
_HARNESS = r"""
import { pathToFileURL } from 'node:url'
import { writeFileSync } from 'node:fs'

const [,, simPath, ticksArg, seedArg, resultPath] = process.argv
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

// Write the verdict to a PRIVATE file (not stdout): a sim that does its own
// console.log must NOT be able to corrupt the result JSON and make the gate
// degrade open, silently discarding real invariant violations.
function out(obj) { writeFileSync(resultPath, JSON.stringify(obj)) }

// Cycle- / BigInt- / Map- / Set-tolerant serializer for the snapshot comparisons,
// so a later check can NEVER throw and discard violations already found in run A.
function snapshot(x) {
  const seen = new WeakSet()
  return JSON.stringify(x, (k, v) => {
    if (typeof v === 'bigint') return 'BIGINT:' + v.toString()
    if (v && typeof v === 'object') {
      // Register EVERY object (Map/Set included) in the cycle guard BEFORE
      // expanding it, so a reference cycle THROUGH a Map/Set value short-circuits
      // to '[Circular]' instead of recursing forever (stack overflow).
      if (seen.has(v)) return '[Circular]'
      seen.add(v)
      if (v instanceof Map) return { __map: [...v.entries()] }
      if (v instanceof Set) return { __set: [...v.values()] }
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
  const arrs = Object.create(null)  // null proto: 'constructor'/'toString' keys aren't inherited-present
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
      // max(): sibling containers share the '[]' suffix, so last-write-wins would
      // hide a leak in a non-last sibling — keep the LARGEST length seen per path.
      const key = path || '(root)'
      arrs[key] = Math.max(arrs[key] || 0, v.length)
      const lim = Math.min(v.length, 2000)
      for (let i = 0; i < lim; i++) walk(v[i], path + '[]', '')
    } else if (v instanceof Map) {
      const key = path || '(root)'
      arrs[key] = Math.max(arrs[key] || 0, v.size)
      for (const [mk, mv] of v) walk(mv, path + '.' + String(mk), String(mk))
    } else if (v instanceof Set) {
      const key = path || '(root)'
      arrs[key] = Math.max(arrs[key] || 0, v.size)
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
  // null-proto maps so `p in arrMid` only sees real keys (a state field literally
  // named 'constructor'/'toString' would otherwise read as inherited-present).
  const arrFirst = Object.create(null), arrMid = Object.create(null), arrEnd = Object.create(null)
  const half = Math.floor(ticks / 2)
  for (let t = 0; t < ticks; t++) {
    s = advance(s, inputFn(t))
    const sc = scan(s)
    if (sc.bad && !firstBad) firstBad = { ...sc.bad, tick: t }
    for (const p in sc.arrs) {
      const len = sc.arrs[p]
      // Baseline = first NON-ZERO size: an array that's empty early then bulk-loads
      // once to a bounded size must not read as growth-from-0 (a blocking FP).
      if (len > 0 && !(p in arrFirst)) arrFirst[p] = len
      if (t < half && len > (arrMid[p] || 0)) arrMid[p] = len
      if (len > (arrEnd[p] || 0)) arrEnd[p] = len
    }
    if (typeof isWin === 'function' && isWin(s)) win = true
    if (typeof isLose === 'function' && isLose(s)) lose = true
  }
  return { state: s, firstBad, arrFirst, arrMid, arrEnd, win, lose }
}

const violations = []
const report = {}

// Run A + the NaN/leak scan are wrapped: a sim that THROWS at runtime (a real
// defect — null deref, bad index) must become a BLOCKING violation with a useful
// message, NOT crash the harness into empty output that the caller would have to
// treat as "couldn't run". (The post-A battery has its own guard below.)
let A
try {
  A = run(TICKS, inputAt)

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
    const end = A.arrEnd[p]
    // Baseline = the array's size BEFORE any leak: its max in the first half if it
    // existed then, otherwise its length when it first appeared. Using the
    // first-sighting size (not 0) means a BOUNDED array that spawns AFTER the
    // midpoint is not false-flagged as a leak (only genuine growth trips it).
    const baseline = (p in A.arrMid) ? A.arrMid[p] : (A.arrFirst[p] || 0)
    if (end > POOL_FLOOR && end >= baseline * 1.8) {
      violations.push(`pool leak: array '${p}' grew to ${end} entries (was ${baseline} earlier) — likely an unbounded spawn/leak`)
      break
    }
  }
} catch (e) {
  out({ applicable: true, passed: false, violations: ['sim threw at runtime: ' + ((e && e.message) || String(e))] })
  process.exit(0)
}

// The post-A checks build/compare snapshots; wrap them so a throw can NEVER
// discard the violations A already found — the final emit must always run.
try {
  // 4. determinism: run twice with DIVERGENT time/entropy so a sim that reads
  // them differs reliably. Math.random diverges via the advancing global PRNG;
  // Date.now / new Date() / performance.now are stubbed to different values per
  // run (their 1ms resolution otherwise makes back-to-back runs land in the same
  // millisecond). The Date CONSTRUCTOR is stubbed too — `new Date().getTime()` is
  // a common wall-clock read that a Date.now-only stub would miss.
  const RealDate = globalThis.Date
  const perf = globalThis.performance
  const realPerf = perf && perf.now ? perf.now.bind(perf) : null
  // A Proxy (not a subclass) so the stub is callable BOTH as a constructor
  // (`new Date()` -> pinned epoch v) AND as a plain function (`Date()` -> a date
  // STRING, like the real thing) — an ES6 class throws when called without `new`,
  // and that throw would be swallowed by the determinism try and silently disable
  // the whole battery. Date.now / new Date()/Date(arg…) / parse / UTC all preserved.
  const setTime = (v) => {
    globalThis.Date = new Proxy(RealDate, {
      apply() { return new RealDate(v).toString() },
      construct(target, args) { return args.length === 0 ? new RealDate(v) : new RealDate(...args) },
      get(target, prop, receiver) {
        if (prop === 'now') return () => v
        return Reflect.get(target, prop, receiver)
      },
    })
    if (perf) perf.now = () => v
  }
  setTime(1000); const d1 = run(TICKS, inputAt)
  setTime(9000); const d2 = run(TICKS, inputAt)
  globalThis.Date = RealDate; if (realPerf) perf.now = realPerf
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

    # A located sim FILE that can't be cleanly verified is the BUILD's defect, not
    # infra — it BLOCKS rather than degrading open (the whole point of the gate).
    def _blocked(message: str, **detail: Any) -> HeadlessGateResult:
        return HeadlessGateResult(
            applicable=True,
            passed=False,
            violations=[message],
            detail={"sim": str(sim.relative_to(pdir)), **detail},
        )

    try:
        with tempfile.TemporaryDirectory(prefix="skyn3t-headless-") as td:
            harness = Path(td) / "harness.mjs"
            harness.write_text(_HARNESS, encoding="utf-8")
            result_file = Path(td) / "result.json"
            try:
                proc = subprocess.run(
                    [node, str(harness), str(sim.resolve()), str(ticks),
                     str(seed), str(result_file)],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                # The located sim hung past the timeout — a pure N-tick sim cannot
                # legitimately do that, so it is the build's defect (likely an
                # infinite loop), not an infra problem.
                return _blocked(
                    f"sim hung at runtime (> {timeout}s) — runtime invariants could "
                    "not be verified (likely an infinite loop)",
                    timeout=timeout,
                )
            # Read the verdict from the PRIVATE result file (immune to any stdout
            # the sim itself produced). Read inside the with so the temp dir is
            # still present.
            raw = (
                result_file.read_text(encoding="utf-8").strip()
                if result_file.is_file() else ""
            )
    except Exception as exc:  # noqa: BLE001 - couldn't even launch node: genuine infra
        log.warning("headless_gate.run_failed", error=str(exc))
        return _skip(f"harness execution failed: {exc}")

    if not raw:
        # node ran on a located sim but emitted no verdict — it crashed before
        # writing (OOM, stack overflow, hard process exit). Attributable to the build.
        return _blocked(
            "sim crashed at runtime (no result emitted) — runtime invariants could "
            "not be verified",
            returncode=proc.returncode,
            stderr=(proc.stderr or "")[:1000],
        )
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return _blocked("headless harness result was not JSON", stdout=raw[:1000])

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
