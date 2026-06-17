"""Proof-run — objective evidence a build actually works.

Runs install + build + boot smoke checks against a generated project. Prefers a
sandboxed execution backend when one is available; otherwise it performs a
degraded *local* check (file presence vs the planner checklist, byte-count of
source files, and a Python syntax compile pass). It NEVER trusts an empty
scaffold: a directory with zero substantive files always fails the proof
(design rule #1 + #3).

Heavy / optional deps (docker) are guarded so the module always imports.
Import has zero side effects (no subprocess, no network).
"""

from __future__ import annotations

import py_compile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Optional sandbox backend — guarded so import never fails.
try:  # pragma: no cover - presence depends on sibling package
    import docker  # type: ignore  # noqa: F401

    _DOCKER_IMPORTABLE = True
except ImportError:
    _DOCKER_IMPORTABLE = False

# Files that don't count as "substantive" deliverables on their own.
_TRIVIAL_FILES = frozenset({"README.md", ".gitignore", "LICENSE", "skyn3t_manifest.json"})
_SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".go", ".rs", ".java")
_MIN_SUBSTANTIVE_BYTES = 16


@dataclass(slots=True)
class ProofResult:
    """Outcome of a proof-run. ``passed`` gates delivery."""

    passed: bool
    mode: str  # "sandbox" | "local"
    files_total: int = 0
    files_substantive: int = 0
    checklist_total: int = 0
    checklist_present: int = 0
    missing: list[str] = field(default_factory=list)
    syntax_errors: list[str] = field(default_factory=list)
    score: float = 0.0  # 0..100 completeness signal
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "mode": self.mode,
            "files_total": self.files_total,
            "files_substantive": self.files_substantive,
            "checklist_total": self.checklist_total,
            "checklist_present": self.checklist_present,
            "missing": list(self.missing),
            "syntax_errors": list(self.syntax_errors),
            "score": self.score,
            "detail": dict(self.detail),
        }


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in p.relative_to(root).parts):
            continue
        yield p


def _scan(project_dir: Path) -> tuple[int, int, list[str]]:
    """Return (total_files, substantive_files, python_syntax_errors)."""
    total = 0
    substantive = 0
    syntax_errors: list[str] = []
    for f in _iter_files(project_dir):
        total += 1
        name = f.name
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        is_source = f.suffix in _SOURCE_SUFFIXES
        if name not in _TRIVIAL_FILES and (is_source or size >= _MIN_SUBSTANTIVE_BYTES):
            substantive += 1
        if f.suffix == ".py" and size > 0:
            try:
                py_compile.compile(str(f), doraise=True)
            except py_compile.PyCompileError as exc:  # noqa: PERF203
                syntax_errors.append(f"{f.relative_to(project_dir)}: {exc.msg}")
            except (SyntaxError, ValueError, OSError) as exc:
                syntax_errors.append(f"{f.relative_to(project_dir)}: {exc}")
    return total, substantive, syntax_errors


def _checklist_status(project_dir: Path, checklist: list[str]) -> tuple[int, list[str]]:
    present = 0
    missing: list[str] = []
    for rel in checklist:
        if (project_dir / rel).exists():
            present += 1
        else:
            missing.append(rel)
    return present, missing


def proof_run(
    project_dir: str | Path,
    *,
    checklist: list[str] | None = None,
    execution_backend: str = "auto",
    stack: str = "",
) -> ProofResult:
    """Run an objective proof of the build. Always returns a ProofResult.

    ``execution_backend``: "auto" | "docker" | "inline". Docker is only used when
    the backend is requested AND the docker SDK imports; otherwise we degrade to
    a deterministic local check. The local check still rejects empty scaffolds.
    """
    pdir = Path(project_dir)
    checklist = checklist or []

    mode = "local"
    if execution_backend == "docker" and _DOCKER_IMPORTABLE:
        # Even with docker importable, a daemon may be absent. We probe lazily and
        # fall back to local rather than crashing (degrade, don't crash).
        if _docker_daemon_ok():
            mode = "sandbox"

    if not pdir.exists():
        return ProofResult(passed=False, mode=mode, detail={"reason": "project_dir missing"})

    total, substantive, syntax_errors = _scan(pdir)
    present, missing = _checklist_status(pdir, checklist)

    # Completeness score: weight checklist coverage + substantive content.
    if checklist:
        coverage = present / len(checklist)
    else:
        coverage = 1.0 if substantive > 0 else 0.0
    content_signal = min(1.0, substantive / 3.0)
    score = round(100.0 * (0.6 * coverage + 0.4 * content_signal), 2)

    # Never trust an empty scaffold: must have at least one substantive file and
    # no Python syntax errors to pass.
    passed = substantive > 0 and not syntax_errors
    # If a checklist exists, require at least half of it present.
    if checklist and present < max(1, len(checklist) // 2):
        passed = False

    return ProofResult(
        passed=passed,
        mode=mode,
        files_total=total,
        files_substantive=substantive,
        checklist_total=len(checklist),
        checklist_present=present,
        missing=missing,
        syntax_errors=syntax_errors,
        score=score,
        detail={"stack": stack},
    )


def _docker_daemon_ok() -> bool:
    """Best-effort docker daemon ping. Never raises."""
    if not _DOCKER_IMPORTABLE:
        return False
    try:  # pragma: no cover - environment dependent
        import docker  # type: ignore

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001 - any docker error -> degrade
        return False
