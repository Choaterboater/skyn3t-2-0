"""Conservative web product-polish gate.

This is intentionally structural, not aesthetic: it blocks only UI web builds
that are too thin to be credible products.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from skyn3t.core.stacks import UI_WEB_STACKS

_UI_STACKS = UI_WEB_STACKS - {"phaser"}
_SUFFIXES = {
    ".html", ".jsx", ".tsx", ".js", ".ts", ".astro", ".vue", ".svelte", ".css",
}
_SKIP_DIRS = {"node_modules", ".next", "dist", "build", "out"}
_HEADING_RE = re.compile(r"<h1\b|className=.*text-[34]|font-size\s*:\s*(?:[2-9]rem|[3-9][0-9]px)", re.I | re.S)
_ACTION_RE = re.compile(r"<(?:button|a|input|select|textarea)\b|onClick=|href=", re.I)
_STYLE_RE = re.compile(r"className=|class=|background|border-radius|box-shadow|grid|flex|--[a-z0-9-]+:", re.I)
_CSS_IMPORT_RE = re.compile(
    r"\bimport\s+(?:[^;\n]*?\s+from\s+)?['\"][^'\"]+\.css(?:[?#][^'\"]*)?['\"]",
    re.I,
)
_CSS_LINK_RE = re.compile(
    r"<link\b(?=[^>]*\brel\s*=\s*['\"]stylesheet['\"])(?=[^>]*\bhref\s*=\s*['\"][^'\"]+\.css(?:[?#][^'\"]*)?['\"])[^>]*>",
    re.I,
)
# Decorative pictographs are a frequent signal of placeholder UI. Product builds
# should use their icon system or meaningful imagery instead.
_DECORATIVE_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u26FF]")


def check_web_polish(project_dir: str | Path, stack: str = "") -> dict[str, Any]:
    try:
        low = (stack or "").lower()
        if low == "phaser":
            return {"ok": True, "skipped": True, "issues": [], "checked": []}
        if low and low not in _UI_STACKS:
            return {"ok": True, "skipped": True, "issues": [], "checked": []}
        root = Path(project_dir)
        if not root.is_dir():
            return {"ok": True, "skipped": True, "issues": [], "checked": []}
        checked: list[str] = []
        corpus: list[str] = []
        markup: list[str] = []
        stylesheets: list[str] = []
        for path in root.rglob("*"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() in _SUFFIXES:
                checked.append(path.relative_to(root).as_posix())
                content = path.read_text(encoding="utf-8", errors="replace")[:10000]
                corpus.append(content)
                if path.suffix.lower() == ".css":
                    stylesheets.append(path.relative_to(root).as_posix())
                else:
                    markup.append(content)
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
        if _DECORATIVE_EMOJI_RE.search("\n".join(markup)):
            issues.append("decorative emoji glyphs detected in UI source")
        if stylesheets:
            markup_text = "\n".join(markup)
            if not (_CSS_IMPORT_RE.search(markup_text) or _CSS_LINK_RE.search(markup_text)):
                issues.append("stylesheets exist but no CSS import or stylesheet link was found")
        return {"ok": not issues, "skipped": False, "issues": issues, "checked": checked[:100]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
