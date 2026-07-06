"""Conservative web product-polish gate.

This is intentionally structural, not aesthetic: it blocks only UI web builds
that are too thin to be credible products.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_UI_STACKS = {"react", "react_vite", "nextjs", "astro", "remix", "static", "phaser"}
_SUFFIXES = {".html", ".jsx", ".tsx", ".js", ".ts", ".astro", ".css"}
_SKIP_DIRS = {"node_modules", ".next", "dist", "build", "out"}
_HEADING_RE = re.compile(r"<h1\b|className=.*text-[34]|font-size\s*:\s*(?:[2-9]rem|[3-9][0-9]px)", re.I | re.S)
_ACTION_RE = re.compile(r"<(?:button|a|input|select|textarea)\b|onClick=|href=", re.I)
_STYLE_RE = re.compile(r"className=|class=|background|border-radius|box-shadow|grid|flex|--[a-z0-9-]+:", re.I)


def check_web_polish(project_dir: str | Path, stack: str = "") -> dict[str, Any]:
    try:
        low = (stack or "").lower()
        if low and low not in _UI_STACKS:
            return {"ok": True, "skipped": True, "issues": [], "checked": []}
        root = Path(project_dir)
        if not root.is_dir():
            return {"ok": True, "skipped": True, "issues": [], "checked": []}
        checked: list[str] = []
        corpus: list[str] = []
        for path in root.rglob("*"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() in _SUFFIXES:
                checked.append(path.relative_to(root).as_posix())
                corpus.append(path.read_text(encoding="utf-8", errors="replace")[:10000])
        if not checked:
            return {"ok": True, "skipped": True, "issues": [], "checked": []}
        text = "\n".join(corpus)
        issues: list[str] = []
        if not _HEADING_RE.search(text):
            issues.append("no primary heading or hero-scale title detected")
        if not _ACTION_RE.search(text):
            issues.append("no user action/link/form control detected")
        if not _STYLE_RE.search(text):
            issues.append("no meaningful styling/layout signal detected")
        return {"ok": not issues, "skipped": False, "issues": issues, "checked": checked[:100]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
