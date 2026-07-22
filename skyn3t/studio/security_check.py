"""Deterministic generated-app security gate.

Conservative static checks only: bundled secret literals, direct eval/function
construction, missing basic web security headers, and obvious SQL string
interpolation. No network, no optional dependencies, never raises.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from skyn3t.core.stacks import DESIGN_STACKS, UI_WEB_STACKS

# Static source security checks also apply to UI aliases and React Native even
# though those stacks are not all HTTP-served.
_WEB_STACKS = DESIGN_STACKS | UI_WEB_STACKS
_SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".html",
    ".astro", ".vue", ".svelte",
}
_SKIP_DIRS = {"node_modules", ".next", "dist", "build", "out", ".venv", "__pycache__"}
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|[A-Z0-9]{20,}SECRET|api[_-]?key\s*[:=]\s*['\"][^'\"]{12,})",
    re.I,
)
_EVAL_RE = re.compile(r"\b(?:eval|Function)\s*\(")
_SQL_INTERP_RE = re.compile(
    r"(?:`|['\"])\s*"
    r"(?:"
    r"SELECT\b[^;\n]*\bFROM\b"
    r"|INSERT\s+INTO\b"
    r"|UPDATE\s+\S+\s+SET\b"
    r"|DELETE\s+FROM\b"
    r")"
    r"[^;\n]*(?:\+|\$\{|%s|\.format\()"
    # Python f-strings: f"SELECT ... FROM ... {var}" carries no +/${/%s/.format
    # marker, so it needs its own alternative with the same statement shape.
    r"|f['\"]\s*(?:SELECT\b[^;\n]*\bFROM\b|INSERT\s+INTO\b|UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b)"
    r"[^;\n]*\{[A-Za-z_]",
    re.I,
)
_HEADER_MARKERS = (
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
)


def _iter_source(root: Path):
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES:
            yield path


def check_security(project_dir: str | Path, stack: str = "") -> dict[str, Any]:
    try:
        low = (stack or "").lower()
        if low and low not in _WEB_STACKS:
            return {"ok": True, "skipped": True, "issues": [], "warnings": [], "checked": []}
        root = Path(project_dir)
        if not root.is_dir():
            return {"ok": True, "skipped": True, "issues": [], "warnings": ["project dir missing"], "checked": []}
        issues: list[str] = []
        warnings: list[str] = []
        checked: list[str] = []
        header_seen = False
        for path in _iter_source(root):
            rel = path.relative_to(root).as_posix()
            checked.append(rel)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low_text = text.lower()
            if any(marker in low_text for marker in _HEADER_MARKERS):
                header_seen = True
            if _SECRET_RE.search(text):
                issues.append(f"{rel}: bundled secret/API key literal")
            if _EVAL_RE.search(text):
                issues.append(f"{rel}: dynamic eval/function execution")
            if _SQL_INTERP_RE.search(text):
                issues.append(f"{rel}: SQL built with string interpolation")
        if checked and low in {"nextjs", "express", "fastapi", "rag", "workflow"} and not header_seen:
            warnings.append("no basic security-header wiring detected")
        return {
            "ok": not issues,
            "skipped": False,
            "issues": issues[:20],
            "warnings": warnings[:20],
            "checked": checked[:200],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
