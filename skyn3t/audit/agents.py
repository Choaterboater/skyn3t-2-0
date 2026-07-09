from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.audit.models import AuditFinding, AuditSection

_SKIP_DIRS = {
    ".claude",
    ".codex",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".worktrees",
    "__pycache__",
    "build",
    "data",
    "dist",
    "logs",
    "node_modules",
    "Projects",
    "scratchpad",
    "wheelhouse",
    "worktrees",
}
_SKIP_DIR_PREFIXES = (".release_verify_",)
_CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".html"}
_SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


@dataclass(slots=True)
class AuditContext:
    repo_root: Path
    include_tests: bool = True
    max_findings: int = 20
    use_llm: bool = False
    llm: Any | None = None

    def read(self, rel: str, max_bytes: int = 120_000) -> str:
        path = self.repo_root / rel
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:max_bytes]
        except OSError:
            return ""

    def exists(self, rel: str) -> bool:
        return (self.repo_root / rel).exists()

    def iter_files(self, suffixes: set[str] | None = None) -> list[Path]:
        out: list[Path] = []
        try:
            for path in self.repo_root.rglob("*"):
                if not path.is_file():
                    continue
                rel_parts = path.relative_to(self.repo_root).parts
                if any(
                    part in _SKIP_DIRS
                    or any(part.startswith(prefix) for prefix in _SKIP_DIR_PREFIXES)
                    for part in rel_parts[:-1]
                ):
                    continue
                if not self.include_tests and rel_parts and rel_parts[0] == "tests":
                    continue
                if suffixes is not None and path.suffix not in suffixes:
                    continue
                out.append(path)
        except OSError:
            return out
        return sorted(out)

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)


class BaseAuditAgent:
    title = "Audit"

    def run(self, ctx: AuditContext) -> AuditSection:
        raise NotImplementedError

    @staticmethod
    def _cap_findings(findings: list[AuditFinding], limit: int) -> list[AuditFinding]:
        return sorted(findings, key=lambda f: (_SEVERITY_RANK.get(f.severity, 9), f.category, f.claim))[:limit]


class RepoReviewAgent(BaseAuditAgent):
    title = "Repo Review"

    def run(self, ctx: AuditContext) -> AuditSection:
        findings: list[AuditFinding] = []
        code_files = ctx.iter_files(_CODE_SUFFIXES)
        test_files = [p for p in ctx.iter_files({".py", ".js", ".jsx", ".ts", ".tsx"}) if ctx.rel(p).startswith("tests/")]
        source_files = [p for p in code_files if not ctx.rel(p).startswith("tests/")]
        todo_hits = _grep(ctx, re.compile(r"\b(TODO|FIXME|HACK|XXX)\b"))

        if not ctx.exists("docs/FILE_MAP.md"):
            findings.append(_finding(
                "P2", "docs", "The repo is missing docs/FILE_MAP.md.",
                ["docs/FILE_MAP.md"],
                "Keep a current map of high-value files for future audit/debug agents.",
            ))
        status = ctx.read("STATUS.md")
        reviewed = re.search(r"Last reviewed:\s*(\d{4}-\d{2}-\d{2})", status)
        if reviewed:
            try:
                reviewed_at = datetime.fromisoformat(reviewed.group(1)).date()
                age_days = (datetime.now(UTC).date() - reviewed_at).days
            except ValueError:
                age_days = 10_000
            if age_days < 0 or age_days > 14:
                findings.append(_finding(
                    "P3", "docs", f"STATUS.md was last reviewed {reviewed.group(1)}.",
                    ["STATUS.md"],
                    "Refresh STATUS.md after major audit council runs.",
                ))
        if len(todo_hits) >= 10:
            findings.append(_finding(
                "P2", "maintainability", f"Found {len(todo_hits)} TODO/FIXME/HACK markers across repo source.",
                todo_hits[:8],
                "Group recurring markers into intentional backlog items or remove stale comments.",
            ))
        if source_files and len(test_files) / len(source_files) < 0.15:
            findings.append(_finding(
                "P2", "tests", "The rough test-to-source file ratio is low for an app factory this broad.",
                ["tests/", "skyn3t/"],
                "Focus new coverage on audit, verifier, improve, and runtime routing seams.",
            ))

        score = 86.0 - min(25.0, len(findings) * 5.0)
        return AuditSection(
            title=self.title,
            score=max(40.0, score),
            findings=self._cap_findings(findings, ctx.max_findings),
            notes=[
                "Reviews SkyN3t repository structure, docs freshness, source/test shape, and maintenance signals.",
                "This is a factory audit, so generated project trees are intentionally out of scope for v1.",
            ],
            metrics={
                "code_files": len(code_files),
                "source_files": len(source_files),
                "test_files": len(test_files),
                "todo_markers": len(todo_hits),
            },
        )


