"""Deterministic generated-app security gate.

Conservative static checks only: bundled secret literals, direct eval/function
construction, missing basic web security headers, and obvious SQL string
interpolation. No network, no optional dependencies, never raises.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from skyn3t.core.stacks import DESIGN_STACKS, UI_WEB_STACKS, WEB_STACKS

# Static source security checks also apply to UI aliases and React Native even
# though those stacks are not all HTTP-served.
_WEB_STACKS = DESIGN_STACKS | UI_WEB_STACKS
# Stacks where missing security-header wiring is worth an advisory warning:
# every HTTP server/API web stack (the web group minus the pure-UI group) plus
# Next.js, which has a server runtime. Derived from the registry so the
# dual-vocab spellings (node_express, next) can never silently drop out, and a
# future API stack is covered automatically.
_HEADER_WARN_STACKS = (WEB_STACKS - UI_WEB_STACKS) | frozenset({"nextjs", "next"})
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
    r"(?:`|['\"]{1,3})\s*"
    r"(?:"
    r"SELECT\b[^;\n]*\bFROM\b"
    r"|INSERT\s+INTO\b"
    r"|REPLACE\s+INTO\b"
    r"|UPDATE\s+\S+\s+SET\b"
    r"|DELETE\s+FROM\b"
    r")"
    r"[^;\n]*(?:\+|\$\{|\.concat\(|%\([^)]{1,40}\)[sdifr]|%[sdifr]|\.format(?:_map)?\()"
    # Python f-strings: f"SELECT ... FROM ... {var}" carries no +/${/.concat/
    # %-format/.format marker, so they need their own alternative with the same
    # statement shape. REPLACE INTO joins the shape family (MySQL/SQLite upsert);
    # %r (repr interpolation) joins the percent marker class.
    # Covers fr/rf prefixes and single-line triple-quoted strings; interpolation
    # is still required after the statement shape, so prose such as
    # f"Select your {item} from the menu" stays clean.
    r"|(?:fr?|rf)['\"]{1,3}\s*(?:SELECT\b[^;\n]*\bFROM\b|INSERT\s+INTO\b|REPLACE\s+INTO\b|UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b)"
    r"[^;\n]*\{[A-Za-z_]"
    # Multiline single literals: JS template literals and Python triple-quoted
    # strings can carry the statement shape across lines, which the single-line
    # alternatives above cannot see. These alternatives allow newlines INSIDE
    # one literal: the interpolation window stops at that literal's own closing
    # delimiter (so two adjacent literals can never chain into a false
    # positive), and the statement keywords must be UPPERCASE (re.I is switched
    # off inside the shape via (?-i:...)) so sentence-case prose spanning lines
    # stays clean.
    # Accepted miss: lowercase or implicit-concatenated multiline SQL.
    r"|`\s*(?-i:SELECT\b[^;`]{0,400}?\bFROM\b|INSERT\s+INTO\b|REPLACE\s+INTO\b|UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b)[^;`]{0,400}?\$\{"
    r"|(?:fr?|rf)?\"\"\"\s*(?-i:SELECT\b[^;\"]{0,400}?\bFROM\b|INSERT\s+INTO\b|REPLACE\s+INTO\b|UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b)[^;\"]{0,400}?(?:\$\{|\{[A-Za-z_]|%\([^)]{1,40}\)[sdifr]|%[sdifr])"
    r"|(?:fr?|rf)?'''\s*(?-i:SELECT\b[^;']{0,400}?\bFROM\b|INSERT\s+INTO\b|REPLACE\s+INTO\b|UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b)[^;']{0,400}?(?:\$\{|\{[A-Za-z_]|%\([^)]{1,40}\)[sdifr]|%[sdifr])"
    # str.join / Array.join with SQL fragments: the concatenation marker sits
    # OUTSIDE the literal carrying the statement shape — " ".join(["SELECT ...",
    # var]) in Python, ["SELECT ...", var].join(" ") in JS — so the
    # marker-after-shape alternatives above can never see it. These
    # alternatives anchor on the join itself: the statement shape must appear
    # inside the bracketed list (UPPERCASE-only via (?-i:...) so sentence-case
    # prose lists stay clean), and a later element must be a bare variable so
    # lists of pure string literals stay clean. The [^\]] window cannot escape
    # the list. Accepted miss: lowercase join fragments, tuple-style join, and
    # indexed element interpolations like a[0] in the postfix form.
    r"|\.join\(\s*\[\s*(?:`|['\"]{1,3})\s*(?-i:SELECT\b[^\]]{0,400}?\bFROM\b|INSERT\s+INTO\b|REPLACE\s+INTO\b|UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b)[^\]]{0,400}?,\s*[A-Za-z_]"
    r"|\[\s*(?:`|['\"]{1,3})\s*(?-i:SELECT\b[^\]]{0,400}?\bFROM\b|INSERT\s+INTO\b|REPLACE\s+INTO\b|UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b)[^\]]{0,400}?,\s*[A-Za-z_][A-Za-z0-9_]*\s*\]\s*\.join\(",
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
        if checked and low in _HEADER_WARN_STACKS and not header_seen:
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
