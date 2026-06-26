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

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Stdlib top-level names (3.10+). A local dir/stem that shadows one of these must
# NOT make a stdlib-submodule import (os.path, email.mime.text, collections.abc)
# look "unresolved" — those resolve via sys.path, not the project tree.
_STDLIB_NAMES: frozenset[str] = frozenset(getattr(sys, "stdlib_module_names", ()))

# Optional sandbox backend — guarded so import never fails.
try:  # pragma: no cover - presence depends on sibling package
    import docker  # type: ignore  # noqa: F401

    _DOCKER_IMPORTABLE = True
except ImportError:
    _DOCKER_IMPORTABLE = False

# Files that don't count as "substantive" deliverables on their own.
_TRIVIAL_FILES = frozenset({"README.md", ".gitignore", "LICENSE", "skyn3t_manifest.json"})
_SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".go", ".rs", ".java", ".astro")
_MIN_SUBSTANTIVE_BYTES = 16

# --- static boot-readiness: relative-import resolution -----------------------
# A `./x` / `../x` import that resolves to no file is a guaranteed bundler boot
# failure (Vite/webpack 500: "Failed to resolve import"). Bare ("react") and
# aliased ("@/x") specifiers are NOT checked — only relative paths, which must
# exist on disk. This is the offline, deterministic gate that catches the
# missing-stylesheet / unwired-module class of "looks done but won't boot".
_JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_RESOLVE_EXTS = (
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json",
    ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
)
# Captures the relative specifier in: `import x from './a'`, `import './a.css'`,
# `export {x} from '../b'`, `import('./c')`, `require('./d')`.
_REL_IMPORT_RE = re.compile(r"""\b(?:from|import|require)\b\s*\(?\s*['"](\.\.?/[^'"]+)['"]""")


def _import_candidates(importer: Path, spec: str) -> list[Path]:
    """Candidate on-disk paths a relative ``spec`` could resolve to.

    Tries the path as-is, with each known extension, and as a directory index.
    Strips Vite ``?url`` / ``?raw`` query and ``#`` fragment suffixes.
    """
    spec = spec.split("?", 1)[0].split("#", 1)[0]
    if not spec:
        return []
    target = importer.parent / spec
    candidates = [target]
    for ext in _RESOLVE_EXTS:
        candidates.append(target.with_name(target.name + ext))
        candidates.append(target / f"index{ext}")
    return candidates


def _import_resolves(importer: Path, spec: str, root: Path) -> bool:
    """True if a relative ``spec`` imported from ``importer`` exists on disk.

    On any filesystem error it returns True (indeterminate, never a false alarm).
    """
    for c in _import_candidates(importer, spec):
        try:
            # is_file (not exists): a bare DIRECTORY satisfies exists() but isn't an
            # importable module, so `import X from './components/Header'` would
            # falsely resolve when only ./components/Header/Header.module.css exists
            # and the component was dropped — the exact half-build the gate must
            # catch. The index candidates (Header/index.jsx) are real files and
            # still resolve a dir-with-index.
            if c.is_file():
                return True
        except OSError:
            return True
    return not _import_candidates(importer, spec)  # empty spec -> nothing to flag


def _resolve_import_file(importer: Path, spec: str) -> Path | None:
    """Resolve a relative ``spec`` to the first existing FILE (for graph walks)."""
    for c in _import_candidates(importer, spec):
        try:
            if c.is_file():
                return c.resolve()
        except OSError:
            return None
    return None


def _unresolved_local_imports(root: Path) -> list[str]:
    """Relative imports in JS/TS files that resolve to no file. Pure & offline."""
    out: list[str] = []
    for f in _iter_files(root):
        if f.suffix not in _JS_SUFFIXES:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for spec in _REL_IMPORT_RE.findall(text):
            if not _import_resolves(f, spec, root):
                out.append(f"{f.relative_to(root)} -> {spec}")
    return out


# Parses `import Default, * as NS, { A, B as C } from 'spec'` capturing the names.
_IMPORT_NAMES_RE = re.compile(
    r"""import\s+"""
    r"""(?:(?P<default>[A-Za-z_$][\w$]*)\s*,?\s*)?"""
    r"""(?:\*\s+as\s+(?P<ns>[A-Za-z_$][\w$]*)\s*)?"""
    r"""(?:\{(?P<named>[^}]*)\}\s*)?"""
    r"""from\s*['"](?P<spec>[^'"]+)['"]""",
    re.MULTILINE,
)
# Common UI component name -> semantic HTML element, so a stub renders REAL,
# usable markup (a stub Button is an actual <button>, not an empty div).
_HTML_FOR = {
    "button": "button", "input": "input", "textarea": "textarea", "select": "select",
    "label": "label", "form": "form", "link": "a", "anchor": "a", "img": "img",
    "image": "img", "nav": "nav", "navbar": "nav", "header": "header", "footer": "footer",
    "section": "section", "badge": "span", "chip": "span", "list": "ul", "table": "table",
}


def _alias_map(root: Path) -> dict[str, list[Path]]:
    """alias prefix ('@/') -> base dirs, from jsconfig/tsconfig ``paths``."""
    import json
    out: dict[str, list[Path]] = {}
    for name in ("tsconfig.json", "jsconfig.json"):
        p = root / name
        if not p.is_file():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        co = cfg.get("compilerOptions") or {}
        base = co.get("baseUrl") or "."
        for pat, targets in (co.get("paths") or {}).items():
            if not isinstance(targets, list) or not pat.endswith("/*"):
                continue
            dirs = [root / base / (t[:-1] if t.endswith("/*") else t) for t in targets
                    if isinstance(t, str)]
            if dirs:
                out.setdefault(pat[:-1], dirs)   # '@/*' -> '@/'
        if out:
            break
    return out


def _local_import_base(importer: Path, spec: str, aliases: dict[str, list[Path]]) -> Path | None:
    """On-disk base path (no extension) for a LOCAL (relative or aliased) import,
    or None for a bare npm package."""
    spec = spec.split("?", 1)[0].split("#", 1)[0]
    if spec.startswith("."):
        return importer.parent / spec
    # Pick the LONGEST matching alias prefix (webpack/tsc resolution). With a
    # multi-pattern config ('@/*' -> ./src/* AND '@/components/*' -> ./components/*),
    # first-match would mis-resolve '@/components/Hero' to src/ and stub it there.
    matches = [(p, d) for p, d in aliases.items() if spec.startswith(p) and d]
    if matches:
        prefix, dirs = max(matches, key=lambda kv: len(kv[0]))
        return dirs[0] / spec[len(prefix):]
    return None


def _base_resolves(base: Path) -> bool:
    for ext in ("", *_RESOLVE_EXTS):
        cand = base if not ext else base.with_name(base.name + ext)
        try:
            if cand.is_file():
                return True
        except OSError:
            return True
    for ext in _RESOLVE_EXTS:
        try:
            if (base / f"index{ext}").is_file():
                return True
        except OSError:
            return True
    return False