class DebugAuditAgent(BaseAuditAgent):
    title = "Debug Audit"

    def run(self, ctx: AuditContext) -> AuditSection:
        findings: list[AuditFinding] = []
        expected = {
            "stage debug loop": "skyn3t/studio/stage_debug.py",
            "build verifier": "skyn3t/agents/build_verifier.py",
            "boot verifier": "skyn3t/agents/boot_verifier.py",
            "consistency reviewer": "skyn3t/agents/consistency_reviewer.py",
            "proof runner": "skyn3t/studio/proof_run.py",
        }
        present = {name: ctx.exists(rel) for name, rel in expected.items()}
        for name, ok in present.items():
            if not ok:
                findings.append(_finding(
                    "P1", "debug", f"Missing expected debug/proof component: {name}.",
                    [expected[name]],
                    "Restore or replace this component before trusting end-to-end product ratings.",
                ))

        runner = ctx.read("skyn3t/studio/runner.py")
        if "format_fix_feedback" not in runner:
            findings.append(_finding(
                "P1", "debug", "Runner does not appear to emit structured fix feedback.",
                ["skyn3t/studio/runner.py"],
                "Keep verifier gaps shaped as actionable QA handoffs for repair agents.",
            ))
        if "completed_no_go" not in runner:
            findings.append(_finding(
                "P2", "debug", "Runner may not distinguish failed builds from completed no-go builds.",
                ["skyn3t/studio/runner.py"],
                "Preserve completed_no_go so users can inspect a delivered but rejected artifact.",
            ))

        score = 90.0 - min(30.0, len(findings) * 8.0)
        return AuditSection(
            title=self.title,
            score=max(35.0, score),
            findings=self._cap_findings(findings, ctx.max_findings),
            notes=["Checks whether the factory has explicit proof, debug, verifier, and repair handoff surfaces."],
            metrics={"components_present": sum(1 for ok in present.values() if ok), "components_expected": len(expected)},
        )


class ProductRaterAgent(BaseAuditAgent):
    title = "Product Rating"

    _DIMENSIONS = {
        "reliability": ("skyn3t/studio/proof_run.py", "skyn3t/studio/bench.py"),
        "usability": ("skyn3t/web/ui/src/routes/Studio.jsx", "skyn3t/web/ui/src/routes/Settings.jsx"),
        "app_breadth": ("docs/APP_TYPES.md", "skyn3t/agents/_scaffold.py"),
        "proof_quality": ("skyn3t/studio/liveness.py", "skyn3t/studio/runner.py"),
        "autonomy": ("skyn3t/cortex/autonomous_loop.py", "skyn3t/cortex/proposal_store.py"),
        "maintainability": ("docs/FILE_MAP.md", "tests/"),
    }

    def run(self, ctx: AuditContext) -> AuditSection:
        findings: list[AuditFinding] = []
        dims: dict[str, float] = {}
        for name, paths in self._DIMENSIONS.items():
            found = sum(1 for rel in paths if ctx.exists(rel))
            score = round(100.0 * found / len(paths), 2)
            dims[name] = score
            if score < 100.0:
                findings.append(_finding(
                    "P2", "product-rating", f"{name.replace('_', ' ').title()} has incomplete evidence in the repo.",
                    list(paths),
                    f"Add or document the missing {name.replace('_', ' ')} surface before raising this rating.",
                ))
        overall = round(sum(dims.values()) / len(dims), 2)
        return AuditSection(
            title=self.title,
            score=overall,
            findings=self._cap_findings(findings, ctx.max_findings),
            notes=["Rates SkyN3t as an app factory across product and engineering dimensions."],
            metrics=dims,
        )


