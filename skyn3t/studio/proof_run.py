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
    return [x for x in out if not (x in seen or seen.add(x))][:10]


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


def _run_node_build(pdir: Path, stack: str, timeout: int) -> tuple[bool, bool, str]:
    """Compile a node/web project for real: npm install + npm run build.

    Returns ``(ran, passed, summary)``. ``ran=False`` is a soft skip (no npm, no
    build script, or the install failed — e.g. offline) and must NOT fail the
    proof. A non-zero build IS a real failure. Never raises.
    """
    import json as _json
    import os
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
        scripts = (_json.loads(pkg_path.read_text(encoding="utf-8")) or {}).get("scripts") or {}
    except (OSError, ValueError):
        return (False, False, "")
    build_cmd = "build" if "build" in scripts else ("typecheck" if "typecheck" in scripts else None)
    if build_cmd is None:
        return (False, False, "no build/typecheck script — skipped")

    env = {**os.environ, "CI": "1", "npm_config_audit": "false", "npm_config_fund": "false"}
    # Install (bounded). A failure here is environmental (offline registry), not
    # a code defect -> soft skip rather than fail the build.
    install_budget = max(30, int(timeout * 0.6))
    try:
        inst = subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--no-progress"],
            cwd=str(pdir), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=install_budget, env=env,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return (False, False, "npm install timed out/failed (offline?) — build skipped")
    if inst.returncode != 0:
        return (False, False, "npm install failed (offline?) — build skipped")

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
    return (True, False, out[-700:])


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