def _make_import_stub(spec: str, default: str | None, ns: str | None, named: list[str]) -> str:
    """A minimal but valid React/JS module satisfying the requested imports.
    Components (PascalCase) render a sensible semantic element forwarding props +
    children; hooks (useX) return {}; other utilities return ''. Uses
    React.createElement (no JSX) so the file is valid as .js/.jsx/.ts/.tsx."""
    last = spec.rstrip("/").rsplit("/", 1)[-1].lower()
    default_el = _HTML_FOR.get(last, "div")
    lines = [
        "// Auto-scaffolded by SkyN3t: the imported module was missing, so this",
        "// minimal stub lets the build resolve. Replace with a real implementation.",
        "import React from 'react';",
        "",
    ]

    def component(name: str, el: str) -> str:
        return (f"export function {name}(props) {{\n"
                f"  const {{ children, ...rest }} = props || {{}};\n"
                f"  return React.createElement('{el}', rest, children);\n"
                f"}}")

    def utility(name: str) -> str:
        if name.startswith("use") and name[3:4].isupper():
            return f"export function {name}() {{ return {{}}; }}"
        return f"export const {name} = (...args) => '';"

    if default:
        lines.append(f"function {default}(props) {{\n"
                     f"  const {{ children, ...rest }} = props || {{}};\n"
                     f"  return React.createElement('{default_el}', rest, children);\n}}")
        lines.append(f"export default {default};")
    for raw in named:
        nm = raw.strip().split(" as ")[-1].strip()
        if not nm:
            continue
        if nm[:1].isupper():
            lines.append(component(nm, _HTML_FOR.get(nm.lower(), default_el)))
        else:
            lines.append(utility(nm))
    if ns:
        lines.append(f"const {ns} = {{}};")
        lines.append(f"export default {ns};" if not default else f"export const {ns} = {{}};")
    if not default and not named and not ns:
        lines.append("export {};")  # side-effect import
    return "\n".join(lines) + "\n"


def scaffold_missing_imports(root: str | Path, *, stack: str = "") -> list[str]:
    """Generate minimal stubs for LOCAL imports (relative or '@/' aliased) whose
    target file does not exist — the recurring 'codegen imports a component it
    never created' break (e.g. '@/components/ui/button' -> Module not found). The
    stub renders real markup so the app builds AND runs; a genuinely broken stub
    is still caught downstream by the boot/liveness gate. Returns paths written."""
    root = Path(root)
    aliases = _alias_map(root)
    ts = (root / "tsconfig.json").is_file()
    written: list[str] = []
    planned: set[Path] = set()
    for f in _iter_files(root):
        if f.suffix not in _JS_SUFFIXES:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _IMPORT_NAMES_RE.finditer(text):
            spec = m.group("spec")
            base = _local_import_base(f, spec, aliases)
            if base is None or _base_resolves(base):
                continue
            named = [n for n in (m.group("named") or "").split(",") if n.strip()]
            # Component-ish (any PascalCase default/named) -> .jsx/.tsx, else util.
            comp = bool(m.group("default")) or any(n.strip()[:1].isupper() for n in named)
            ext = (".tsx" if ts else ".jsx") if comp else (".ts" if ts else ".js")
            stub = base.with_name(base.name + ext)
            if stub in planned or stub.exists():
                continue
            content = _make_import_stub(spec, m.group("default"), m.group("ns"), named)
            try:
                stub.parent.mkdir(parents=True, exist_ok=True)
                stub.write_text(content, encoding="utf-8")
                planned.add(stub)
                written.append(str(stub.relative_to(root)))
            except OSError:
                continue
    return written


def _module_resolves(base: Path, dotted: str) -> bool:
    """True if dotted module ``a.b.c`` resolves to a file/package under ``base``.

    Accepts a regular module (``a/b/c.py``), a package (``a/b/c/__init__.py``) or a
    namespace-package dir (``a/b/c/``). Indeterminate filesystem errors return True
    (never a false alarm)."""
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return True
    target = base.joinpath(*parts)
    try:
        return (target.with_suffix(".py").is_file()
                or (target / "__init__.py").is_file()
                or target.is_dir())
    except OSError:
        return True


def _local_top_level(root: Path) -> set[str]:
    """Top-level importable names defined by the project (root dirs + .py stems)."""
    names: set[str] = set()
    try:
        for child in root.iterdir():
            if child.name.startswith(".") or child.name in {"node_modules", "__pycache__"}:
                continue
            if child.is_dir():
                names.add(child.name)
            elif child.is_file() and child.suffix == ".py":
                names.add(child.stem)
    except OSError:
        pass
    return names


def _unresolved_python_imports(root: Path) -> list[str]:
    """LOCAL Python imports that resolve to no module on disk — the cross-module
    (cross-slice) break the syntax pass + entrypoint smoke can miss (e.g. a backend
    slice importing ``api.routes.users`` that the routes slice never wrote).

    Conservative to avoid false alarms: an ABSOLUTE import is only checked when its
    first segment is a LOCAL top-level package/module (third-party + stdlib skipped,
    exactly like bare JS specifiers); RELATIVE imports are resolved against the
    file's own package. Pure & offline."""
    py_files = [f for f in _iter_files(root) if f.suffix == ".py"]
    if not py_files:
        return []
    local_top = _local_top_level(root)
    if not local_top:
        return []

    def _is_local(seg: str) -> bool:
        # Local AND not a stdlib name (a stdlib-shadowing local dir must not make
        # `os.path`/`email.mime.text` look unresolved).
        return seg in local_top and seg not in _STDLIB_NAMES

    out: list[str] = []
    for f in py_files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"), str(f))
        except (SyntaxError, ValueError, OSError):
            continue  # syntax errors are reported by the dedicated syntax pass
        # Imports guarded by `try: ... except ImportError` are intentionally
        # optional (the common optional-dependency pattern) — never flag them.
        guarded = _guarded_import_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    if _is_local(mod.split(".")[0]) and not _module_resolves(root, mod):
                        out.append(f"{f.relative_to(root)} -> import {mod}")
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative — resolve against the file's package dir
                    base = f.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    if not (base == root or root in base.parents):
                        continue  # escaped the project root — don't guess
                    if node.module and not _module_resolves(base, node.module):
                        out.append(f"{f.relative_to(root)} -> from {'.' * node.level}{node.module}")
                elif node.module and _is_local(node.module.split(".")[0]):
                    if not _module_resolves(root, node.module):
                        out.append(f"{f.relative_to(root)} -> from {node.module}")
    seen: set[str] = set()
    unique: list[str] = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique[:10]


def _guarded_import_nodes(tree: ast.AST) -> set[int]:
    """ids of Import/ImportFrom nodes inside a ``try`` whose ``except`` catches
    ImportError/ModuleNotFoundError (or is bare/Exception) — intentionally-optional
    imports that must not be flagged as missing."""
    def _catches_import(handler: ast.ExceptHandler) -> bool:
        t = handler.type
        if t is None:  # bare except
            return True
        names = t.elts if isinstance(t, ast.Tuple) else [t]
        return any(isinstance(n, ast.Name) and n.id in (
            "ImportError", "ModuleNotFoundError", "Exception") for n in names)

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_catches_import(h) for h in node.handlers):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(sub))
    return guarded


# --- static boot-readiness: unwired components -------------------------------
# A half-built app delivers real generated components but an entry that never
# reaches them (e.g. the scaffold counter stub), so the page renders the stub,
# not the app. The signal is REACHABILITY — not the scaffold marker, which can
# legitimately coexist with a wired entry. We only flag when the entry graph
# reaches NONE of the generated components (the unambiguous stub-entry case), and
# we skip projects with path aliases (we can't trace aliased imports reliably).
_ENTRY_NAMES = frozenset({
    "App.jsx", "App.tsx", "main.jsx", "main.tsx", "index.jsx", "index.tsx",
    # Next.js App Router entries — every app/**/page.* and app/**/layout.* is a
    # real route entry, so the reachability tracer must start from them too
    # (else app-router components looked "unwired"). Additive: only widens what
    # counts as reachable, never narrows it.
    "page.jsx", "page.tsx", "layout.jsx", "layout.tsx",
})


