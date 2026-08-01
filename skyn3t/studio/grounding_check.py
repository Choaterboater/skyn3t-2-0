"""Semantic grounding lint (deterministic, advisory).

Ported from the v0 Design Systems 2.0 rule: if a component, prop, or token
cannot be verified from the project's own sources, it should not be used.
Enforced here as a deterministic lint, not prompt text — and deliberately
ADVISORY only (warnings, never blocking issues): both detectors are weak
signals. A var() may be defined at runtime by a framework or a JS theme
provider the lint cannot see, and a missing import target may be satisfied
by a bundler alias or generated at build time. The gate's job is to record
the smell on the manifest and hand the improve loop concrete fix hints —
not to veto the build.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from skyn3t.core.stacks import UI_WEB_STACKS

_UI_STACKS = UI_WEB_STACKS - {"phaser"}
_SKIP_DIRS = {"node_modules", ".next", "dist", "build", "out"}
# Cap files read: a lint over a delivered project must stay cheap and must
# never wander into a vendored tree. Anything past the cap is simply not
# linted (advisory coverage, not completeness).
_MAX_FILES = 400
_MAX_CHARS_PER_FILE = 20000
_CSS_SUFFIXES = {".css"}
_MARKUP_SUFFIXES = {".html", ".jsx", ".tsx", ".astro", ".vue", ".svelte"}
_IMPORT_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".astro"}

# Custom-property DEFINITIONS: `--foo: value` inside any selector. Markup
# files are scanned whole — an approximation that intentionally also covers
# <style> blocks, inline style="..." attributes, and CSS-in-JS template
# literals without parsing each dialect. False positives just mean a var is
# treated as defined, i.e. fewer warnings: the safe direction for an
# advisory lint.
_CSS_VAR_DEF_RE = re.compile(r"(--[A-Za-z][\w-]*)\s*:")
# var() REFERENCES, capturing whether a fallback follows the name:
# var(--x)      -> fallback group empty -> must be defined somewhere
# var(--x, #fff)-> fallback present     -> self-grounding, always fine
_CSS_VAR_REF_RE = re.compile(r"var\(\s*(--[A-Za-z][\w-]*)\s*(,|\))")
# Framework-provided vars are defined by the toolchain, not the project
# sources; flagging them would be pure noise.
_ALLOWLIST_VARS = frozenset({"--vh", "--vw"})
_ALLOWLIST_PREFIXES = ("--astro-", "--next-")

# Relative imports only. Bare package imports are reconciled elsewhere
# (proof_run's relink_unresolved_relative_imports fixes some of these
# pre-delivery; this lint is the post-delivery/improve-time net). CSS
# @import/url() are ignored — those follow different resolution rules.
_REL_IMPORT_RE = re.compile(
    r"(?:import|export)\s[^;\n'\"]*?\sfrom\s*['\"](\.{1,2}/[^'\"]+)['\"]"
    r"|import\s*['\"](\.{1,2}/[^'\"]+)['\"]"
    r"|import\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)"
    r"|require\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)"
)
_RESOLVE_EXTS = (".js", ".jsx", ".ts", ".tsx", ".astro", ".css", ".json")


def _line_of(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _resolve_relative(base_dir: Path, spec: str) -> bool:
    """True when a relative import target exists on disk. Extensionless
    specs try the common source/stylesheet extensions and directory
    index.* files — matching what mainstream bundlers resolve."""
    spec = spec.split("?", 1)[0].split("#", 1)[0]
    target = base_dir.joinpath(*spec.split("/"))
    if target.is_file():
        return True
    for ext in _RESOLVE_EXTS:
        if Path(str(target) + ext).is_file():
            return True
    if target.is_dir():
        return any((target / f"index{ext}").is_file() for ext in _RESOLVE_EXTS)
    return False


def check_grounding(project_dir: str | Path, stack: str = "") -> dict[str, Any]:
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
        files: list[tuple[str, Path, str]] = []
        for path in root.rglob("*"):
            if len(files) >= _MAX_FILES:
                break
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            suffix = path.suffix.lower()
            if path.is_file() and suffix in (_CSS_SUFFIXES | _MARKUP_SUFFIXES | _IMPORT_SUFFIXES):
                rel = path.relative_to(root).as_posix()
                checked.append(rel)
                content = path.read_text(encoding="utf-8", errors="replace")[:_MAX_CHARS_PER_FILE]
                files.append((rel, path, content))
        if not checked:
            return {"ok": True, "skipped": True, "issues": [], "checked": []}

        # Detector A — undefined CSS custom properties. Weak signal: a var
        # may be injected at runtime by a JS theme provider or a CDN build
        # of a framework. Advisory warning only.
        defined: set[str] = set()
        for _rel, _path, content in files:
            defined.update(_CSS_VAR_DEF_RE.findall(content))
        missing: dict[str, tuple[str, int]] = {}
        for rel, _path, content in files:
            for match in _CSS_VAR_REF_RE.finditer(content):
                name, fallback = match.group(1), match.group(2)
                if fallback == ",":
                    continue
                if name in defined:
                    continue
                if name in _ALLOWLIST_VARS or name.startswith(_ALLOWLIST_PREFIXES):
                    continue
                if name not in missing:
                    missing[name] = (rel, _line_of(content, match.start()))

        # Detector B — phantom local component imports. Weak signal: a
        # bundler alias or a build-time generated file can satisfy an import
        # the disk scan cannot see. Advisory warning only.
        phantom: dict[str, tuple[str, int]] = {}
        for rel, path, content in files:
            if path.suffix.lower() not in _IMPORT_SUFFIXES:
                continue
            for match in _REL_IMPORT_RE.finditer(content):
                spec = next(g for g in match.groups() if g)
                if _resolve_relative(path.parent, spec):
                    continue
                if spec not in phantom:
                    phantom[spec] = (rel, _line_of(content, match.start()))

        warnings: list[str] = []
        for name in sorted(missing):
            rel, line = missing[name]
            warnings.append(
                f"undefined CSS custom property var({name}) referenced at "
                f"{rel}:{line} — define it in the project styles or add a fallback"
            )
        for spec in sorted(phantom):
            rel, line = phantom[spec]
            warnings.append(
                f"phantom local import '{spec}' at {rel}:{line} — target file "
                "does not exist; create it or fix the path"
            )
        return {
            "ok": True,
            "skipped": False,
            "issues": [],
            "warnings": warnings,
            "checked": checked[:100],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "skipped": True, "issues": [], "warnings": [str(exc)[:160]], "checked": []}
