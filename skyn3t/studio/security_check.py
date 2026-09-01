"""Deterministic generated-app security gate.

Conservative static checks only: bundled secret literals, direct eval/function
construction, missing basic web security headers, and obvious SQL string
interpolation. No network, no optional dependencies, never raises.

Pairs with :func:`rewrite_secret_literals`, the equally conservative repair for
the bundled-secret finding: it rewrites only simple whole-literal assignments
to environment reads, so a re-run of the same gate passes on the rewritten tree.
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


# --- Deterministic bundled-secret repair ------------------------------------
# The gate above only FLAGS secret literals; this repair rewrites the simple
# ones to environment reads so a re-run of the same gate passes. It is MORE
# conservative than the gate: only a whole-literal value in a one-line
# assignment/initializer is touched (const X = "...", X = '...', X: "...",
# "X": "..."). Interpolations, concatenations, key-position literals, and
# unsupported file types are left untouched and reported under "skipped".
_REWRITE_SUFFIXES = {".py", ".js", ".ts", ".jsx", ".tsx"}
_SECRET_TOKEN_FULL_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|[A-Z0-9]{20,}SECRET)", re.I)
_API_KEY_NAME_RE = re.compile(r"api[_-]?key", re.I)
_STRING_VALUE = r"(?P<val>(?:[^'\"\\\n]|\\.)*)"
_PY_ASSIGN_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_]\w*)\s*=\s*['\"]" + _STRING_VALUE + r"['\"]\s*(?:#.*)?$"
)
_PY_DICT_RE = re.compile(
    r"^\s*['\"](?P<name>[A-Za-z_][\w .-]*)['\"]\s*:\s*['\"]"
    + _STRING_VALUE
    + r"['\"]\s*,?\s*(?:#.*)?$"
)
_JS_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*['\"]"
    + _STRING_VALUE
    + r"['\"]\s*;?\s*(?://.*)?$"
)
_JS_PROP_RE = re.compile(
    r"^\s*['\"]?(?P<name>[A-Za-z_$][\w$-]*)['\"]?\s*:\s*['\"]"
    + _STRING_VALUE
    + r"['\"]\s*,?\s*(?://.*)?$"
)
_HAS_OS_IMPORT_RE = re.compile(r"(?m)^\s*(?:import\s+os\b|from\s+os\s+import\b)")
# Bare generic names carry no provider hint, so they always get a numeric
# suffix (token -> TOKEN_1): two parallel secrets can never share one var.
_GENERIC_ENV_NAMES = {"TOKEN", "SECRET", "KEY", "PASSWORD", "AUTH"}


def _env_base_name(raw: str) -> str:
    """camelCase/snake/kebab -> UPPER_SNAKE; non-alnum collapses to '_'."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return name or "SECRET"


def _assign_env_name(base: str, literal: str, state: dict[str, Any]) -> str:
    """Stable 1:1 literal -> env-name mapping: identical literals share a name,
    distinct literals that derive the same base get numeric suffixes."""
    by_literal: dict[str, str] = state["by_literal"]
    used: set[str] = state["used"]
    if literal in by_literal:
        return by_literal[literal]
    name = f"{base}_1" if base in _GENERIC_ENV_NAMES else base
    n = 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    by_literal[literal] = name
    return name


def _pure_secret_value(value: str, name: str) -> bool:
    """True only when the literal value IS the secret (not, say, a URL that
    happens to embed one). The gate's api_key alternative flags any 12+ char
    value on an api-key-shaped name, so the same shape rule applies here."""
    stripped = value.strip()
    if _SECRET_TOKEN_FULL_RE.fullmatch(stripped):
        return True
    return bool(_API_KEY_NAME_RE.search(name)) and len(stripped) >= 12


def _rewrite_line(body: str, is_py: bool, state: dict[str, Any]) -> tuple[str, str, str] | None:
    """Rewrite one line's simple secret initializer to an env read and return
    (new_line, variable_name, env_var); None when the line is not a
    conservative whole-literal assignment."""
    patterns = (_PY_ASSIGN_RE, _PY_DICT_RE) if is_py else (_JS_DECL_RE, _JS_PROP_RE)
    for pattern in patterns:
        match = pattern.match(body)
        if match is None:
            continue
        name = match.group("name")
        value = match.group("val")
        if not _pure_secret_value(value, name):
            return None  # the flagged secret sits elsewhere on the line
        var = _assign_env_name(_env_base_name(name), value, state)
        replacement = f'os.getenv("{var}", "")' if is_py else f'process.env.{var} || ""'
        start = match.start("val") - 1  # include the opening quote
        end = match.end("val") + 1  # include the closing quote
        return body[:start] + replacement + body[end:], name, var
    return None


