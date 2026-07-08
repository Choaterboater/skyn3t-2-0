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
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# The all-stacks exam: one concrete case per REAL builder stack, so a per-app-type
# GO-rate (summarize_by_stack) can never go blind on a stack when this broader
# suite is requested. Briefs are concrete so intent scoring has real signal. A
# case's `stack` must be a valid selector pin (studio.stack_selector._validate_pin
# = the REAL_BUILDER_STACKS set, with the flask->fastapi / cli->python collapses);
# an invalid pin silently drops to selector choice.
ALL_STACK_CASES_RAW = [
    ("coloring-site", "a website with printable coloring pages for kids", "static"),
    ("todo-cli", "a python command-line to-do list that saves tasks to a file", "python"),
    ("notes-api", "a FastAPI service to create and list short text notes", "fastapi"),
    ("weather-spa", "a React app that shows a 5-day weather forecast for a city", "react"),
    ("expense-tracker", "a Flask web app to track monthly expenses by category", "flask"),
    ("dino-runner", "a phaser browser game where a dino jumps over cacti to score points", "phaser"),
    # Breadth coverage — the remaining registry stacks so no app-type is invisible.
    ("blog-ssr", "a Next.js blog with server-rendered post pages and a home index", "nextjs"),
    ("docs-astro", "an Astro static documentation site with a sidebar and code blocks", "astro"),
    ("shop-remix", "a Remix storefront with a product list page and a cart page", "remix"),
    ("ops-vue", "a Vue dashboard for tracking support tickets by status", "vue"),
    ("portal-sveltekit", "a SvelteKit customer portal with account settings and messages", "sveltekit"),
    ("inventory-react-ts", "a TypeScript React inventory tracker with filters and stock alerts", "react_ts"),
    ("shortener-express", "a Node Express URL shortener API with an in-memory store", "express"),
    ("habit-mobile", "an Expo mobile app: a habit tracker with daily streaks", "react_native"),
    ("timer-macos", "a native macOS SwiftUI menu-bar countdown timer app", "swift"),
    ("notes-desktop", "a Tauri desktop markdown notes app that saves to a local file", "tauri"),
    ("sqlite-mcp", "an MCP server exposing a tool to run read-only queries on a sqlite database", "mcp"),
    ("docs-rag", "a RAG app to ingest and chat with your uploaded text documents", "rag"),
    ("digest-workflow", "an agent workflow that fetches a URL, summarizes it, and posts the summary to a webhook", "workflow"),
    ("marketing-pack", "an agent persona pack for a startup marketing team with a catalog", "agent_pack"),
]


@dataclass(slots=True)
class BenchCase:
    id: str
    brief: str
    stack: str = ""               # optional pin; "" lets the selector choose

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ALL_STACK_CASES = [BenchCase(id=i, brief=b, stack=s) for (i, b, s) in ALL_STACK_CASES_RAW]
GAME_CASES = [c for c in ALL_STACK_CASES if c.stack == "phaser"]

# The day-to-day factory exam is app-first. Games remain buildable and benchmarkable
# through ALL_STACK_CASES/GAME_CASES, but they are no longer part of the default
# ratchet loop while the app factory is being stabilized.
DEFAULT_CASES = [c for c in ALL_STACK_CASES if c.stack != "phaser"]


def built_in_cases(suite: str = "apps") -> list[BenchCase]:
    """Return a built-in bench suite.

    ``apps`` is the default reliability loop, ``all`` restores one case per
    builder stack, and ``games`` isolates the Phaser game suite.
    """
    key = (suite or "apps").strip().lower().replace("_", "-")
    if key in {"all", "all-stacks", "full"}:
        return list(ALL_STACK_CASES)
    if key in {"game", "games", "phaser"}:
        return list(GAME_CASES)
    return list(DEFAULT_CASES)


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
    def from_outcome(cls, case: BenchCase, outcome: Any) -> BenchResult:
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
            # Per-app-type breakdown rides along with every serialized run so a
            # ledger/`--json` consumer (and the ratchet's per-stack regression
            # guard) can see a change that lifts the aggregate while silently
            # regressing one stack. Empty for an empty run.
            "by_stack": summarize_by_stack(self.results),
            "results": [r.to_dict() for r in self.results],
        }


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mean(values: list[float | None]) -> float:
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


