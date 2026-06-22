# skyn3t/studio/bench.py
"""Benchmark / regression harness (Spec 2).

The factory's objective backbone: run a fixed brief-set through the build
pipeline, record a scored ledger, diff two runs, and gate a change (a cortex
prompt-rewrite or skill promotion) on the MEASURED before/after delta instead of
vibes. The harness is build_fn-injected so its logic is testable without the
heavy agent spine; the CLI supplies a real build_fn that drives StudioRunner.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

# A small, stack-diverse default brief-set. Intentionally concrete so intent
# scoring + stack selection have real signal to measure.
# Stacks MUST be valid selector pin keys (studio.stack_selector._validate_pin):
# static/python/fastapi/react/express/react_native + collapses (flask->fastapi,
# cli->python). 'python_cli'/'react_vite' are NOT pin keys and silently drop.
DEFAULT_CASES_RAW = [
    ("coloring-site", "a website with printable coloring pages for kids", "static"),
    ("todo-cli", "a python command-line to-do list that saves tasks to a file", "python"),
    ("notes-api", "a FastAPI service to create and list short text notes", "fastapi"),
    ("weather-spa", "a React app that shows a 5-day weather forecast for a city", "react"),
    ("expense-tracker", "a Flask web app to track monthly expenses by category", "flask"),
]


@dataclass(slots=True)
class BenchCase:
    id: str
    brief: str
    stack: str = ""               # optional pin; "" lets the selector choose

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CASES = [BenchCase(id=i, brief=b, stack=s) for (i, b, s) in DEFAULT_CASES_RAW]


@dataclass(slots=True)
class BenchResult:
    case_id: str
    brief: str
    slug: str
    verdict: str
    score: float | None
    intent_score: float | None
    proof_passed: bool
    status: str
    stack: str
    cost_usd: float | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "go"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["passed"] = self.passed
        return d

    @classmethod
    def from_outcome(cls, case: "BenchCase", outcome: Any) -> "BenchResult":
        """Adapt a StudioRunner BuildOutcome (or any object exposing verdict/
        score/status/stack/manifest) into a BenchResult."""
        manifest = getattr(outcome, "manifest", None) or {}
        extra = (manifest.get("extra") if isinstance(manifest, dict) else {}) or {}
        intent = extra.get("intent") or {}
        proof = extra.get("proof") or {}
        return cls(
            case_id=case.id,
            brief=case.brief,
            slug=str(getattr(outcome, "slug", "") or ""),
            verdict=str(getattr(outcome, "verdict", "no_go") or "no_go"),
            score=_as_float(getattr(outcome, "score", None)),
            intent_score=_as_float(intent.get("score")),
            proof_passed=bool(proof.get("passed", False)),
            status=str(getattr(outcome, "status", "") or ""),
            stack=str(getattr(outcome, "stack", "") or case.stack),
            cost_usd=_as_float(getattr(outcome, "cost_usd", None)),
        )


@dataclass(slots=True)
class BenchRun:
    label: str
    results: list[BenchResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self) -> None:
        # Auto-summarize so a run is always self-describing, however constructed.
        if not self.summary and self.results:
            self.summary = summarize(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "created_at": self.created_at,
            "summary": self.summary,
            "results": [r.to_dict() for r in self.results],
        }


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def summarize(results: list[BenchResult]) -> dict[str, Any]:
    n = len(results)
    go = sum(1 for r in results if r.verdict == "go")
    n_error = sum(1 for r in results if r.status == "error")
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    total_cost = round(sum(costs), 6) if costs else 0.0
    return {
        "n": n,
        "go": go,
        "n_error": n_error,
        "go_rate": round(go / n, 4) if n else 0.0,
        "mean_score": _mean([r.score for r in results]),
        # go-only mean — the shippable-quality signal the gate keys on (a no_go's
        # score shouldn't let a change pass by "improving" failing builds).
        "mean_score_go": _mean([r.score for r in results if r.verdict == "go"]),
        "mean_intent": _mean([r.intent_score for r in results]),
        # efficiency: spend per SHIPPED build (None when nothing shipped — that's
        # undefined, not zero). mean_cost is None when there's no cost data at all
        # (consistent with cost_per_go — "no data" is not "zero spend").
        "total_cost_usd": total_cost,
        "mean_cost_usd": round(total_cost / len(costs), 6) if costs else None,
        "cost_per_go_usd": round(total_cost / go, 6) if go else None,
    }


BuildFn = Callable[[BenchCase], Awaitable[Any]]


async def run_bench(cases: list[BenchCase], build_fn: BuildFn, *,
                    label: str = "", created_at: float = 0.0) -> BenchRun:
    """Build each case via build_fn and collect scored results. A build that
    raises is recorded as a no_go/error result rather than aborting the run."""
    results: list[BenchResult] = []
    for case in cases:
        try:
            outcome = await build_fn(case)
            results.append(BenchResult.from_outcome(case, outcome))
        except Exception:  # noqa: BLE001 - one bad build must not kill the run
            results.append(BenchResult(
                case_id=case.id, brief=case.brief, slug="", verdict="no_go",
                score=None, intent_score=None, proof_passed=False,
                status="error", stack=case.stack))
    return BenchRun(label=label, results=results, summary=summarize(results),
                    created_at=created_at)


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def save_run(run: BenchRun, data_dir) -> Path:
    """Persist a run to <data_dir>/bench/run-<label>.json."""
    out_dir = Path(data_dir) / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (run.label or "run"))
    path = out_dir / f"run-{safe}.json"
    path.write_text(json.dumps(run.to_dict(), indent=2), encoding="utf-8")
    return path


def load_run(path) -> BenchRun:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    results = [
        BenchResult(
            case_id=r["case_id"], brief=r.get("brief", ""), slug=r.get("slug", ""),
            verdict=r.get("verdict", "no_go"), score=_as_float(r.get("score")),
            intent_score=_as_float(r.get("intent_score")),
            proof_passed=bool(r.get("proof_passed", False)),
            status=r.get("status", ""), stack=r.get("stack", ""),
            cost_usd=_as_float(r.get("cost_usd")))
        for r in data.get("results", [])
    ]
    return BenchRun(label=data.get("label", ""), results=results,
                    summary=data.get("summary", {}),
                    created_at=float(data.get("created_at", 0.0)))


# ---------------------------------------------------------------------------
# diff + gate
# ---------------------------------------------------------------------------

def diff_runs(before: BenchRun, after: BenchRun, *,
              score_regress_threshold: float = 5.0) -> dict[str, Any]:
    """Per-case + aggregate deltas between two runs, matched by case_id.

    A regression = a go->non-go flip (incl. a crash/error or a case DROPPED from
    the suite) OR a score drop beyond the threshold on a case that stayed go. An
    improvement = a non-go->go flip. Score deltas are computed only when both
    scores are present (a None/error score is "no data", never coerced to 0)."""
    before_by = {r.case_id: r for r in before.results}
    after_by = {r.case_id: r for r in after.results}
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    per_case: list[dict[str, Any]] = []

    for a in after.results:
        b = before_by.get(a.case_id)
        if b is None:
            per_case.append({"case_id": a.case_id, "verdict_before": "new",
                             "verdict_after": a.verdict, "score_delta": None})
            continue
        score_delta = (round(a.score - b.score, 2)
                       if (a.score is not None and b.score is not None) else None)
        entry = {"case_id": a.case_id, "verdict_before": b.verdict,
                 "verdict_after": a.verdict, "score_delta": score_delta}
        per_case.append(entry)
        if b.verdict == "go" and a.verdict != "go":
            regressions.append({**entry, "kind": "verdict"})
        elif b.verdict != "go" and a.verdict == "go":
            improvements.append({**entry, "kind": "verdict"})
        elif (b.verdict == "go" and a.verdict == "go" and score_delta is not None
              and score_delta <= -score_regress_threshold):
            regressions.append({**entry, "kind": "score"})

    # Cases present in the baseline but absent from the new run = coverage loss.
    for b in before.results:
        if b.case_id not in after_by:
            entry = {"case_id": b.case_id, "verdict_before": b.verdict,
                     "verdict_after": "dropped", "score_delta": None}
            per_case.append(entry)
            if b.verdict == "go":
                regressions.append({**entry, "kind": "dropped"})

    def _d(key: str, nd: int = 2) -> float:
        return round(after.summary.get(key, 0.0) - before.summary.get(key, 0.0), nd)

    def _cd(key: str) -> float | None:  # cost delta, tolerant of None ("undefined")
        av, bv = after.summary.get(key), before.summary.get(key)
        return round(av - bv, 6) if (av is not None and bv is not None) else None

    return {
        "n_before": before.summary.get("n", 0),
        "n_after": after.summary.get("n", 0),
        "mean_score_delta": _d("mean_score"),
        "mean_score_go_delta": _d("mean_score_go"),
        "mean_intent_delta": _d("mean_intent"),
        "go_rate_delta": _d("go_rate", 4),
        "total_cost_delta": _d("total_cost_usd", 6),
        "cost_per_go_delta": _cd("cost_per_go_usd"),
        "regressions": regressions,
        "improvements": improvements,
        "per_case": per_case,
    }


def gate_change(delta: dict[str, Any], *, min_mean_score_delta: float = 0.0,
                allow_verdict_regressions: bool = False,
                max_cost_per_go_increase: float | None = None) -> tuple[bool, list[str]]:
    """Whether a change should be ACCEPTED based on the measured delta. Rejects on
    any regression (verdict flip, crash, or dropped case), a drop in go-rate, an
    empty run on either side, a go-only mean-score delta below the bar, or (when
    a bar is set) a cost-per-go increase beyond it. Keys on the GO-ONLY mean so
    improving still-failing builds can't rubber-stamp a pass. Returns (accept,
    reasons-for-rejection)."""
    reasons: list[str] = []
    if not delta.get("n_before") or not delta.get("n_after"):
        reasons.append("a run has zero cases — nothing to compare")
    regs = delta.get("regressions", [])
    if regs and not allow_verdict_regressions:
        ids = ", ".join(r["case_id"] for r in regs)
        reasons.append(f"{len(regs)} regression(s): {ids}")
    if float(delta.get("go_rate_delta", 0.0)) < 0:
        reasons.append(f"go-rate dropped by {abs(delta['go_rate_delta'])}")
    msd = float(delta.get("mean_score_go_delta", delta.get("mean_score_delta", 0.0)))
    if msd < min_mean_score_delta:
        reasons.append(f"go-mean score delta {msd} < required {min_mean_score_delta}")
    cpgd = delta.get("cost_per_go_delta")
    if max_cost_per_go_increase is not None and cpgd is not None and cpgd > max_cost_per_go_increase:
        reasons.append(f"cost-per-go rose by {cpgd} > allowed {max_cost_per_go_increase}")
    return (not reasons), reasons