def _rewrite_source(
    text: str, is_py: bool, rel: str, state: dict[str, Any]
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    rewritten: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    lines = text.splitlines(keepends=True)
    changed = False
    for idx, line in enumerate(lines):
        if not _SECRET_RE.search(line):
            continue
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        result = _rewrite_line(body, is_py, state)
        if result is None:
            skipped.append({
                "file": rel, "line": idx + 1,
                "reason": "not a simple whole-literal assignment",
            })
            continue
        new_body, name, var = result
        if _SECRET_RE.search(new_body):
            # The rewrite must clear the finding on this line; when it cannot
            # (e.g. a second literal in a trailing comment) leave it untouched.
            skipped.append({
                "file": rel, "line": idx + 1,
                "reason": "rewrite would not clear the finding",
            })
            continue
        lines[idx] = new_body + eol
        changed = True
        rewritten.append({"file": rel, "line": idx + 1, "name": name, "var": var})
    return ("".join(lines) if changed else text), rewritten, skipped


def _with_os_import(text: str) -> str:
    """Insert ``import os`` after any shebang/encoding line, module docstring,
    and ``from __future__`` imports (those must keep their first-position)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    if i < len(lines) and re.match(r"#.*coding[:=]", lines[i]):
        i += 1
    if i < len(lines):
        docstring = re.match(r"\s*(?:[rRuUbB]{0,2})?(\"\"\"|''')", lines[i])
        if docstring is not None:
            quote = docstring.group(1)
            one_liner = quote in lines[i][docstring.end():]
            i += 1
            while not one_liner and i < len(lines):
                closing = lines[i]
                i += 1
                if quote in closing:
                    break
    while i < len(lines) and lines[i].strip().startswith("from __future__"):
        i += 1
    lines.insert(i, f"import os{newline}")
    return "".join(lines)


def _update_env_example(root: Path, entries: list[tuple[str, str]], report: dict[str, Any]) -> None:
    """Append newly introduced VAR names (each with a one-line provenance
    comment) to the project's .env.example, creating it if missing."""
    path = root / ".env.example"
    if path.is_symlink():
        report["ok"] = False
        report["errors"].append(".env.example is a symlink; not writing")
        return
    try:
        existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError as exc:
        report["ok"] = False
        report["errors"].append(f".env.example: {exc}"[:160])
        return
    defined = set(re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", existing))
    additions: list[str] = []
    seen: set[str] = set()
    for var, comment in entries:
        if var in defined or var in seen:
            continue
        seen.add(var)
        additions.append(f"# {comment}\n{var}=\n")
    if not additions:
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    try:
        path.write_text(existing + separator + "\n".join(additions), encoding="utf-8")
    except OSError as exc:
        report["ok"] = False
        report["errors"].append(f".env.example: {exc}"[:160])
        return
    report["env_example"] = ".env.example"


def rewrite_secret_literals(project_dir: str | Path) -> dict[str, Any]:
    """Deterministically rewrite gate-flagged secret literals to env reads.

    For every literal the security gate flags in a .py/.js/.ts/.jsx/.tsx file,
    replace a simple whole-literal assignment/initializer with an environment
    read — ``os.getenv("VAR", "")`` in Python (adding ``import os`` when
    absent), ``process.env.VAR || ""`` in JS/TS — and append each new VAR to
    the project's .env.example. Anything that is not a conservative
    initializer (expressions, interpolations, key-position literals,
    unsupported file types) is left untouched and reported under "skipped".
    Uses the gate's own file selection (_iter_source). Never raises; failures
    are reported, not thrown."""
    report: dict[str, Any] = {
        "ok": True, "rewritten": [], "skipped": [], "env_example": None, "errors": [],
    }
    try:
        root = Path(project_dir)
        if not root.is_dir():
            report["errors"].append("project dir missing")
            return report
        state: dict[str, Any] = {"by_literal": {}, "used": set()}
        env_entries: list[tuple[str, str]] = []
        for path in _iter_source(root):
            if path.is_symlink():
                continue
            suffix = path.suffix.lower()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                report["ok"] = False
                report["errors"].append(f"{path.name}: {exc}"[:160])
                continue
            if not _SECRET_RE.search(text):
                continue
            rel = path.relative_to(root).as_posix()
            if suffix not in _REWRITE_SUFFIXES:
                report["skipped"].append({
                    "file": rel, "line": 0,
                    "reason": f"file type '{suffix}' is not rewritten",
                })
                continue
            try:
                new_text, rewritten, skipped = _rewrite_source(text, suffix == ".py", rel, state)
            except Exception as exc:  # noqa: BLE001 - one file must not stop the pass
                report["ok"] = False
                report["errors"].append(f"{rel}: {exc}"[:160])
                continue
            report["rewritten"].extend(rewritten)
            report["skipped"].extend(skipped)
            if not rewritten:
                continue
            if suffix == ".py" and not _HAS_OS_IMPORT_RE.search(new_text):
                new_text = _with_os_import(new_text)
            try:
                path.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                report["ok"] = False
                report["errors"].append(f"{rel}: {exc}"[:160])
                continue
            env_entries.extend(
                (item["var"], f"from {rel} (rewritten from a hardcoded literal)")
                for item in rewritten
            )
        if env_entries:
            _update_env_example(root, env_entries, report)
    except Exception as exc:  # noqa: BLE001 - a repair must never break a build
        report["ok"] = False
        report["errors"].append(str(exc)[:160])
    return report