class GapAnalystAgent(BaseAuditAgent):
    title = "Gap Analysis"

    def run(self, ctx: AuditContext) -> AuditSection:
        future = ctx.read("docs/FUTURE_IDEAS.md").lower()
        roadmap = ctx.read("docs/ROADMAP.md").lower()
        checks = {
            "products_not_builds": ("products, not builds", "skyn3t/studio/improve.py"),
            "brief_fit_verdicts": ("brief-fit verdicts", "skyn3t/agents/test_author.py"),
            "bench_exam": ("bench = the factory", "skyn3t/studio/bench.py"),
            "ship_execution": ("ship it", "skyn3t/studio/deploy.py"),
            "portfolio_rag": ("portfolio rag", "skyn3t/rag/rag_engine.py"),
        }
        findings: list[AuditFinding] = []
        covered = 0
        for key, (phrase, rel) in checks.items():
            mentioned = phrase in future or phrase in roadmap
            exists = ctx.exists(rel)
            if mentioned and exists:
                covered += 1
            else:
                findings.append(_finding(
                    "P2", "gap", f"Roadmap capability '{key}' is not fully evidenced by docs plus code.",
                    ["docs/FUTURE_IDEAS.md", "docs/ROADMAP.md", rel],
                    "Decide whether this is shipped, partial, or intentionally deferred and reflect it in the next handoff.",
                ))
        score = round(100.0 * covered / len(checks), 2)
        return AuditSection(
            title=self.title,
            score=score,
            findings=self._cap_findings(findings, ctx.max_findings),
            notes=["Compares stated factory ambitions against repo-local evidence."],
            metrics={"covered": covered, "checked": len(checks)},
        )


class ExplorerAgent(BaseAuditAgent):
    title = "Explorer Map"

    def run(self, ctx: AuditContext) -> AuditSection:
        areas = {
            "CLI": "skyn3t/cli/main.py",
            "Studio pipeline": "skyn3t/studio/runner.py",
            "Proof and gates": "skyn3t/studio/proof_run.py",
            "Agents": "skyn3t/agents/",
            "Cortex": "skyn3t/cortex/",
            "Web UI": "skyn3t/web/ui/src/routes/Studio.jsx",
            "Bench": "skyn3t/studio/bench.py",
            "Docs": "docs/START_HERE.md",
        }
        findings = [
            _finding(
                "P3", "explorer", "Use this subsystem map as the next engineer's starting path.",
                list(areas.values()),
                "Open these paths first before broad searching; keep docs/FILE_MAP.md in sync when boundaries move.",
            )
        ]
        present = sum(1 for rel in areas.values() if ctx.exists(rel))
        return AuditSection(
            title=self.title,
            score=round(100.0 * present / len(areas), 2),
            findings=findings,
            notes=[f"{name}: {rel}" for name, rel in areas.items()],
            metrics={"mapped_areas": len(areas), "present": present},
        )


class ComparatorAgent(BaseAuditAgent):
    title = "Comparison Rubric"

    _RUBRIC = {
        "multi_stack_generation": ("Multiple app stacks and scaffolds", "skyn3t/agents/_scaffold.py"),
        "objective_proof": ("Objective proof/run gates", "skyn3t/studio/proof_run.py"),
        "visual_runtime_checks": ("Visual or liveness runtime checks", "skyn3t/studio/liveness.py"),
        "learning_memory": ("Build lessons and reusable memory", "skyn3t/memory/store.py"),
        "bench_regression": ("Bench/regression exam", "skyn3t/studio/bench.py"),
        "human_gated_autonomy": ("Human-gated autonomy proposals", "skyn3t/cortex/proposal_store.py"),
        "deploy_readiness": ("Deploy planning or execution", "skyn3t/studio/deploy.py"),
        "handoff_reporting": ("Durable handoff/reporting", "NEXT_SESSION.md"),
    }

    def run(self, ctx: AuditContext) -> AuditSection:
        findings: list[AuditFinding] = []
        rows: dict[str, str] = {}
        present = 0
        for key, (label, rel) in self._RUBRIC.items():
            ok = ctx.exists(rel)
            rows[key] = "present" if ok else "missing"
            present += int(ok)
            if not ok:
                findings.append(_finding(
                    "P2", "comparison", f"Capability missing from local app-builder rubric: {label}.",
                    [rel],
                    "Implement or document this capability before claiming parity with mature app-builder products.",
                ))
        score = round(100.0 * present / len(self._RUBRIC), 2)
        return AuditSection(
            title=self.title,
            score=score,
            findings=self._cap_findings(findings, ctx.max_findings),
            notes=["Local rubric only: v1 does not browse live competitors or make market freshness claims."],
            metrics=rows,
        )


def _finding(
    severity: str,
    category: str,
    claim: str,
    evidence: list[str],
    recommendation: str,
) -> AuditFinding:
    return AuditFinding(
        severity=severity,
        category=category,
        claim=claim,
        evidence=evidence,
        recommendation=recommendation,
    )


def _grep(ctx: AuditContext, pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for path in ctx.iter_files(_CODE_SUFFIXES):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            if pattern.search(line):
                hits.append(f"{ctx.rel(path)}:{i}")
                break
    return hits