def summarize_by_stack(results: list[BenchResult]) -> dict[str, dict[str, Any]]:
    """Per-app-type breakdown of a run: the same aggregate `summarize` metrics,
    grouped by stack. This is the signal that catches a change which lifts the
    overall go-rate while silently regressing one app-type (e.g. Phaser games).

    Results whose stack is empty are grouped under "unknown" so nothing is
    silently dropped. Stacks are ordered by descending case count for stable,
    scan-friendly reporting.
    """
    by_stack: dict[str, list[BenchResult]] = {}
    for r in results:
        by_stack.setdefault(r.stack or "unknown", []).append(r)
    ordered = sorted(by_stack.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {stack: summarize(group) for stack, group in ordered}


def scorecard(run: BenchRun) -> dict[str, Any]:
    """Human/product-facing scorecard for a measured factory bench run.

    ``summary`` is good for math; this scorecard is good for decisions: headline
    health, weakest stacks, and the exact failed/error cases to attack next.
    """
    results = list(run.results)
    summary = run.summary or summarize(results)
    by_stack = summarize_by_stack(results)
    proof_known = [r for r in results if r.status != "error"]
    proof_passed = sum(1 for r in proof_known if r.proof_passed)
    weak_stacks = []
    for stack, row in by_stack.items():
        if row.get("go_rate", 0.0) < 1.0 or row.get("n_error", 0):
            weak_stacks.append({
                "stack": stack,
                "go": row.get("go", 0),
                "n": row.get("n", 0),
                "go_rate": row.get("go_rate", 0.0),
                "n_error": row.get("n_error", 0),
                "mean_score": row.get("mean_score", 0.0),
            })
    weak_stacks.sort(key=lambda row: (
        -int(row.get("n_error", 0)),
        float(row.get("go_rate", 0.0)),
        float(row.get("mean_score", 0.0)),
        str(row.get("stack", "")),
    ))
    failures = [
        {
            "case_id": r.case_id,
            "stack": r.stack or "unknown",
            "status": r.status,
            "score": r.score,
        }
        for r in sorted(
            results,
            key=lambda item: (
                item.status != "error",
                item.stack or "unknown",
                item.case_id,
            ),
        )
        if r.verdict != "go" or r.status == "error"
    ]
    return {
        "label": run.label or "latest",
        "headline": {
            "cases": summary.get("n", 0),
            "go": summary.get("go", 0),
            "go_rate": summary.get("go_rate", 0.0),
            "errors": summary.get("n_error", 0),
            "mean_score": summary.get("mean_score", 0.0),
            "mean_score_go": summary.get("mean_score_go", 0.0),
            "proof_pass_rate": round(proof_passed / len(proof_known), 4)
            if proof_known else 0.0,
            "total_cost_usd": summary.get("total_cost_usd", 0.0),
            "cost_per_go_usd": summary.get("cost_per_go_usd"),
        },
        "weak_stacks": weak_stacks,
        "case_failures": failures,
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
    from skyn3t.atomic_io import atomic_write_text
    atomic_write_text(path, json.dumps(run.to_dict(), indent=2))
    return path


def publish_go_rate(run: BenchRun, out_dir) -> dict[str, Path]:
    """Publish a human-readable go-rate report plus machine-readable summary.

    This does not run the bench; it makes an already-measured ledger visible as
    the product claim: aggregate go-rate and per-stack rates from the same run.
    """
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    data = run.to_dict()
    summary = data.get("summary") or {}
    by_stack = data.get("by_stack") or {}
    card = scorecard(run)
    headline = card.get("headline") or {}
    label = run.label or "latest"
    md_lines = [
        f"# SkyN3t Go-Rate: {label}",
        "",
        f"- Cases: {summary.get('go', 0)}/{summary.get('n', 0)} go",
        f"- Go-Rate: {float(summary.get('go_rate', 0.0)) * 100:.1f}%",
        f"- Mean Score: {summary.get('mean_score', 0)}",
        f"- Errors: {headline.get('errors', 0)}",
        f"- Proof Pass-Rate: {float(headline.get('proof_pass_rate', 0.0)) * 100:.1f}%",
        f"- Cost / Go: {headline.get('cost_per_go_usd')}",
        "",
        "## Factory Scorecard",
        "",
        f"- Weak Stacks: {len(card.get('weak_stacks') or [])}",
        f"- Failed/Error Cases: {len(card.get('case_failures') or [])}",
        "",
        "| Stack | Go | Cases | Go-Rate | Mean Score |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for stack, row in by_stack.items():
        md_lines.append(
            f"| {stack} | {row.get('go', 0)} | {row.get('n', 0)} | "
            f"{float(row.get('go_rate', 0.0)) * 100:.1f}% | {row.get('mean_score', 0)} |"
        )
    weak = card.get("weak_stacks") or []
    if weak:
        md_lines.extend([
            "",
            "## Weak Stacks",
            "",
            "| Stack | Go | Cases | Errors | Go-Rate | Mean Score |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ])
        for row in weak:
            md_lines.append(
                f"| {row.get('stack', '')} | {row.get('go', 0)} | {row.get('n', 0)} | "
                f"{row.get('n_error', 0)} | "
                f"{float(row.get('go_rate', 0.0)) * 100:.1f}% | "
                f"{row.get('mean_score', 0)} |"
            )
    failures = card.get("case_failures") or []
    if failures:
        md_lines.extend([
            "",
            "## Failed/Error Cases",
            "",
            "| Case | Stack | Status | Score |",
            "| --- | --- | --- | ---: |",
        ])
        for row in failures:
            md_lines.append(
                f"| {row.get('case_id', '')} | {row.get('stack', '')} | "
                f"{row.get('status', '')} | {row.get('score')} |"
            )
    from skyn3t.atomic_io import atomic_write_text

    md_path = base / "go-rate.md"
    json_path = base / "go-rate.json"
    atomic_write_text(md_path, "\n".join(md_lines) + "\n")
    atomic_write_text(
        json_path,
        json.dumps(
            {
                "label": label,
                "created_at": run.created_at,
                "summary": summary,
                "by_stack": by_stack,
                "scorecard": card,
            },
            indent=2,
            sort_keys=True,
        ),
    )
    return {"markdown": md_path, "json": json_path}


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
# regression cases — the flywheel's memory: a failed REAL build becomes a
# permanent exam case, so any future change must keep it green (deduped, capped).
# ---------------------------------------------------------------------------

_REGRESSION_FILENAME = "regression_cases.json"


def _slug(text: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (text or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:80]


def regression_cases_path(data_dir) -> Path:
    return Path(data_dir) / "bench" / _REGRESSION_FILENAME


def load_regression_cases(data_dir) -> list[BenchCase]:
    """Cases captured from real failed builds. Never raises; missing/corrupt => []."""
    path = regression_cases_path(data_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[BenchCase] = []
    for d in data if isinstance(data, list) else []:
        cid = _slug(str((d or {}).get("id", "")))
        brief = str((d or {}).get("brief", "")).strip()
        if cid and brief:
            out.append(BenchCase(id=cid, brief=brief, stack=str((d or {}).get("stack", "")).strip()))
    return out


def capture_regression_case(data_dir, case_id: str, brief: str, stack: str = "",
                            *, cap: int = 200) -> bool:
    """Append a (failed real build) as a permanent regression case — deduped by id
    against BOTH the defaults and prior captures, capped to the most recent ``cap``.
    Returns True when a NEW case was added. Never raises."""
    try:
        cid = _slug(case_id)
        brief = (brief or "").strip()
        if not cid or not brief:
            return False
        existing = load_regression_cases(data_dir)
        seen = {c.id for c in existing} | {c.id for c in DEFAULT_CASES}
        if cid in seen:
            return False
        existing.append(BenchCase(id=cid, brief=brief, stack=(stack or "").strip()))
        existing = existing[-cap:]
        path = regression_cases_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        from skyn3t.atomic_io import atomic_write_text
        atomic_write_text(path, json.dumps([c.to_dict() for c in existing], indent=2))
        return True
    except Exception:  # noqa: BLE001 - capturing a case must never break a build
        return False


def all_cases(data_dir, *, include_games: bool = False) -> list[BenchCase]:
    """The app exam: built-in app cases plus captured app regressions.

    Captured game regressions are still stored, but the default autonomy/ratchet
    loop is app-first; callers must opt in before Phaser failures re-enter it.
    """
    regressions = load_regression_cases(data_dir)
    if not include_games:
        regressions = [c for c in regressions if c.stack.strip().lower() != "phaser"]
    return list(DEFAULT_CASES) + regressions


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
