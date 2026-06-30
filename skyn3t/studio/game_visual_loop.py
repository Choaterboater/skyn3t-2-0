"""Game visual REPAIR loop (roadmap #9, the "feeds the fix-loop" rung).

The advisory game visual check (``game_visual_check.py``) tells us a delivered game
looks EMPTY or has TINY entities; this loop makes that signal ACT — it feeds the gap to
the improver, rebuilds/re-serves, and re-judges. Because the verdict is a FUZZY vision
call, the loop is do-no-harm: a repair is kept ONLY if it (a) preserves headless-gate
correctness, (b) actually improves the visual verdict, and (c) still builds — otherwise
the tree is rolled back. It never blocks the build verdict.

Every collaborator (judge / improver / gate / build) is injected, so the control flow
and the rollback guards are testable without a browser, vision model, node, or build.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.studio.game_visual_check import GameVisualVerdict

# Game-source files the improver is steered at, in priority order. The sim core spawns
# entities (fixes "empty"); the renderer/main set the display size (fixes "tiny").
_SIM_CORES = (
    "src/sim.js", "src/sim/sim.js", "src/sim/index.js",
    "sim.js", "src/game/sim.js", "src/game/sim.mjs",
)
_RENDER_FILES = ("src/renderer.js", "src/render.js", "src/main.js")


# ── pure verdict comparisons ────────────────────────────────────────────────

def visual_regressed(before: GameVisualVerdict, after: GameVisualVerdict) -> bool:
    """True iff ``after`` introduced a NEW failure not present in ``before``. A skipped
    ``after`` (could-not-judge) is unknown, not a regression."""
    if after.skipped:
        return False
    if before.populated and not after.populated:
        return True
    if before.entities_readable_size and not after.entities_readable_size:
        return True
    # New concrete visual defects the model flagged: a non-clean ``after`` that lists
    # strictly MORE issues than ``before`` introduced a problem the repair didn't have
    # to (asymmetric with visual_improved, which already required after.ok for its
    # issues-only branch). Free-text issues are noisy, so we only fire on a strict
    # increase and never when the frame came back clean.
    if (not after.ok) and len(after.issues) > len(before.issues):
        return True
    return False


def visual_improved(before: GameVisualVerdict, after: GameVisualVerdict) -> bool:
    """True iff ``after`` fixed at least one failure present in ``before`` without
    introducing a new one. Skipped/regressed/already-clean ``before`` => False."""
    if after.skipped:
        return False
    if visual_regressed(before, after):
        return False
    if before.ok:
        return False
    return (
        (not before.populated and after.populated)
        or (not before.entities_readable_size and after.entities_readable_size)
        or ((before.gap() is not None) and after.ok)
    )


# ── game source-file selection (what the improver is steered at) ─────────────

def select_game_source_files(project_dir: str | Path) -> list[str]:
    """The EXISTING game-source files (relative POSIX paths) to hand the improver,
    deduped, capped at 4, in priority order. Never raises."""
    out: list[str] = []
    try:
        root = Path(project_dir)
        first_core = next((c for c in _SIM_CORES if (root / c).is_file()), None)
        if first_core:
            out.append(first_core)
        for rel in _RENDER_FILES:
            if (root / rel).is_file():
                out.append(rel)
    except Exception:  # noqa: BLE001 - selection must never raise
        return []
    # dedupe (preserve order) + cap at 4
    seen: set[str] = set()
    deduped: list[str] = []
    for rel in out:
        if rel not in seen:
            seen.add(rel)
            deduped.append(rel)
    return deduped[:4]


# ── result dataclasses ───────────────────────────────────────────────────────

@dataclass(slots=True)
class GameVisualRepairRound:
    index: int
    accepted: bool
    gate_ok: bool
    improved: bool
    regressed: bool
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "accepted": self.accepted,
            "gate_ok": self.gate_ok,
            "improved": self.improved,
            "regressed": self.regressed,
            "issues": list(self.issues),
        }


def _verdict_dict(v: GameVisualVerdict) -> dict[str, Any]:
    return {
        "ok": v.ok,
        "skipped": v.skipped,
        "populated": v.populated,
        "entities_readable_size": v.entities_readable_size,
        "issues": list(v.issues),
        "gap": v.gap() or "",
    }


@dataclass(slots=True)
class GameVisualRepairResult:
    initial: GameVisualVerdict
    final: GameVisualVerdict
    repaired: bool = False
    skipped: bool = False
    build_reverted: bool = False
    rounds: list[GameVisualRepairRound] = field(default_factory=list)
    gate: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial": _verdict_dict(self.initial),
            "final": _verdict_dict(self.final),
            "repaired": self.repaired,
            "skipped": self.skipped,
            "build_reverted": self.build_reverted,
            "rounds": [r.to_dict() for r in self.rounds],
        }


# ── filesystem snapshot/restore (all ops guarded) ────────────────────────────

# Generated/heavy dirs the improver should never need to roll back — excluding them
# keeps the whole-tree snapshot cheap and avoids fighting the package manager.
_SNAPSHOT_IGNORE_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", ".vite", ".next", "out",
    ".cache", "__pycache__", ".turbo", "coverage", ".parcel-cache",
})


def _iter_tree_files(root: Path):
    """Yield (relposix, Path) for every snapshot-eligible file under ``root``."""
    for p in root.rglob("*"):
        try:
            rel = p.relative_to(root)
            if any(part in _SNAPSHOT_IGNORE_DIRS for part in rel.parts):
                continue
            if p.is_file() and not p.is_symlink():
                yield rel.as_posix(), p
        except Exception:  # noqa: BLE001
            continue


def _snapshot_tree(project_dir: Path) -> dict[str, bytes]:
    """Capture the WHOLE project tree (bytes, so binary assets round-trip) so a
    rejected/failed improver run can be fully undone — the improver is only *steered*
    at a few files but may create/edit ANY file. Never raises."""
    snap: dict[str, bytes] = {}
    try:
        for rel, p in _iter_tree_files(project_dir):
            try:
                snap[rel] = p.read_bytes()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return snap


def _restore_tree(project_dir: Path, snap: dict[str, bytes]) -> None:
    """Restore the tree to a snapshot: rewrite every captured file AND delete any
    file the improver created that isn't in the snapshot. Never raises."""
    root = Path(project_dir)
    # 1. delete improver-created files (present now, absent in the snapshot)
    try:
        for rel, p in list(_iter_tree_files(root)):
            if rel not in snap:
                try:
                    p.unlink()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    # 2. restore/recreate every captured file
    for rel, data in snap.items():
        try:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        except Exception:  # noqa: BLE001
            pass


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# ── the repair loop ──────────────────────────────────────────────────────────