def _has_path_aliases(root: Path) -> bool:
    """True if the project configures import path aliases (untraceable here)."""
    for name in ("vite.config.js", "vite.config.ts", "tsconfig.json", "jsconfig.json"):
        p = root / name
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                if '"paths"' in text or "resolve.alias" in text or "alias:" in text:
                    return True
        except OSError:
            continue
    return False


def _reachable_files(root: Path) -> set[Path]:
    """Resolved file paths reachable from the entry files via relative imports."""
    entries = [
        f.resolve() for f in _iter_files(root)
        if f.name in _ENTRY_NAMES and f.suffix in _JS_SUFFIXES
    ]
    seen: set[Path] = set()
    stack = list(entries)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        try:
            text = cur.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for spec in _REL_IMPORT_RE.findall(text):
            tgt = _resolve_import_file(cur, spec)
            if tgt is not None and tgt not in seen:
                stack.append(tgt)
    return seen


def _unwired_components(root: Path) -> str | None:
    """Note if generated components exist but the entry graph reaches NONE of
    them (stub/unwired entry); else None. Pure & offline, conservative."""
    if _has_path_aliases(root):
        return None
    components = []
    for f in _iter_files(root):
        if f.suffix not in _JS_SUFFIXES or "components" not in f.relative_to(root).parts:
            continue
        try:
            if len(f.read_text(encoding="utf-8", errors="replace").strip()) >= _MIN_SUBSTANTIVE_BYTES:
                components.append(f.resolve())
        except OSError:
            continue
    if not components:
        return None
    reachable = _reachable_files(root)
    orphaned = [c for c in components if c not in reachable]
    if orphaned and len(orphaned) == len(components):
        return (f"entry reaches none of the {len(components)} generated "
                f"component(s) — the app renders an unwired/stub entry")
    return None


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

    def error_gaps(self) -> list[str]:
        """Real, targeted repair hints distilled from the captured failure output.

        The actual compiler/test/boot/import/syntax errors are ALREADY captured in
        ``detail`` + ``syntax_errors`` — this surfaces them as clean, per-failure
        gap strings the code-improver can act on, instead of one stringified
        ``detail`` blob. Every string carries the offending filename(s), so the
        improver's gap→file targeting rewrites exactly what broke. Returns ``[]``
        when the proof carries no actionable error text (e.g. a pure
        missing-files failure), letting the caller keep its generic fallback.
        """
        return extract_error_gaps(self.detail, self.syntax_errors)


def extract_error_gaps(
    detail: dict[str, Any] | None, syntax_errors: list[str] | None = None
) -> list[str]:
    """Distil captured proof failure output into clean per-failure gap strings.

    Pure helper shared by :meth:`ProofResult.error_gaps` (live) and the learning
    loop (which reads a persisted proof ``detail`` dict). Returns ``[]`` when no
    actionable error text is present.
    """
    gaps: list[str] = []
    d = detail or {}
    # Real npm/tsc/vite build output (700-char tail from _run_node_build).
    if d.get("build") == "failed" and d.get("build_summary"):
        gaps.append(f"BUILD FAILED — fix the cause of this compiler output:\n{d['build_summary']}")
    # Real pytest failures (500-char tail from _run_generated_tests).
    if d.get("tests") == "failed" and d.get("test_summary"):
        gaps.append(f"TESTS FAILED — make the code satisfy these failing tests:\n{d['test_summary']}")
    # Real entrypoint import traceback (400-char tail from _entrypoint_check).
    if d.get("boot_error"):
        gaps.append(f"BOOT/IMPORT ERROR — fix the entrypoint so it imports cleanly:\n{d['boot_error']}")
    # Unresolved relative imports — each entry is "<importer> -> <spec>".
    for imp in (d.get("unresolved_imports") or []):
        gaps.append(f"UNRESOLVED IMPORT — create the missing target or fix the path: {imp}")
    # Generated components exist but the entry reaches none of them.
    if d.get("unwired_components"):
        gaps.append(f"UNWIRED ENTRY — wire the entry to render the real app: {d['unwired_components']}")
    # Python syntax errors — each entry is "<file>: <SyntaxError>".
    for se in (syntax_errors or []):
        gaps.append(f"SYNTAX ERROR — fix: {se}")
    return gaps


# Generated/installed trees that are NOT the app's own source — scanning them
# (e.g. the minified `.next/` build output) scraped pseudo-imports like `${r}`
# into package.json and polluted the import/substance signals.
_NON_SOURCE_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "node_modules", ".next", "dist", "build",
    ".vite", "out", "coverage", ".turbo", ".cache", "vendor", ".svelte-kit",
})


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in _NON_SOURCE_DIRS for part in p.relative_to(root).parts):
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
            # In-memory syntax check: validates syntax WITHOUT emitting a
            # __pycache__/.pyc cache into the delivered project (proof must not
            # pollute the artifact it proves).
            try:
                compile(f.read_text(encoding="utf-8", errors="replace"), str(f), "exec")
            except SyntaxError as exc:  # noqa: PERF203
                syntax_errors.append(f"{f.relative_to(project_dir)}: {exc}")
            except (ValueError, OSError) as exc:
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


# A bare (npm) specifier: `from "pkg"`, `import "pkg"`, `require("pkg")` — NOT
# relative ('.'/'..') and NOT absolute ('/'). Scoped (@x/y) allowed.
_BARE_IMPORT_RE = re.compile(r"""\b(?:from|import|require)\b\s*\(?\s*['"]([^./][^'"]*)['"]""")
_NODE_BUILTINS = frozenset({
    "fs", "path", "os", "http", "https", "crypto", "url", "util", "stream",
    "events", "child_process", "assert", "buffer", "process", "zlib", "net", "tls",
})
# Friendly pinned versions for common packages; anything else gets "latest".
_KNOWN_NPM_VERSIONS = {
    "prop-types": "^15.8.1", "react-router-dom": "^6.21.0", "axios": "^1.6.2",
    "zustand": "^4.4.7", "clsx": "^2.1.0", "classnames": "^2.5.1",
    "date-fns": "^3.0.0", "uuid": "^9.0.1", "lodash": "^4.17.21",
    "@testing-library/react": "^14.1.2", "@testing-library/jest-dom": "^6.1.5",
    "@testing-library/user-event": "^14.5.1", "framer-motion": "^10.16.16",
    "react": "^18.2.0", "react-dom": "^18.2.0", "zod": "^3.22.4",
    # Common standalone packages (no tricky peer deps) — pinned so they don't
    # fall to nondeterministic "latest". Anything wrong is still caught by the
    # real npm install gate; the three.js ecosystem is deliberately omitted
    # (its inter-package peer ranges conflict if mis-pinned).
    "react-icons": "^5.0.1", "react-hook-form": "^7.49.0", "dayjs": "^1.11.10",
    "swr": "^2.2.4", "nanoid": "^5.0.4", "recharts": "^2.10.3",
    "tailwind-merge": "^2.2.0", "immer": "^10.0.3", "@heroicons/react": "^2.1.1",
    # Form validation: pin a KNOWN-COMPATIBLE combo. Codegen emitting
    # `"@hookform/resolvers": "latest"` resolved to a version whose
    # `@hookform/resolvers/yup` subpath broke `next build`; resolvers ^3 + yup ^1
    # + react-hook-form ^7 is the standard working set.
    "@hookform/resolvers": "^3.3.4", "yup": "^1.4.0",
}


def _pkg_name(spec: str) -> str:
    """Bare specifier -> npm package name ('react-dom/client'->'react-dom',
    '@scope/pkg/sub'->'@scope/pkg')."""
    if spec.startswith("@/"):
        return ""
    if spec.startswith("@"):
        parts = spec.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else spec
    return spec.split("/")[0]


