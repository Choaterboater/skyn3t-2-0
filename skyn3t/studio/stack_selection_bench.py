"""Selection-only benchmark; never builds apps or calls the fallback LLM."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.adapters.typesafe import TypeSafeAdapter
from skyn3t.atomic_io import atomic_write_text
from skyn3t.config.settings import Settings
from skyn3t.studio.bench import ALL_STACK_CASES
from skyn3t.studio.stack_selector import (
    _TYPESAFE_MIN_CONFIDENCE,
    _TYPESAFE_STACK_INSTRUCTIONS,
    REAL_BUILDER_STACKS,
    _validate_pin,
    keyword_choice,
    typesafe_choice_from_answer,
)


@dataclass(frozen=True)
class SelectionCase:
    id: str
    group: str
    brief: str
    acceptable: tuple[str, ...]  # Empty means ambiguous/unscored, not "all correct".


CASES = tuple(
    SelectionCase(c.id, "registry", c.brief, (_validate_pin(c.stack),))
    for c in ALL_STACK_CASES
) + (
    SelectionCase("plain-landing", "task_only",
                  "A single HTML landing page for a bakery, no framework or backend.", ("static",)),
    SelectionCase("terminal-rename", "task_only",
                  "A terminal utility to rename local files in batches, no browser UI.", ("python",)),
    SelectionCase("space-game", "task_only",
                  "A 2D space shooter playable in the browser, with power-ups and a boss.", ("phaser",)),
    SelectionCase("pomodoro", "task_only",
                  "A browser Pomodoro timer with start/pause/reset, work/break lengths, "
                  "a session counter and tick sound toggle. No backend.",
                  ("react", "react_ts", "vue", "sveltekit", "static")),
    SelectionCase("phone-native", "task_only",
                  "A native iPhone and iPad app using SwiftData and Apple's native UI.", ("swift_ios",)),
    SelectionCase("mac-native", "task_only",
                  "A native macOS menu-bar clipboard manager using Apple's native UI.", ("swift",)),
    SelectionCase("cross-platform-phone", "task_only",
                  "A shared JavaScript mobile app for both Android and iOS, not a website.",
                  ("react_native",)),
    SelectionCase("cross-platform-desktop", "task_only",
                  "A cross-platform desktop notes app with a Rust shell and web frontend.", ("tauri",)),
    SelectionCase("grounded-docs", "task_only",
                  "Ingest PDF documents, embed chunks, retrieve relevant passages and "
                  "answer questions with citations.", ("rag",)),
    SelectionCase("tool-server", "task_only",
                  "A Python stdio server implementing Model Context Protocol tools "
                  "for AI assistants to query local inventory.", ("mcp",)),
    SelectionCase("automation-ledger", "task_only",
                  "A multi-step automation runner with scheduled triggers, dry runs, "
                  "and a persistent execution ledger.", ("workflow",)),
    SelectionCase("personas-only", "task_only",
                  "A pack of distinct AI team personas as markdown definitions, "
                  "with lint and conversion tooling; no running service.", ("agent_pack",)),
    SelectionCase("negative-react", "contrast",
                  "Do not build a React frontend. Build a Python REST API for storing notes.",
                  ("fastapi",)),
    SelectionCase("negative-python", "contrast",
                  "Not a Python CLI. Build a TypeScript React browser dashboard for CSV charts.",
                  ("react_ts",)),
    SelectionCase("negative-mobile", "contrast",
                  "Not an iPhone app. Build a native macOS SwiftUI menu-bar timer.", ("swift",)),
    SelectionCase("game-docs", "contrast",
                  "An Astro documentation site about Phaser games, not a playable game.", ("astro",)),
    SelectionCase("vague-app", "ambiguous", "Build an app for my business.", ()),
    SelectionCase("vague-dashboard", "ambiguous", "A dashboard.", ()),
    SelectionCase("vague-platform", "ambiguous",
                  "A productivity tool, but I have not decided between web, mobile or desktop.", ()),
    SelectionCase("vague-existing", "ambiguous",
                  "Add a customer portal to my existing app. Its framework is not provided.", ()),
)


def distribution_evidence(answer: dict[str, Any]) -> dict[str, Any]:
    """Keep complete distributions; flag malformed evidence rather than repairing it."""
    evidence: dict[str, Any] = {
        "distribution_status": "missing",
        "probabilities": None,
        "selected_probability": None,
        "top_two_margin": None,
    }
    if "probabilities" not in answer:
        return evidence
    probabilities = answer["probabilities"]
    evidence["distribution_status"] = "invalid"
    if not isinstance(probabilities, dict) or set(probabilities) != set(REAL_BUILDER_STACKS):
        return evidence
    if any(
        isinstance(p, bool) or not isinstance(p, (int, float))
        or not math.isfinite(p) or not 0 <= p <= 1
        for p in probabilities.values()
    ):
        return evidence
    if not math.isclose(sum(probabilities.values()), 1.0, rel_tol=0, abs_tol=1e-6):
        return evidence
    selected = answer.get("choice")
    if not isinstance(selected, str) or selected not in probabilities:
        return evidence
    ranked = sorted(probabilities.values(), reverse=True)
    if probabilities[selected] != ranked[0]:
        return evidence
    return {
        "distribution_status": "valid",
        "probabilities": dict(probabilities),
        "selected_probability": probabilities[selected],
        "top_two_margin": ranked[0] - ranked[1],
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if r["acceptable"]]
    accepted = [r for r in scored if r["accepted"]]
    ambiguous = [r for r in rows if not r["acceptable"]]
    latencies = sorted(r["elapsed_s"] for r in rows if r["error"] is None)
    return {
        "requests": len(rows),
        "errors": sum(r["error"] is not None for r in rows),
        "scored_requests": len(scored),
        "raw_accuracy": sum(r["raw_correct"] for r in scored) / len(scored) if scored else None,
        "accepted_accuracy": (
            sum(r["raw_correct"] for r in accepted) / len(accepted) if accepted else None
        ),
        "accepted_scored": len(accepted),
        "accepted_wrong": sum(not r["raw_correct"] for r in accepted),
        "confident_wrong": sum(
            not r["raw_correct"] and r["confidence"] >= _TYPESAFE_MIN_CONFIDENCE
            for r in accepted
        ),
        "coverage": len(accepted) / len(scored) if scored else None,
        "fallback_requests": sum(not r["accepted"] for r in rows),
        "ambiguous_requests": len(ambiguous),
        "ambiguous_accepted": sum(r["accepted"] for r in ambiguous),
        "missing_distribution_requests": sum(r["distribution_status"] == "missing" for r in rows),
        "invalid_distribution_requests": sum(r["distribution_status"] == "invalid" for r in rows),
        "latency_median_s": statistics.median(latencies) if latencies else None,
        "latency_p95_s": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
    }


def run_benchmark(
    provider: str, output: Path, *, repeats: int = 3, settings: Settings | None = None,
) -> dict[str, Any]:
    if provider not in {"keyword", "typesafe"}:
        raise ValueError("provider must be keyword or typesafe")
    if not 1 <= repeats <= 10:
        raise ValueError("repeats must be between 1 and 10")
    suite = [asdict(c) for c in CASES]
    contract = {
        "cases": suite, "menu": REAL_BUILDER_STACKS,
        "instructions": _TYPESAFE_STACK_INSTRUCTIONS,
        "threshold": _TYPESAFE_MIN_CONFIDENCE,
    }
    report: dict[str, Any] = {
        "schema_version": 2,
        "created_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "model_requested": "jev-latest" if provider == "typesafe" else None,
        "suite_sha256": hashlib.sha256(
            json.dumps(contract, sort_keys=True).encode()
        ).hexdigest(),
        "contract": contract,
        "repeats": repeats,
        "status": "running",
        "scope": "raw classification before toolchain demotion; no fallback LLM or app builds",
        "limitations": [
            "Repository-authored acceptable sets, not independently adjudicated ground truth.",
            "Explicit framework prompts are easier than open-ended project descriptions.",
            "Ambiguous cases are unscored; accepted guesses are reported separately.",
            "Repeated prompts are not independent accuracy samples.",
            "Latency excludes client setup; one client is reused, unlike per-build construction.",
            "Confidence measures distribution concentration, not probability of correctness.",
            "Model identity and token usage are provider-reported when present, not billing proof.",
            "No inference of whole-build speedup.",
        ],
        "rows": [],
    }

    def save() -> None:
        report["summary"] = summarize(report["rows"])
        report["by_group"] = {
            group: summarize([r for r in report["rows"] if r["group"] == group])
            for group in sorted({c.group for c in CASES})
        }
        atomic_write_text(output, json.dumps(report, indent=2, allow_nan=False) + "\n")

    adapter = TypeSafeAdapter(settings) if provider == "typesafe" else None
    if adapter is not None and not adapter.available:
        report["status"] = "blocked"
        report["error"] = "TypeSafe SDK/key unavailable; configure TYPESAFE_API_KEY or SKYN3T_TYPESAFE_API_KEY."
        save()
        return report

    save()
    rng = random.Random(0)
    for repeat in range(1, repeats + 1):
        cases = list(CASES)
        rng.shuffle(cases)
        for case in cases:
            error = None
            raw: dict[str, Any] = {}
            started = time.perf_counter()
            try:
                if adapter is None:
                    choice = keyword_choice(case.brief)
                    raw = {"choice": choice.stack, "confidence": choice.confidence}
                else:
                    raw = adapter.evaluate_system_one(
                        state=case.brief,
                        questions={"stack": {
                            "type": "choice", "instructions": _TYPESAFE_STACK_INSTRUCTIONS,
                            "criteria": REAL_BUILDER_STACKS,
                        }},
                        raise_on_error=True,
                    ).get("stack", {})
                    choice = typesafe_choice_from_answer(raw)
                stack = raw.get("choice")
                confidence = raw.get("confidence")
                valid_confidence = (
                    not isinstance(confidence, bool)
                    and isinstance(confidence, (int, float))
                    and math.isfinite(confidence)
                    and 0 <= confidence <= 1
                )
                if not isinstance(stack, str) or stack not in REAL_BUILDER_STACKS or not valid_confidence:
                    error = "invalid_response"
                    choice = None
            except Exception as exc:  # noqa: BLE001 - record failed requests, never count as success
                error = type(exc).__name__
                choice = None
                stack, confidence, valid_confidence = None, None, False
            elapsed = time.perf_counter() - started
            evidence = distribution_evidence(raw)
            if adapter is None:
                evidence["distribution_status"] = "not_applicable"
            report["rows"].append({
                **asdict(case),
                **evidence,
                "repeat": repeat,
                "response_metadata": raw.get("response_metadata", {}),
                "raw_stack": stack if isinstance(stack, str) else None,
                "confidence": confidence if valid_confidence else None,
                "accepted": choice is not None,
                "raw_correct": (
                    error is None and stack in case.acceptable if case.acceptable else None
                ),
                "error": error,
                "elapsed_s": elapsed,
            })
            save()
    report["status"] = "completed_with_errors" if report["summary"]["errors"] else "completed"
    save()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("keyword", "typesafe"), required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_benchmark(args.provider, args.output, repeats=args.repeats)
    print(json.dumps({
        "status": report["status"], "output": str(args.output),
        "summary": report["summary"], "error": report.get("error"),
    }, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
