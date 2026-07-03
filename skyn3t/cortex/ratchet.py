"""The reliability ratchet — keep a change only if it MEASURABLY helps.

The flywheel's decision brain: given a proposed change (a tuning/prompt override),
run the bench BEFORE, apply the change, run the bench AFTER, and keep it only when
the measured delta improves — no aggregate go-rate drop AND no per-app-type
go-rate regression (a change that lifts the overall rate while breaking one stack
must not pass). Otherwise it is reverted. This is how autonomy earns its leash:
a proposal survives only on evidence, not confidence.

Fully injected — ``apply_change`` / ``revert_change`` / ``make_build_fn`` are
passed in, so the decision logic is testable without a real spine or Cortex. The
CLI supplies the real bench build_fn + persist/restore. Opt-in
(``reliability_ratchet_enabled``, default off) and never auto-applied to
production; it only decides and reverts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

# Cortex writes live overrides to these files under <data_dir>/cortex/. There is
# no revert primitive, so the ratchet snapshots their bytes and restores them.
_OVERRIDE_FILES = ("settings_overrides.json", "prompt_overrides.json")


def _safe_call(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - a revert must never raise
        pass


def snapshot_overrides(data_dir: str | Path) -> dict[str, str | None]:
    """Capture the exact bytes of the cortex override files so a rejected change
    can be restored precisely. ``None`` marks a file that did not exist."""
    base = Path(data_dir) / "cortex"
    snap: dict[str, str | None] = {}
    for name in _OVERRIDE_FILES:
        p = base / name
        try:
            snap[name] = p.read_text(encoding="utf-8") if p.exists() else None
        except OSError:
            snap[name] = None
    return snap


def restore_overrides(data_dir: str | Path, snapshot: dict[str, str | None]) -> None:
    """Restore the override files to a prior :func:`snapshot_overrides` — deleting
    files that did not exist then. Never raises."""
    base = Path(data_dir) / "cortex"
    from skyn3t.atomic_io import atomic_write_text

    for name, content in (snapshot or {}).items():
        p = base / name
        try:
            if content is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(p, content)
        except OSError:
            continue


def per_stack_go_rate_regressions(before: Any, after: Any) -> list[str]:
    """Stacks whose go-rate DROPPED between two runs — the per-app-type guard the
    aggregate :func:`gate_change` misses. A change that lifts the overall go-rate
    while breaking one app-type (e.g. swift builds) must not pass."""
    from skyn3t.studio.bench import summarize_by_stack

    b = summarize_by_stack(before.results)
    a = summarize_by_stack(after.results)
    out: list[str] = []
    for stack, bs in b.items():
        af = a.get(stack)
        if af is not None and af["go_rate"] < bs["go_rate"]:
            out.append(f"{stack} {bs['go_rate']}->{af['go_rate']}")
    return out


BuildFnFactory = Callable[[], Callable[[Any], Awaitable[Any]]]


async def evaluate_change(
    *,
    apply_change: Callable[[], Any],
    revert_change: Callable[[], Any],
    make_build_fn: BuildFnFactory,
    cases: list[Any] | None = None,
    label: str = "ratchet",
    require_per_stack: bool = True,
    gate_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run bench-before, apply the change, run bench-after, and KEEP the change
    only if the bench improves (no aggregate OR per-app-type go-rate regression);
    otherwise REVERT it. Returns a decision record.

    ``make_build_fn`` is a factory called once per bench run so the AFTER run sees
    the freshly-applied override (a build_fn holding stale Settings would not).
    The change is reverted on any error too — the ratchet never leaves a
    half-applied change behind.
    """
    from skyn3t.studio.bench import DEFAULT_CASES, diff_runs, gate_change, run_bench

    cases = list(cases) if cases is not None else list(DEFAULT_CASES)
    before = await run_bench(cases, make_build_fn(), label=f"{label}-before")

    try:
        apply_change()
        after = await run_bench(cases, make_build_fn(), label=f"{label}-after")
    except Exception as exc:  # noqa: BLE001 - a broken apply/build must not strand the change
        # Revert defensively: apply may have partially applied before raising, and
        # a failed after-bench leaves a fully-applied change — neither may persist.
        _safe_call(revert_change)
        return {"kept": False, "error": True, "reasons": [f"ratchet error: {exc}"]}

    delta = diff_runs(before, after)
    accept, reasons = gate_change(delta, **(gate_kwargs or {}))

    stack_regressions = (
        per_stack_go_rate_regressions(before, after) if require_per_stack else []
    )
    if stack_regressions:
        accept = False
        reasons = [*reasons, "per-app-type go-rate regressed: " + ", ".join(stack_regressions)]

    if not accept:
        _safe_call(revert_change)

    return {
        "kept": bool(accept),
        "reasons": reasons,
        "before": before.summary,
        "after": after.summary,
        "go_rate_delta": delta.get("go_rate_delta"),
        "stack_regressions": stack_regressions,
    }