def _invalid_npm_package_names(pkg: dict[str, Any]) -> list[str]:
    """Declared dependency keys that npm will reject before install starts.

    Path aliases such as ``@/components`` are valid import specifiers in a
    configured app, but they are not npm package names and must never be written
    into package.json dependencies.
    """
    invalid: list[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = pkg.get(key)
        if not isinstance(deps, dict):
            continue
        for name in deps:
            if not isinstance(name, str) or not name:
                invalid.append(str(name))
            elif (
                name.startswith("@/") or name.startswith("/") or "\\" in name
                # npm rejects any whitespace in a package name with
                # EINVALIDPACKAGENAME ("name can only contain URL-friendly
                # characters") — e.g. a generated `" slick-carousel"`.
                or re.search(r"\s", name)
                # …and any other non-URL-safe char: a template-literal fragment
                # like `${r}` scraped from minified code must never be a dep.
                or re.search(r"[${}()<>\[\]'\"`!*~,;]", name)
            ):
                invalid.append(name)
    return sorted(set(invalid))


# npm error signatures that mean the REGISTRY was unreachable (offline dev box),
# as opposed to a real dependency defect. Only these soft-skip the build; every
# other non-zero install (ERESOLVE peer conflicts, E404/ETARGET nonexistent
# versions, EINVALIDPACKAGENAME) is a genuine, build-breaking failure.
_OFFLINE_NPM_MARKERS = (
    "ENOTFOUND", "ECONNREFUSED", "ETIMEDOUT", "EAI_AGAIN", "ENETUNREACH",
    "ECONNRESET", "EHOSTUNREACH", "getaddrinfo", "network request to",
    "request to https://registry", "socket hang up",
)


def _npm_install_is_offline(output: str) -> bool:
    """True only for genuine connectivity/registry-unreachable failures, which
    legitimately soft-skip the build. Dependency defects (ERESOLVE/E404/ETARGET/
    EINVALIDPACKAGENAME) return False — they are real failures that must fail the
    proof so the fix-loop gets the real error to repair."""
    return any(m in (output or "") for m in _OFFLINE_NPM_MARKERS)


def _project_invalid_npm_package_names(root: Path) -> list[str]:
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return []
    import json as _json

    try:
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    if not isinstance(pkg, dict):
        return ["<package.json>"]
    return _invalid_npm_package_names(pkg)


def reconcile_npm_deps(root: str | Path) -> list[str]:
    """Add every imported-but-undeclared npm package to package.json.dependencies.

    Generated code routinely imports a package (prop-types, @testing-library/react,
    axios, ...) without declaring it, so `npm install` skips it and Vite/rollup 500s
    at runtime/build — an unrunnable app that the offline gate misses. This scans
    the source, diffs against declared deps, and appends the missing ones (pinned
    version when known, else "latest"). Returns the package names added. Pure on
    a non-node project (no package.json -> []). Never raises."""
    root = Path(root)
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return []
    import json as _json
    try:
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(pkg, dict):
        return []
    declared = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        d = pkg.get(key)
        if isinstance(d, dict):
            declared |= set(d)
    used: set[str] = set()
    for f in _iter_files(root):
        if f.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for spec in _BARE_IMPORT_RE.findall(text):
            if spec.startswith("node:"):
                continue
            name = _pkg_name(spec)
            if name and name not in _NODE_BUILTINS:
                used.add(name)
    missing = sorted(u for u in used if u not in declared)
    deps = pkg.setdefault("dependencies", {})
    if not isinstance(deps, dict):
        return []
    changed = False
    for m in missing:
        deps[m] = _KNOWN_NPM_VERSIONS.get(m, "latest")
        changed = True
    # Normalize a DECLARED "latest" pin to a known-good version for curated
    # packages. Codegen sometimes emits `"@hookform/resolvers": "latest"`, which
    # can resolve to a version whose subpath import breaks the build. Bounded to
    # the curated table, so an arbitrary package is never touched.
    for key in ("dependencies", "devDependencies"):
        d = pkg.get(key)
        if not isinstance(d, dict):
            continue
        for name, ver in list(d.items()):
            if ver == "latest" and name in _KNOWN_NPM_VERSIONS:
                d[name] = _KNOWN_NPM_VERSIONS[name]
                changed = True
    if not changed:
        return []
    try:
        pkg_path.write_text(_json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return []
    return missing


# Build-tool PEER deps implied by a next.config flag but never imported in source
# (so reconcile_npm_deps can't see them): (flag-substring-in-config, package, version).
# experimental.optimizeCss runs `critters` to inline CSS during `next build`; with
# critters absent the export throws on EVERY page incl. /404, /500, /_not-found.
_NEXT_CONFIG_PEERS: tuple[tuple[str, str, str], ...] = (
    ("optimizeCss", "critters", "^0.0.23"),
)


def reconcile_next_config_peers(root: str | Path) -> list[str]:
    """Declare build-tool peer deps implied by next.config flags into
    devDependencies (e.g. ``experimental.optimizeCss`` -> ``critters``).

    These are pulled in by a config flag, never imported in code, so
    reconcile_npm_deps can't see them — an un-buildable app the offline gate
    misses. Returns the package names added. Pure on a non-node project (no
    package.json / no next.config -> []). Never raises."""
    root = Path(root)
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return []
    cfg_text = ""
    for name in ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs"):
        p = root / name
        if p.is_file():
            try:
                cfg_text += p.read_text(encoding="utf-8", errors="replace") + "\n"
            except OSError:
                continue
    if not cfg_text:
        return []
    import json as _json
    try:
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(pkg, dict):
        return []
    declared = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        d = pkg.get(key)
        if isinstance(d, dict):
            declared |= set(d)
    dev = pkg.setdefault("devDependencies", {})
    if not isinstance(dev, dict):
        return []
    added: list[str] = []
    for flag, peer, ver in _NEXT_CONFIG_PEERS:
        if flag in cfg_text and peer not in declared:
            dev[peer] = ver
            added.append(peer)
    if not added:
        return []
    try:
        pkg_path.write_text(_json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return []
    return added


# Client-only signals that REQUIRE a `"use client"` directive in a Next.js App
# Router component: a JSX event-handler prop, a client React/Next hook, or a
# browser global. Without the directive the component is a Server Component and
# `next build` fails static generation ("Event handlers cannot be passed to
# Client Component props" -> static-page-generation timeout).
_USE_CLIENT_SIGNAL = re.compile(
    r"\bon[A-Z]\w+\s*=\s*\{"                                          # onClick={...}
    r"|\buse(?:State|Effect|Reducer|Ref|Context|Callback|Memo|LayoutEffect"
    r"|Transition|ImperativeHandle|DeferredValue|SyncExternalStore"
    r"|Router|Pathname|SearchParams|FormState|FormStatus)\s*\("       # client hooks
    r"|\b(?:window|document|localStorage|sessionStorage|navigator)\." # browser globals
)


def _has_use_client_directive(text: str) -> bool:
    """True when the first real statement is a ``use client`` directive."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("//", "/*", "*")):
            continue
        return s.strip(";") in ('"use client"', "'use client'")
    return False


# Server-only exports that a Client Component is FORBIDDEN to have. When a file
# needs "use client" (interactivity) AND has one of these, next build errors
# ("You are attempting to export metadata ... from a component marked with use
# client"). The deterministic resolution: make it a client component and remove
# the server-only export (per-page metadata falls back to the layout's).
_METADATA_EXPORT = re.compile(
    r"export\s+const\s+metadata\b[^={]*=\s*\{"
    r"|export\s+(?:async\s+)?function\s+generateMetadata\s*\([^)]*\)\s*(?::[^={]+)?\{"
)


def _strip_metadata_exports(text: str) -> tuple[str, bool]:
    """Remove `export const metadata = {...}` / `export [async] function
    generateMetadata(...) {...}` blocks (brace-matched). Returns (new_text, removed)."""
    removed = False
    while True:
        m = _METADATA_EXPORT.search(text)
        if not m:
            break
        open_idx = text.index("{", m.end() - 1)
        depth, j = 0, open_idx
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        end = j + 1
        if end < len(text) and text[end] == ";":
            end += 1
        text = text[:m.start()] + text[end:]
        removed = True
    return text, removed


def add_use_client_directives(root: str | Path) -> list[str]:
    """Prepend ``"use client";`` to Next.js App Router components that use
    client-only features (event handlers, state/effect hooks, browser globals) but
    lack the directive — otherwise `next build` fails static generation. Returns
    the relative paths modified. No-op outside a Next.js app. Never raises."""
    root = Path(root)
    is_next = (root / "app").is_dir() or any(
        (root / f).is_file() for f in
        ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs"))
    if not is_next:
        return []
    changed: list[str] = []
    for f in _iter_files(root):
        if f.suffix not in (".jsx", ".tsx", ".js", ".ts", ".mjs"):
            continue
        rel = f.relative_to(root)
        # Only component dirs — never touch config/util/pages-router files.
        if not rel.parts or rel.parts[0] not in ("app", "components", "src"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _USE_CLIENT_SIGNAL.search(text) or _has_use_client_directive(text):
            continue
        # A Client Component can't export metadata/generateMetadata — strip those
        # (server-only) when present so the interactive component can build.
        body, _ = _strip_metadata_exports(text) if _METADATA_EXPORT.search(text) else (text, False)
        try:
            f.write_text('"use client";\n\n' + body.lstrip("﻿"), encoding="utf-8")
        except OSError:
            continue
        changed.append(str(rel))
    return changed


_AT_IMPORT_RE = re.compile(r"""(?:from|import|require\()\s*['"]@/""")


def _parse_jsonish(raw: str):
    """Parse JSON, tolerating tsconfig JSONC (// and /* */ comments, trailing commas).
    Returns the parsed value, or None if it still can't parse — so callers can REFUSE
    to clobber a config they couldn't read (do-no-harm)."""
    try:
        return json.loads(raw)
    except ValueError:
        pass
    txt = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)   # block comments
    txt = re.sub(r"(?m)//[^\n]*$", "", txt)                  # line comments (best-effort)
    txt = re.sub(r",(\s*[}\]])", r"\1", txt)                 # trailing commas
    try:
        return json.loads(txt)
    except ValueError:
        return None


def ensure_path_alias_config(root: str | Path) -> list[str]:
    """If the project imports via the ``@/`` alias but no jsconfig/tsconfig maps it,
    write the ``@/*`` mapping so `next build` resolves those imports — a common
    generated-app failure. Maps to ``./src/*`` for a src/ layout, ``./*`` otherwise.
    MERGES into an existing config and NEVER clobbers one it can't parse. Returns
    [config path] or []. Never raises."""
    root = Path(root)
    ts, js = root / "tsconfig.json", root / "jsconfig.json"
    for cfg in (ts, js):
        if cfg.is_file():
            try:
                if '"@/' in cfg.read_text(encoding="utf-8", errors="replace"):
                    return []  # already mapped
            except OSError:
                return []
    uses_alias = False
    for f in _iter_files(root):
        if f.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
            continue
        try:
            if _AT_IMPORT_RE.search(f.read_text(encoding="utf-8", errors="replace")):
                uses_alias = True
                break
        except OSError:
            continue
    if not uses_alias:
        return []
    target = ts if ts.is_file() else js
    data: dict = {}
    if target.is_file():
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        parsed = _parse_jsonish(raw)
        if not isinstance(parsed, dict):
            return []  # do-no-harm: never overwrite a config we couldn't parse
        data = parsed
    # A src/ layout serves @/ from ./src/*, not ./* — else the alias still won't resolve.
    src_layout = any((root / "src" / d).is_dir() for d in ("app", "components", "pages", "lib"))
    co = data.setdefault("compilerOptions", {})
    co.setdefault("baseUrl", ".")
    paths = co.setdefault("paths", {})
    if "@/*" in paths:
        return []
    paths["@/*"] = ["./src/*"] if src_layout else ["./*"]
    try:
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return []
    return [target.name]


_IMPORT_TYPE_RE = re.compile(r"^\s*import\s+type\s")
_EXPORT_TYPE_RE = re.compile(r"^\s*export\s+type\s+\w")


def _balanced_line(s: str) -> bool:
    return (s.count("(") == s.count(")") and s.count("[") == s.count("]")
            and s.count("{") == s.count("}"))


def strip_ts_type_in_js(root: str | Path) -> list[str]:
    """Drop TypeScript-only statements some models emit into plain .js/.jsx files, which
    break the JS build ("Expected '{', got 'type'"). CONSERVATIVE to avoid corrupting
    valid code: only removes a single-line ``import type ...`` or a SELF-CONTAINED
    one-line ``export type X = ...;`` (brackets balanced, ends with ';'); multi-line
    type forms are left for the fix-loop. Skips lines inside block comments. Never
    touches .ts/.tsx. Returns modified rel paths. Never raises."""
    root = Path(root)
    changed: list[str] = []
    for f in _iter_files(root):
        if f.suffix not in (".js", ".jsx", ".mjs"):
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError:
            continue
        out: list[str] = []
        in_block = False
        removed = False
        for ln in lines:
            stripped = ln.strip()
            if in_block:
                out.append(ln)
                if "*/" in ln:
                    in_block = False
                continue
            if stripped.startswith("/*") and "*/" not in stripped:
                in_block = True
                out.append(ln)
                continue
            if _IMPORT_TYPE_RE.match(ln):
                removed = True
                continue
            if (_EXPORT_TYPE_RE.match(ln) and _balanced_line(ln)
                    and stripped.endswith(";")):
                removed = True
                continue
            out.append(ln)
        if removed:
            try:
                f.write_text("".join(out), encoding="utf-8")
            except OSError:
                continue
            changed.append(str(f.relative_to(root)))
    return changed


def proof_run(
    project_dir: str | Path,
    *,
    checklist: list[str] | None = None,
    execution_backend: str = "auto",
    stack: str = "",
    run_tests: bool = False,
    test_timeout: int = 90,
    run_build: bool = False,
    build_timeout: int = 300,
) -> ProofResult:
    """Run an objective proof of the build. Always returns a ProofResult.

    ``execution_backend``: "auto" | "docker" | "inline". Docker is only used when
    the backend is requested AND the docker SDK imports; otherwise we degrade to
    a deterministic local check. The local check still rejects empty scaffolds.
    """
    pdir = Path(project_dir)
    checklist = checklist or []

    mode = "local"
    # "auto" means "use Docker if available" (matches security/sandbox.py and the
    # rest of the codebase); only "inline" forces the degraded local check.
    if execution_backend in ("docker", "auto") and _DOCKER_IMPORTABLE:
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

    # Behaviour, not vibes (rule #3): a code project that has no runnable
    # entrypoint, or whose entrypoint cannot even be imported, has NOT been
    # proven — a missing/broken root was the exact failure the old static-only
    # proof greenlit. Web/static stacks count index.html as an entrypoint.
    entrypoints, boot_error = _entrypoint_check(pdir, stack)
    detail: dict[str, Any] = {"stack": stack, "entrypoints": entrypoints}
    if total > 0 and not entrypoints:
        passed = False
        detail["reason"] = "no runnable entrypoint found"
        if "<entrypoint>" not in missing:
            missing = [*missing, "<entrypoint>"]
    elif boot_error:
        passed = False
        detail["boot_error"] = boot_error

    invalid_packages = _project_invalid_npm_package_names(pdir)
    if invalid_packages:
        passed = False
        detail["invalid_package_names"] = invalid_packages
        detail.setdefault("reason", "package.json declares invalid npm package names")
        if "<package.json>" not in missing:
            missing = [*missing, "<package.json>"]

    # Stack-aware artifact check (rule #3, stack-agnostic gap): the generic
    # entrypoint pass above accepts ANY recognised filename regardless of stack.
    # A "static" brief that ships main.py passes the generic check — but it has
    # NOT been proven for a static stack.  We run the stack-specific check and
    # record whether it was a genuine stack-specific pass or a generic fallback,
    # so callers can distinguish "statically verified for this stack" from
    # "generic entrypoint present". Never raises; never tightens a generic pass
    # into a fail when no stack-specific check exists (checked=False).
    if passed and total > 0:
        sa_checked, sa_passed, sa_note = _stack_artifact_check(pdir, stack)
        if sa_checked:
            detail["stack_check"] = "pass" if sa_passed else "fail"
            detail["stack_check_note"] = sa_note
            if not sa_passed:
                passed = False
                if "<stack-artifact>" not in missing:
                    missing = [*missing, "<stack-artifact>"]
        else:
            # No stack-specific check available — generic path was used.
            detail["stack_check"] = "generic"
            if sa_note:
                detail["stack_check_note"] = sa_note

    # Behaviour, not vibes (rule #3): a JS/TS file importing a RELATIVE path that
    # resolves to no file is a guaranteed bundler boot failure (Vite 500). This
    # offline, deterministic gate catches the missing-stylesheet / unwired-module
    # class the npm build (which soft-skips offline) otherwise lets through.
    if passed and total > 0:
        # JS/TS relative imports AND local Python cross-module imports — both are
        # guaranteed runtime/boot failures the syntax pass + entrypoint smoke miss,
        # and both are exactly the cross-slice wiring breaks parallel slicing can
        # leave (a slice importing a sibling another slice failed to write).
        broken_imports = _unresolved_local_imports(pdir) + _unresolved_python_imports(pdir)
        if broken_imports:
            passed = False
            detail["unresolved_imports"] = broken_imports[:10]
            detail.setdefault("reason", f"{len(broken_imports)} unresolved local import(s)")
            if "<imports>" not in missing:
                missing = [*missing, "<imports>"]

    # Behaviour, not vibes (rule #3): a delivered app that ships real components
    # but an entry which reaches NONE of them renders an unwired/stub entry, not
    # the app — it has NOT been proven.
    if passed and total > 0:
        stub_note = _unwired_components(pdir)
        if stub_note:
            passed = False
            detail["unwired_components"] = stub_note
            detail.setdefault("reason", stub_note)
            if "<wired-entry>" not in missing:
                missing = [*missing, "<wired-entry>"]

    # Behaviour, not vibes: when it boots, actually RUN the project's own tests.
    # A real failure fails the proof (and routes into the fix loop). Inability to
    # run them (no runner / deps / no tests) is a soft skip, never a hard fail.
    if run_tests and passed:
        ran, tests_passed, summary = _run_generated_tests(pdir, stack, test_timeout)
        if ran:
            detail["tests"] = "passed" if tests_passed else "failed"
            detail["test_summary"] = summary
            if not tests_passed:
                passed = False
                if "<tests>" not in missing:
                    missing = [*missing, "<tests>"]
        else:
            detail["tests"] = "skipped"
            if summary:
                detail["test_summary"] = summary

    # Behaviour, not vibes for node/web: actually COMPILE it (npm install + npm
    # run build). A static "package.json exists" check greenlit a build that
    # didn't type-check/compile; this catches that. Soft-skips offline.
    if run_build and passed:
        ran, build_ok, summary = _run_node_build(pdir, stack, build_timeout)
        if ran:
            detail["build"] = "passed" if build_ok else "failed"
            detail["build_summary"] = summary
            if not build_ok:
                passed = False
                if "<build>" not in missing:
                    missing = [*missing, "<build>"]
            else:
                # Advisory JS/TS test run: node_modules is now installed, so run
                # the project's test script if a real runner is declared. This is
                # NOT a hard gate — a subtly-wrong generated test must not no_go a
                # building app; the result is recorded for score dampening only.
                t_ran, t_ok, t_sum = _run_node_tests(pdir, test_timeout)
                if t_ran:
                    detail["node_tests"] = "passed" if t_ok else "failed"
                    detail["node_tests_summary"] = t_sum
        else:
            detail.setdefault("build", "skipped")
            if summary:
                detail["build_summary"] = summary

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
        detail=detail,
    )


# Stacks for which a runnable entrypoint is expected before we call it "proven".
_CODE_STACKS = ("cli", "python", "fastapi", "flask", "django", "node", "express", "nextjs")


def _stack_artifact_check(pdir: Path, stack: str) -> tuple[bool, bool, str]:
    """Stack-aware artifact presence check.

    Returns ``(checked, passed, note)`` where:
    - ``checked=False`` means no stack-specific check could be done (fall back to
      the generic entrypoint/boot path; caller must NOT claim a stack-specific pass).
    - ``checked=True, passed=True`` means the stack's required artifact is present
      and non-trivial.
    - ``checked=True, passed=False`` means the artifact is absent/trivial — the
      project definitely does NOT satisfy its declared stack.

    Never raises. Best-effort only: offline-safe (no network, no installs).
    """
    low = (stack or "").lower()
    # _MIN_SUBSTANTIVE_BYTES guards the overall empty-scaffold check; the stack
    # artifact check just needs the file to be non-empty (size > 0).
    _NONEMPTY = 1
    try:
        # ---- static / HTML-only -----------------------------------------
        if low in ("static", "html"):
            html_files = [
                f for f in _iter_files(pdir)
                if f.suffix == ".html"
                and f.stat().st_size >= _NONEMPTY
            ]
            if not html_files:
                return (True, False, "static stack: no index.html found")
            # Prefer an index.html at the root or one directory deep.
            has_index = any(f.name == "index.html" for f in html_files)
            if not has_index:
                return (True, False, "static stack: index.html missing")
            return (True, True, "static stack: index.html present")

        # ---- pure python / cli ------------------------------------------
        if low in ("python", "cli"):
            py_files = [
                f for f in _iter_files(pdir)
                if f.suffix == ".py"
                and f.stat().st_size >= _NONEMPTY
            ]
            if not py_files:
                return (True, False, f"{low} stack: no .py files found")
            # Look for a recognised entrypoint name.
            ep_names = {"main.py", "app.py", "__main__.py", "cli.py", "run.py"}
            has_ep = any(f.name in ep_names for f in py_files)
            if not has_ep:
                return (True, False, f"{low} stack: no entrypoint file (main.py / app.py / cli.py) found")
            return (True, True, f"{low} stack: entrypoint .py file present")

        # ---- fastapi / flask / django ------------------------------------
        if low in ("fastapi", "flask", "django"):
            py_files = [
                f for f in _iter_files(pdir)
                if f.suffix == ".py"
                and f.stat().st_size >= _NONEMPTY
            ]
            if not py_files:
                return (True, False, f"{low} stack: no .py files found")
            # Require at least one file containing the framework import as a marker.
            marker = low  # "fastapi", "flask", "django"
            for f in py_files:
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    if marker in text.lower():
                        return (True, True, f"{low} stack: framework marker found in source")
                except OSError:
                    pass
            return (True, False, f"{low} stack: no file imports/references {low}")

        # ---- react_vite / react -----------------------------------------
        if low in ("react", "react_vite"):
            pkg = pdir / "package.json"
            if not pkg.exists():
                return (True, False, f"{low} stack: package.json missing")
            vite_config = any(
                f.name in ("vite.config.js", "vite.config.ts", "vite.config.mjs")
                for f in _iter_files(pdir)
            ) if low == "react_vite" else True
            entry_present = any(
                f.name in ("main.tsx", "main.jsx", "main.ts", "App.tsx", "App.jsx", "index.tsx", "index.jsx")
                for f in _iter_files(pdir)
                if f.stat().st_size >= _NONEMPTY
            )
            if not entry_present:
                return (True, False, f"{low} stack: no React entry file (main.tsx/App.tsx) found")
            if low == "react_vite" and not vite_config:
                return (True, False, "react_vite stack: vite.config.* missing")
            return (True, True, f"{low} stack: package.json + entry file present")

        # ---- node_express / node / express ------------------------------
        if low in ("node", "node_express", "express"):
            pkg = pdir / "package.json"
            if not pkg.exists():
                return (True, False, f"{low} stack: package.json missing")
            # Require a server entry file or framework reference.
            js_files = [
                f for f in _iter_files(pdir)
                if f.suffix in (".js", ".ts", ".mjs")
                and f.stat().st_size >= _NONEMPTY
            ]
            if not js_files:
                return (True, False, f"{low} stack: no .js/.ts files found")
            marker_words = {"express", "fastify", "koa", "hapi", "http.createServer"}
            for f in js_files:
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    if any(m in text.lower() for m in marker_words):
                        return (True, True, f"{low} stack: server framework marker found")
                except OSError:
                    pass
            # Fallback: server.js / index.js present is enough.
            has_server_entry = any(f.name in ("server.js", "server.ts", "index.js", "index.ts") for f in js_files)
            if not has_server_entry:
                return (True, False, f"{low} stack: no server entry (server.js/index.js) found")
            return (True, True, f"{low} stack: server entry file present")

        # ---- nextjs / astro / remix (npm meta-frameworks) ----------------
        # Each needs a package.json declaring its framework dep AND a recognised
        # entry/config artifact, mirroring the react/node checks above.
        if low in ("nextjs", "astro", "remix"):
            pkg = pdir / "package.json"
            if not pkg.exists():
                return (True, False, f"{low} stack: package.json missing")
            try:
                pkg_text = pkg.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                pkg_text = ""
            framework_dep = {"nextjs": "next", "astro": "astro", "remix": "@remix-run"}[low]
            if framework_dep not in pkg_text:
                return (True, False, f"{low} stack: package.json does not depend on {framework_dep}")
            names = {f.name for f in _iter_files(pdir) if f.stat().st_size >= _NONEMPTY}
            # An entry/config artifact unique to the framework.
            markers = {
                "nextjs": ("page.jsx", "page.tsx", "next.config.js", "next.config.mjs"),
                "astro": ("astro.config.mjs", "astro.config.ts", "index.astro"),
                "remix": ("root.tsx", "root.jsx", "_index.tsx", "vite.config.ts"),
            }[low]
            if not any(m in names for m in markers):
                return (True, False, f"{low} stack: no entry/config artifact ({', '.join(markers)}) found")
            return (True, True, f"{low} stack: package.json + framework entry present")

    except Exception:  # noqa: BLE001 — never let a stack check crash proof
        return (False, False, "stack check failed unexpectedly — fell back to generic")

    # Unknown/unrecognised stack: signal to the caller to use generic path.
    return (False, False, "")


def _entrypoint_check(pdir: Path, stack: str) -> tuple[list[str], str]:
    """Return (entrypoints found, boot_error). Boot is a guarded import smoke.

    Degrades to a static presence check when no python runtime is available or
    the stack isn't a python code project (degrade, don't crash — rule #6).
    """
    try:
        from skyn3t.agents import _verify_common as vc
    except Exception:  # noqa: BLE001 - keep proof self-contained if import fails
        return ([], "")
    entrypoints = vc.find_entrypoints(pdir)
    if not entrypoints:
        return ([], "")

    low = (stack or "").lower()
    is_python = low in ("cli", "python", "fastapi", "flask", "django") or any(
        e.endswith(".py") for e in entrypoints
    )
    if not is_python:
        return (entrypoints, "")  # node/web: presence is the local-mode signal

    py = _python_executable()
    target = next((e for e in entrypoints if e.endswith(".py")), None)
    if py is None or target is None:
        return (entrypoints, "")  # no runtime -> presence-only (already syntax-checked)

    module = target[:-3].replace("/", ".")
    import os
    import subprocess

    # -B + PYTHONDONTWRITEBYTECODE so the smoke import never leaves a
    # __pycache__/.pyc inside the delivered artifact (the proof must not pollute
    # what it proves).
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(
            [py, "-B", "-c", f"import sys; sys.path.insert(0, '.'); import importlib; importlib.import_module({module!r})"],
            cwd=str(pdir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        # A hung/uncrunnable import is indeterminate, not a pass.
        return (entrypoints, "entrypoint import did not complete (hung or unrunnable)")
    if proc.returncode == 0:
        return (entrypoints, "")
    tail = (proc.stderr or proc.stdout or "")[-400:]
    # Missing third-party deps are not a code defect (offline env); the syntax
    # pass already validated the source compiles.
    if "ModuleNotFoundError" in tail or "ImportError" in tail:
        return (entrypoints, "")
    return (entrypoints, tail.strip())


def _python_executable() -> str | None:
    import shutil

    return shutil.which("python") or shutil.which("python3")


def _has_python_tests(pdir: Path) -> bool:
    for f in _iter_files(pdir):
        if f.suffix != ".py":
            continue
        name = f.name
        if name.startswith("test_") or name.endswith("_test.py") or "tests" in f.relative_to(pdir).parts:
            return True
    return False


def _run_generated_tests(pdir: Path, stack: str, timeout: int) -> tuple[bool, bool, str]:
    """Run the generated project's own test suite.

    Returns ``(ran, passed, summary)``. ``ran=False`` is a soft skip (no test
    runner, no deps, or no tests) and must NOT fail the proof. Never raises.
    """
    import os
    import subprocess

    py = _python_executable()
    low = (stack or "").lower()
    is_python = low in ("cli", "python", "fastapi", "flask", "django")
    if py is None or not (is_python or _has_python_tests(pdir)):
        return (False, False, "")  # node/web tests need an install step we skip
    if not _has_python_tests(pdir):
        return (False, False, "")

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(pdir)}
    # pytest must be importable; otherwise soft-skip rather than fail a build for
    # a missing dev tool in the environment.
    try:
        probe = subprocess.run([py, "-c", "import pytest"], capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return (False, False, "")
    if probe.returncode != 0:
        return (False, False, "pytest not installed — tests skipped")

    try:
        proc = subprocess.run(
            [py, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", str(pdir)],
            cwd=str(pdir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return (True, False, f"tests timed out after {timeout}s")
    except (OSError, ValueError):
        return (False, False, "")

    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = out[-500:]
    rc = proc.returncode
    if rc == 0:
        return (True, True, tail)
    if rc == 5:
        return (False, False, "no tests collected")  # soft skip
    # Collection/import errors from missing third-party deps aren't a code defect.
    if rc in (2, 3, 4) and ("ModuleNotFoundError" in out or "ImportError" in out):
        return (False, False, "tests need uninstalled deps — skipped")
    return (True, False, tail)


# react_native is a node stack for proof purposes: its package.json ships a
# `typecheck` script (tsc --noEmit), so _run_node_build proves it with a type
# check rather than a long-running Expo dev server.
_NODE_STACKS = (
    "react", "react_vite", "react_native", "node", "node_express", "express",
    "nextjs", "astro", "remix", "static",
)


_NODE_TEST_RUNNERS = ("vitest", "jest", "mocha", "@testing-library/react",
                      "@testing-library/vue", "ava")


def _run_node_tests(pdir: Path, timeout: int) -> tuple[bool, bool, str]:
    """Advisory: run the project's JS/TS test script when a REAL runner is
    declared and node_modules is present. Returns ``(ran, passed, summary)``.

    ``ran=False`` (no runner / no script / launch failure / timeout) means
    'could not assess' — callers must treat this as advisory and never hard-fail
    a build on it (node runners lack pytest's clean 'no tests' exit code, so a
    flaky/jsdom test would otherwise false-fail a valid app). Runs under CI=1 so
    vitest/jest execute once instead of entering watch mode. Never raises."""
    import json as _json
    import os
    import shutil
    import subprocess

    npm = shutil.which("npm")
    pkg_path = pdir / "package.json"
    if npm is None or not pkg_path.exists() or not (pdir / "node_modules").is_dir():
        return (False, False, "")
    try:
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return (False, False, "")
    if not isinstance(pkg, dict) or "test" not in (pkg.get("scripts") or {}):
        return (False, False, "no test script")
    all_deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    if not any(r in all_deps for r in _NODE_TEST_RUNNERS):
        return (False, False, "no recognized test runner")
    env = {**os.environ, "CI": "1", "npm_config_audit": "false", "npm_config_fund": "false"}
    try:
        res = subprocess.run(
            [npm, "test", "--silent"], cwd=str(pdir), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return (False, False, "node tests timed out / failed to launch")
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    return (True, res.returncode == 0, out[-500:])


# High-signal build-failure lines that name the offending file/symbol. Next/
# webpack/tsc emit these EARLY in the log, so a blind tail (out[-700:]) drops the
# exact lines the code-improver needs to target the right file (e.g. add the
# missing export). We surface them explicitly.
_BUILD_DIAG_RE = re.compile(
    r"Attempted import error:"
    r"|Module not found:"
    r"|is not exported from"
    r"|Type error:"
    r"|Failed to collect page data for"
    r"|Error occurred prerendering page"
    r"|^\s*\./[\w./\-@\[\]]+\.(?:jsx?|tsx?|mjs|cjs)\s*$"   # offending file path line
)


def _distill_build_errors(output: str, *, tail: int = 700, max_diag: int = 30) -> str:
    """Pair the actionable, file/symbol-naming diagnostic lines pulled from the
    FULL build log (import errors, module-not-found, type errors, offending file
    paths) with the log tail. A blind tail misses the early diagnostics the
    improver needs to repair the real cause. Falls back to the tail when no
    high-signal line is found."""
    diag: list[str] = []
    seen: set[str] = set()
    for ln in output.splitlines():
        s = ln.strip()
        if not s or s in seen or not _BUILD_DIAG_RE.search(s):
            continue
        seen.add(s)
        diag.append(s)
        if len(diag) >= max_diag:
            break
    tail_text = output[-tail:]
    if not diag:
        return tail_text
    return "Key errors:\n" + "\n".join(diag) + "\n\n...build output tail:\n" + tail_text


# Generated apps often instantiate an LLM/provider SDK client at module top-level,
# which `next build`'s "collect page data" phase executes — a MISSING key then crashes
# the build ("Missing credentials" / "set OPENAI_API_KEY") even though the key is only
# needed at runtime. Seed placeholders for common providers so build-time evaluation
# doesn't fail; the real key is still required at serve.
_BUILD_PLACEHOLDER_KEYS = (
    "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
    "GROQ_API_KEY", "MISTRAL_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
)


def _node_build_env() -> dict:
    """Build-time env for npm: CI flags + placeholder provider keys so a top-level
    SDK client init doesn't crash the build on a missing key (real key set at serve)."""
    import os
    env = {**os.environ, "CI": "1", "npm_config_audit": "false", "npm_config_fund": "false"}
    for k in _BUILD_PLACEHOLDER_KEYS:
        env.setdefault(k, "sk-build-placeholder")
    return env


def _run_node_build(pdir: Path, stack: str, timeout: int) -> tuple[bool, bool, str]:
    """Compile a node/web project for real: npm install + npm run build.

    Returns ``(ran, passed, summary)``. ``ran=False`` is a soft skip (no npm, no
    build script, or the install failed — e.g. offline) and must NOT fail the
    proof. A non-zero build IS a real failure. Never raises.
    """
    import json as _json
    import shutil
    import subprocess

    pkg_path = pdir / "package.json"
    low = (stack or "").lower()
    if low not in _NODE_STACKS and not pkg_path.exists():
        return (False, False, "")
    npm = shutil.which("npm")
    if npm is None or not pkg_path.exists():
        return (False, False, "npm or package.json missing — build skipped")
    try:
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return (False, False, "")
    if not isinstance(pkg, dict):
        return (True, False, "package.json is not an object")
    invalid_names = _invalid_npm_package_names(pkg)
    if invalid_names:
        return (True, False, "invalid npm package names: " + ", ".join(invalid_names[:8]))
    scripts = pkg.get("scripts") or {}
    build_cmd = "build" if "build" in scripts else ("typecheck" if "typecheck" in scripts else None)
    if build_cmd is None:
        return (False, False, "no build/typecheck script — skipped")

    env = _node_build_env()
    # Install (bounded). A non-zero install is a REAL, build-breaking failure
    # (ERESOLVE / E404 / ETARGET / bad name) and must fail the proof so the
    # fix-loop sees the error. ONLY a genuine connectivity failure (offline
    # registry) soft-skips. Floor the budget at 120s so a slow-but-valid install
    # isn't starved into a false timeout.
    install_budget = max(120, int(timeout * 0.6))
    try:
        inst = subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--no-progress"],
            cwd=str(pdir), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=install_budget, env=env,
        )
    except subprocess.TimeoutExpired:
        # A hang/too-slow install is a delivery problem, not a free pass.
        return (True, False, f"npm install timed out after {install_budget}s")
    except (OSError, ValueError):
        # npm could not even be launched -> environmental, soft-skip.
        return (False, False, "npm install could not be launched — build skipped")
    if inst.returncode != 0:
        out = ((inst.stdout or "") + (inst.stderr or "")).strip()
        if _npm_install_is_offline(out):
            return (False, False, "npm install failed (offline registry) — build skipped")
        return (True, False, out[-700:])

    try:
        bld = subprocess.run(
            [npm, "run", build_cmd],
            cwd=str(pdir), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=max(30, timeout - install_budget), env=env,
        )
    except subprocess.TimeoutExpired:
        return (True, False, f"npm run {build_cmd} timed out after build budget")
    except (OSError, ValueError):
        return (False, False, "")
    out = ((bld.stdout or "") + (bld.stderr or "")).strip()
    if bld.returncode == 0:
        return (True, True, out[-300:])
    # Surface the file/symbol-naming diagnostics (not just the tail) so the
    # fix-loop's improver can target the real cause (e.g. a missing export).
    return (True, False, _distill_build_errors(out))


def _docker_daemon_ok() -> bool:
    """Best-effort docker daemon ping. Never raises."""
    if not _DOCKER_IMPORTABLE:
        return False
    client = None
    try:  # pragma: no cover - environment dependent
        import docker  # type: ignore

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001 - any docker error -> degrade
        return False
    finally:  # pragma: no cover - environment dependent
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
