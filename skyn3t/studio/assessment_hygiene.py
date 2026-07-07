"""Lightweight drift checks for the assessment follow-up artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _contains(root: Path, rel: str, needle: str, *, absent: bool = False) -> dict[str, Any]:
    path = root / rel
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    found = needle in text
    return {
        "name": f"{rel}:{needle}",
        "ok": (not found) if absent else found,
        "path": rel,
    }


def _exists(root: Path, rel: str) -> dict[str, Any]:
    return {"name": f"{rel}:exists", "ok": (root / rel).exists(), "path": rel}


def check_assessment_artifacts(root: str | Path) -> list[dict[str, Any]]:
    """Return named pass/fail checks for recent assessment remediation slices."""
    base = Path(root)
    return [
        _exists(base, "docs/FIRST_RUN.md"),
        _contains(base, "docs/INDEX.md", "FIRST_RUN.md"),
        _exists(base, "skyn3t/web/ui/public/fonts/fonts.css"),
        _contains(base, "skyn3t/web/ui/index.html", "fonts.googleapis", absent=True),
        _contains(base, "skyn3t/web/routes.py", "reference_images"),
        _contains(base, "skyn3t/studio/deploy.py", "rollback_deployment"),
        _contains(base, "skyn3t/studio/runner.py", "write_app_observability"),
        _exists(base, "skyn3t/studio/app_observability.py"),
        _contains(base, "skyn3t/cortex/prompt_evolver.py", "evolve_from_manifest"),
        _exists(base, "skyn3t/studio/collaboration.py"),
        _exists(base, "skyn3t/studio/flow_canvas.py"),
        _contains(base, "skyn3t/studio/runner.py", "write_flow_canvas"),
    ]