async def repair_game_visual(
    project_dir: str | Path,
    *,
    settings: Any,
    brief: str = "",
    stack: str = "phaser",
    plan_dict: dict | None = None,
    extra: Any = None,
    gate: Any = None,
    judge: Any = None,
    run_improver: Any = None,
    run_gate: Any = None,
    build_check: Any = None,
    attempts: int = 2,
) -> GameVisualRepairResult:
    """Serve -> vision-judge -> (if empty/tiny) feed the gap to the improver -> re-verify
    correctness + re-judge, keeping a repair ONLY when it preserves the headless gate,
    improves the visual verdict, and still builds; else roll back. Never raises."""
    root = Path(project_dir)

    if judge is None:
        async def judge(p):  # type: ignore[misc]
            from skyn3t.studio.game_visual_check import check_game_visual
            return await check_game_visual(p, settings=settings)

    if run_gate is None:
        def run_gate(p):  # type: ignore[misc]
            from skyn3t.studio.headless_gate import run_headless_gate
            return run_headless_gate(p)

    async def _judge() -> GameVisualVerdict:
        try:
            return await judge(root)
        except Exception as exc:  # noqa: BLE001
            return GameVisualVerdict(skipped=True, reason=f"judge error: {exc}")

    initial = await _judge()
    result = GameVisualRepairResult(initial=initial, final=initial, gate=gate)

    # 2. could-not-judge: unknown, do nothing.
    if initial.skipped:
        result.skipped = True
        return result
    # 3. already good: nothing to repair.
    if initial.gap() is None:
        return result
    # 4. no improver capability: record-only (today's behavior).
    if run_improver is None:
        return result
    # 5. no game source to steer at: record-only.
    files = select_game_source_files(root)
    if not files:
        return result

    # 6. baseline for the build backstop (discard ALL repairs if the build breaks).
    #    Whole-tree, because the improver may create/edit files beyond ``files``.
    baseline = _snapshot_tree(root)
    current = initial
    # If the game ALREADY had a verifiable sim core, a repair must KEEP it verifiable
    # (losing applicability is a correctness regression, not "gate preserved").
    initial_gate_applicable = bool(getattr(gate, "applicable", False)) if gate is not None else False

    for i in range(max(1, int(attempts))):
        gap = current.gap()
        if gap is None:
            break
        before = _snapshot_tree(root)
        try:
            ran = await run_improver([gap], files)
        except Exception:  # noqa: BLE001 - an improver failure ends the loop
            ran = False
        if not ran:
            # The improver may have written partial, unvalidated edits before it
            # failed/declined — roll the whole tree back so nothing un-checked ships.
            _restore_tree(root, before)
            break

        try:
            new_gate = await _maybe_await(run_gate(root))
        except Exception:  # noqa: BLE001
            new_gate = None
        # Fail CLOSED on the correctness check: a None/errored gate means we could not
        # verify invariants, so the repair must not be kept. And if the game started
        # verifiable, the repair must stay verifiable (applicable) AND pass; only a
        # game that was never verifiable may keep an inapplicable (no-sim-core) gate.
        if new_gate is None:
            gate_ok = False
        elif initial_gate_applicable:
            gate_ok = bool(getattr(new_gate, "applicable", False)) and bool(
                getattr(new_gate, "passed", False))
        else:
            gate_ok = (not getattr(new_gate, "applicable", False)) or bool(
                getattr(new_gate, "passed", False))

        after = await _judge()
        regressed = visual_regressed(current, after)
        improved = visual_improved(current, after)
        accept = gate_ok and improved and not regressed
        result.rounds.append(GameVisualRepairRound(
            index=i, accepted=accept, gate_ok=gate_ok,
            improved=improved, regressed=regressed, issues=list(after.issues),
        ))
        if accept:
            current = after
            result.repaired = True
            result.final = after
            if new_gate is not None:
                result.gate = new_gate
        else:
            _restore_tree(root, before)
            result.final = current
            break

    # 8. build backstop: a kept repair that doesn't build is fully reverted to the
    #    pre-loop tree (which also deletes any file the improver created).
    if result.repaired and build_check is not None:
        try:
            builds = bool(await _maybe_await(build_check(root)))
        except Exception:  # noqa: BLE001
            builds = False
        if not builds:
            _restore_tree(root, baseline)
            result.repaired = False
            result.build_reverted = True
            result.final = initial
            result.gate = gate

    return result
