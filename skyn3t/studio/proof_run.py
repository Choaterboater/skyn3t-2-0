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
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from skyn3t.npm_utils import (
    discard_foreign_node_modules,
    mark_npm_build_current,
    mark_npm_install_current,
    npm_build_current,
    npm_docker_install_stamp_path,
    npm_env,
    npm_install_args,
    npm_install_current,
    npm_install_fingerprint,
)
from skyn3t.persisted_write import (
    PERSISTED_WRITE_RECEIPT_MAX_BYTES,
    is_persisted_write_receipt_body,
)

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
_SOURCE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".go", ".rs",
    ".java", ".astro", ".vue", ".svelte", ".swift",
)
_MIN_SUBSTANTIVE_BYTES = 16

# --- static boot-readiness: relative-import resolution -----------------------
# A `./x` / `../x` import that resolves to no file is a guaranteed bundler boot
# failure (Vite/webpack 500: "Failed to resolve import"). Bare ("react") and
# aliased ("@/x") specifiers are NOT checked — only relative paths, which must
# exist on disk. This is the offline, deterministic gate that catches the
# missing-stylesheet / unwired-module class of "looks done but won't boot".
_JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_RELATIVE_IMPORT_SOURCE_SUFFIXES = (*_JS_SUFFIXES, ".astro")
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
    """Relative imports in JS/TS/Astro files that resolve to no file."""
    out: list[str] = []
    for f in _iter_files(root):
        if f.suffix not in _RELATIVE_IMPORT_SOURCE_SUFFIXES:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for spec in _REL_IMPORT_RE.findall(text):
            if not _import_resolves(f, spec, root):
                out.append(f"{f.relative_to(root)} -> {spec}")
    return out


# A browser treats an ESM named import as a hard module-instantiation contract:
# `import { bootstrap } from './js/main.js'` cannot consume
# `export default { bootstrap }`.  A static-site build often has no compile step,
# so that defect otherwise survives objective proof and appears only as a blank
# page in the browser.  Keep this check deliberately narrow: local ESM modules,
# explicit named imports, and export forms whose surface we can enumerate with
# confidence.  Re-export stars or unfamiliar/dynamic module shapes degrade open.
_ESM_NAMED_IMPORT_RE = re.compile(
    r"^[ \t]*import[ \t]+(?!type\b)"
    r"(?:[A-Za-z_$][\w$]*[ \t]*,[ \t]*)?"
    r"\{(?P<named>[^}]*)\}[ \t\r\n]*"
    r"from[ \t\r\n]*['\"](?P<spec>\.\.?/[^'\"]+)['\"]",
    re.MULTILINE,
)
_ESM_EXPORT_LIST_RE = re.compile(
    r"^[ \t]*export[ \t]+(?:type[ \t]+)?\{(?P<named>[^}]*)\}",
    re.MULTILINE,
)
_ESM_EXPORT_DECL_RE = re.compile(
    r"^[ \t]*export[ \t]+(?:declare[ \t]+)?(?:async[ \t]+)?"
    r"(?:function|class|interface|type|enum|namespace)[ \t]+"
    r"(?P<name>[A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_ESM_EXPORT_VAR_RE = re.compile(
    r"^[ \t]*export[ \t]+(?:declare[ \t]+)?(?:const|let|var)[ \t]+"
    r"(?P<body>[^;\r\n]+)",
    re.MULTILINE,
)
_ESM_EXPORT_STAR_RE = re.compile(
    r"^[ \t]*export[ \t]+\*(?![ \t]+as\b)[ \t]+from\b",
    re.MULTILINE,
)
_ESM_EXPORT_STAR_AS_RE = re.compile(
    r"^[ \t]*export[ \t]+\*[ \t]+as[ \t]+(?P<name>[A-Za-z_$][\w$]*)"
    r"[ \t]+from\b",
    re.MULTILINE,
)
_ESM_EXPORT_DEFAULT_RE = re.compile(
    r"^[ \t]*export[ \t]+default\b",
    re.MULTILINE,
)


def _mask_js_non_code(text: str, *, preserve_strings: bool) -> str:
    """Mask comments/templates, and optionally quoted strings, preserving shape.

    Regexes can then stay line-anchored without treating a block comment or a
    multi-line template literal as authored module syntax.  Import parsing keeps
    quoted strings for the module specifier; export parsing does not need them.
    This is a scanner, not a JavaScript parser, and intentionally degrades toward
    missed findings instead of guessing across unfamiliar syntax.
    """
    chars = list(text)
    out = list(text)
    state = "code"
    escaped = False
    i = 0
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                out[i] = out[i + 1] = " "
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                out[i] = out[i + 1] = " "
                state = "block_comment"
                i += 2
                continue
            if ch == "`":
                out[i] = " "
                state = "template"
                escaped = False
            elif ch == "'":
                if not preserve_strings:
                    out[i] = " "
                state = "single"
                escaped = False
            elif ch == '"':
                if not preserve_strings:
                    out[i] = " "
                state = "double"
                escaped = False
        elif state == "line_comment":
            if ch in "\r\n":
                state = "code"
            else:
                out[i] = " "
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                state = "code"
                i += 2
                continue
            if ch not in "\r\n":
                out[i] = " "
        elif state == "template":
            if ch not in "\r\n":
                out[i] = " "
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "`":
                state = "code"
        else:  # single/double quoted string
            if not preserve_strings and ch not in "\r\n":
                out[i] = " "
            quote = "'" if state == "single" else '"'
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                state = "code"
            elif ch in "\r\n":
                # Invalid/unescaped multiline strings must not hide all later
                # source from a best-effort proof scan.
                state = "code"
        i += 1
    return "".join(out)


def _binding_names(raw: str, *, imported: bool) -> set[str]:
    """Names from an ESM `{ a, b as c }` list.

    Imports contract against the source name (left side); exports publish the
    destination name (right side).  Unsupported string-named bindings are
    skipped rather than guessed.
    """
    out: set[str] = set()
    for item in raw.split(","):
        clean = re.sub(r"\s+", " ", item).strip()
        if clean.startswith("type "):
            clean = clean[5:].strip()
        if not clean:
            continue
        parts = re.split(r"\s+as\s+", clean, maxsplit=1)
        candidate = parts[0] if imported or len(parts) == 1 else parts[1]
        if re.fullmatch(r"[A-Za-z_$][\w$]*", candidate):
            out.add(candidate)
    return out


def _esm_export_surface(text: str) -> tuple[set[str], bool]:
    """Return (explicit named exports, indeterminate surface)."""
    code = _mask_js_non_code(text, preserve_strings=False)
    names: set[str] = set()
    spans: list[tuple[int, int]] = []

    for pattern in (
        _ESM_EXPORT_LIST_RE,
        _ESM_EXPORT_DECL_RE,
        _ESM_EXPORT_VAR_RE,
        _ESM_EXPORT_STAR_RE,
        _ESM_EXPORT_STAR_AS_RE,
        _ESM_EXPORT_DEFAULT_RE,
    ):
        spans.extend(match.span() for match in pattern.finditer(code))

    for match in _ESM_EXPORT_LIST_RE.finditer(code):
        names.update(_binding_names(match.group("named"), imported=False))
    for match in _ESM_EXPORT_DECL_RE.finditer(code):
        names.add(match.group("name"))
    for match in _ESM_EXPORT_STAR_AS_RE.finditer(code):
        names.add(match.group("name"))
    if _ESM_EXPORT_DEFAULT_RE.search(code):
        # `import { default as X }` is a legal (if uncommon) spelling of a
        # default import and must satisfy the same contract.
        names.add("default")

    indeterminate = bool(_ESM_EXPORT_STAR_RE.search(code))
    for match in _ESM_EXPORT_VAR_RE.finditer(code):
        body = match.group("body").strip()
        declared = re.match(r"([A-Za-z_$][\w$]*)\b", body)
        if declared and "," not in body:
            names.add(declared.group(1))
        else:
            # Destructuring and multi-declarator exports are legal but require a
            # parser to enumerate safely.  Do not turn them into a false failure.
            indeterminate = True

    # Unknown export syntax (for example TypeScript's `export =`) makes the
    # surface non-enumerable to this intentionally small scanner.
    for keyword in re.finditer(r"\bexport\b", code):
        if not any(start <= keyword.start() < end for start, end in spans):
            indeterminate = True
            break
    if re.search(r"\b(?:module\.exports|exports\s*\.)", code):
        indeterminate = True
    return names, indeterminate


def _esm_named_export_mismatches(root: Path) -> list[dict[str, Any]]:
    """Definite local ESM named-import/export contract violations.

    Each finding carries both the raw specifier and its canonical project-relative
    target so the fix loop edits the module that owns the missing export instead
    of guessing from a basename.
    """
    project = root.resolve()
    findings: list[dict[str, Any]] = []
    importer_suffixes = frozenset((*_JS_SUFFIXES, ".astro", ".vue", ".svelte"))
    for importer in _iter_files(project):
        if importer.suffix.lower() not in importer_suffixes:
            continue
        try:
            imports = _mask_js_non_code(
                importer.read_text(encoding="utf-8", errors="replace"),
                preserve_strings=True,
            )
            importer_rel = importer.relative_to(project).as_posix()
        except (OSError, ValueError):
            continue
        for match in _ESM_NAMED_IMPORT_RE.finditer(imports):
            requested = _binding_names(match.group("named"), imported=True)
            if not requested:
                continue
            spec = match.group("spec")
            target = _resolve_import_file(importer, spec)
            if target is None or target.suffix.lower() not in _JS_SUFFIXES:
                continue
            try:
                target = target.resolve()
                if _confine(project, target) != target or target.is_symlink():
                    continue
                target_rel = target.relative_to(project).as_posix()
                exported, indeterminate = _esm_export_surface(
                    target.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, ValueError):
                continue
            if indeterminate:
                continue
            missing = sorted(requested - exported)
            if missing:
                findings.append({
                    "importer": importer_rel,
                    "specifier": spec,
                    "module": target_rel,
                    "missing": missing,
                })
    return findings


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


# Stacks whose codegen idiomatically uses JSX/React components. A missing-import
# stub for any OTHER stack (phaser, express, python web, etc.) must not inject a
# bogus React dependency — real bug: a Phaser build's stub for a missing
# './PreloadScene.js' import still failed downstream (a Scene expects to be a
# class, not a React component) even after the filename was fixed. Empty stack
# ("" — existing callers that predate the `stack` kwarg) keeps the original
# React-shaped behavior for backward compatibility.
_REACT_LIKE_STACKS = frozenset({"react", "react_vite", "nextjs", "remix", "react_native", "tauri"})


def _make_import_stub(
    spec: str, default: str | None, ns: str | None, named: list[str], *, stack: str = "",
) -> str:
    """A minimal but valid JS/TS module satisfying the requested imports. For a
    react-like stack (or no stack specified — preserves prior behavior for
    existing callers): components (PascalCase) render a sensible semantic
    element forwarding props + children via React.createElement (no JSX, so the
    file is valid as .js/.jsx/.ts/.tsx); hooks (useX) return {}. For every other
    stack: components become an empty exported class (a safe generic stand-in
    for a Scene/service/anything PascalCase, without assuming a UI framework);
    utilities are unchanged (a no-op function/const)."""
    react_shaped = (not stack) or (stack.lower() in _REACT_LIKE_STACKS)
    last = spec.rstrip("/").rsplit("/", 1)[-1].lower()
    default_el = _HTML_FOR.get(last, "div")
    header = (
        ["// Auto-scaffolded by SkyN3t: the imported module was missing, so this",
         "// minimal stub lets the build resolve. Replace with a real implementation.",
         "import React from 'react';", ""]
        if react_shaped else
        ["// Auto-scaffolded by SkyN3t: the imported module was missing, so this",
         "// minimal stub lets the build resolve. Replace with a real implementation.", ""]
    )
    lines = list(header)

    def component(name: str, el: str) -> str:
        if react_shaped:
            return (f"export function {name}(props) {{\n"
                    f"  const {{ children, ...rest }} = props || {{}};\n"
                    f"  return React.createElement('{el}', rest, children);\n"
                    f"}}")
        return f"export class {name} {{}}"

    def utility(name: str) -> str:
        if name.startswith("use") and name[3:4].isupper():
            return f"export function {name}() {{ return {{}}; }}"
        return f"export const {name} = (...args) => '';"

    if default:
        if react_shaped:
            lines.append(f"function {default}(props) {{\n"
                         f"  const {{ children, ...rest }} = props || {{}};\n"
                         f"  return React.createElement('{default_el}', rest, children);\n}}")
            lines.append(f"export default {default};")
        else:
            lines.append(f"class {default} {{}}")
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


_STYLESHEET_EXTS = (".css", ".scss", ".sass", ".less")
_STYLESHEET_STUB_CONTENT = "/* stub stylesheet — import target was missing */\n"


def _confine(root: Path, target: Path) -> Path | None:
    """Resolve `target` and return it ONLY if confined within `root` — never
    write a stub OUTSIDE the project root for an escaping '../../x' relative
    import (leave it correctly flagged as unresolved/no_go instead). A missing
    confinement check here is a real path-traversal / arbitrary-file-write bug,
    not just a correctness nicety. Returns None for anything that escapes, or
    on any resolution error."""
    try:
        resolved = target.resolve()
        root_resolved = root.resolve()
        if os.path.commonpath([str(root_resolved), str(resolved)]) != str(root_resolved):
            return None
        return resolved
    except (OSError, ValueError):
        return None


def relink_unresolved_relative_imports(root: str | Path) -> list[str]:
    """Relink uniquely identifiable relative imports that point at the wrong depth.

    Cheap codegen commonly writes an import as if a nested route lived one directory
    higher, for example ``src/pages/drills/index.astro`` importing
    ``../layouts/BaseLayout.astro``. Before creating a stub at that incorrect path,
    look for exactly one existing project file with the requested basename and
    rewrite only the import specifier. Ambiguous names, valid imports, symlinks, and
    paths whose original target escapes the project are left untouched.

    Returns stable evidence strings in ``importer: old -> new`` form.
    """
    project = Path(root).resolve()
    target_suffixes = frozenset((*_RESOLVE_EXTS, ".astro"))
    by_name: dict[str, list[Path]] = {}
    by_stem: dict[str, list[Path]] = {}

    for candidate in _iter_files(project):
        try:
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.suffix not in target_suffixes
            ):
                continue
            resolved = candidate.resolve()
        except OSError:
            continue
        if _confine(project, resolved) != resolved:
            continue
        by_name.setdefault(candidate.name, []).append(resolved)
        by_stem.setdefault(candidate.stem, []).append(resolved)

    evidence: list[str] = []
    for importer in _iter_files(project):
        if importer.suffix not in _RELATIVE_IMPORT_SOURCE_SUFFIXES or importer.is_symlink():
            continue
        try:
            original = importer.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        changes: list[tuple[str, str]] = []

        def _replacement(
            match: re.Match[str],
            *,
            _importer: Path = importer,
            _changes: list[tuple[str, str]] = changes,
        ) -> str:
            spec = match.group(1)
            if _import_resolves(_importer, spec, project):
                return match.group(0)

            cut = len(spec)
            for marker in ("?", "#"):
                marker_at = spec.find(marker)
                if marker_at >= 0:
                    cut = min(cut, marker_at)
            import_path, trailer = spec[:cut], spec[cut:]
            normalized = import_path.replace("\\", "/")
            basename = normalized.rsplit("/", 1)[-1]
            if not basename or basename in {".", ".."}:
                return match.group(0)

            # Refuse to reinterpret an import that itself targets outside the
            # project, even when a same-named file happens to exist inside it.
            if _confine(project, _importer.parent / normalized) is None:
                return match.group(0)

            explicit_suffix = Path(basename).suffix
            if explicit_suffix:
                if explicit_suffix not in target_suffixes:
                    return match.group(0)
                matches = by_name.get(basename, [])
            else:
                matches = by_stem.get(basename, [])
            unique = sorted(set(matches), key=lambda path: path.as_posix())
            if len(unique) != 1 or unique[0] == _importer.resolve():
                return match.group(0)

            relinked = os.path.relpath(unique[0], _importer.parent).replace("\\", "/")
            if not explicit_suffix and unique[0].suffix:
                relinked = relinked[: -len(unique[0].suffix)]
            if not relinked.startswith("."):
                relinked = "./" + relinked
            new_spec = relinked + trailer
            if new_spec == spec:
                return match.group(0)

            _changes.append((spec, new_spec))
            start, end = match.span(1)
            offset = match.start()
            return (
                match.group(0)[: start - offset]
                + new_spec
                + match.group(0)[end - offset :]
            )

        rewritten = _REL_IMPORT_RE.sub(_replacement, original)
        if rewritten == original:
            continue
        try:
            importer.write_text(rewritten, encoding="utf-8")
            importer_rel = importer.relative_to(project).as_posix()
        except (OSError, ValueError):
            continue
        for old, new in dict.fromkeys(changes):
            evidence.append(f"{importer_rel}: {old} -> {new}")
    return evidence


def scaffold_missing_imports(root: str | Path, *, stack: str = "") -> list[str]:
    """Generate minimal stubs for LOCAL imports (relative or '@/' aliased) whose
    target file does not exist — the recurring 'codegen imports a component it
    never created' break (e.g. '@/components/ui/button' -> Module not found). The
    stub renders real markup so the app builds AND runs; a genuinely broken stub
    is still caught downstream by the boot/liveness gate. Returns paths written.

    Two detection passes over each file: `_IMPORT_NAMES_RE` (named/default/
    namespace bindings — informs component-vs-utility stub CONTENT) and
    `_REL_IMPORT_RE` (the permissive superset proof_run's OWN unresolved-import
    detector uses — also catches a BARE side-effect import like
    `import './app.css';`, which has no `from` clause and `_IMPORT_NAMES_RE`
    structurally cannot match). Without the second pass, a proof could flag an
    import as unresolved that this repair could never see, let alone fix — the
    exact gap that used to require a separate, narrower `_stub_dangling_
    stylesheets` mechanism working off the proof's own detector instead."""
    # Resolved once, up front: every stub path is confinement-checked (and thus
    # resolved) against this same canonical root before being written or
    # relativized, so `.relative_to(root)` below can't mismatch on a symlink or
    # a relative/absolute inconsistency.
    root = Path(root).resolve()
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
        handled_specs: set[str] = set()
        for m in _IMPORT_NAMES_RE.finditer(text):
            spec = m.group("spec")
            handled_specs.add(spec)
            base = _local_import_base(f, spec, aliases)
            if base is None or _base_resolves(base):
                continue
            named = [n for n in (m.group("named") or "").split(",") if n.strip()]
            if base.suffix and base.suffix in _RESOLVE_EXTS:
                stub = base
            else:
                comp = bool(m.group("default")) or any(n.strip()[:1].isupper() for n in named)
                ext = (".tsx" if ts else ".jsx") if comp else (".ts" if ts else ".js")
                stub = base.with_name(base.name + ext)
            confined_stub = _confine(root, stub)
            if confined_stub is None or confined_stub in planned or confined_stub.exists():
                continue
            stub = confined_stub
            if stub.suffix in _STYLESHEET_EXTS:
                content = _STYLESHEET_STUB_CONTENT
            else:
                content = _make_import_stub(
                    spec, m.group("default"), m.group("ns"), named, stack=stack)
            try:
                stub.parent.mkdir(parents=True, exist_ok=True)
                stub.write_text(content, encoding="utf-8")
                planned.add(stub)
                written.append(stub.relative_to(root).as_posix())
            except OSError:
                continue
        # Bare/side-effect imports (`import './x.css';`, no `from`) — invisible
        # to the pass above. No named/default/ns info exists for these by
        # construction, so the stub is always the generic "side-effect import"
        # shape (or the stylesheet shape, when the extension calls for it).
        for spec in _REL_IMPORT_RE.findall(text):
            if spec in handled_specs:
                continue
            handled_specs.add(spec)
            base = _local_import_base(f, spec, aliases)
            if base is None or _base_resolves(base):
                continue
            if base.suffix and base.suffix in _RESOLVE_EXTS:
                stub = base
            else:
                stub = base.with_name(base.name + (".ts" if ts else ".js"))
            confined_stub = _confine(root, stub)
            if confined_stub is None or confined_stub in planned or confined_stub.exists():
                continue
            stub = confined_stub
            content = (_STYLESHEET_STUB_CONTENT if stub.suffix in _STYLESHEET_EXTS
                      else _make_import_stub(spec, None, None, [], stack=stack))
            try:
                stub.parent.mkdir(parents=True, exist_ok=True)
                stub.write_text(content, encoding="utf-8")
                planned.add(stub)
                written.append(stub.relative_to(root).as_posix())
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
    """Resolved file paths reachable from the entry files via relative imports.

    Entries come from two sources, and BOTH are needed. ``_ENTRY_NAMES`` covers
    bundler conventions (main.jsx, App.tsx, Next.js page/layout). A plain static
    site has none of those: its entry is whatever ``index.html`` loads via
    ``<script src>``, commonly ``assets/js/main.js``. Without HTML seeding the
    graph starts from nothing, so every component is unreachable and
    :func:`_unwired_components` reports a correctly-wired static app as an
    unwired stub. Additive — this can only widen reachability, never narrow it.
    """
    entries = [
        f.resolve() for f in _iter_files(root)
        if f.name in _ENTRY_NAMES and f.suffix in _JS_SUFFIXES
    ]
    for html in _iter_files(root):
        if html.suffix.lower() not in (".html", ".htm"):
            continue
        try:
            markup = html.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ref in _HTML_LOCAL_REF_RE.findall(markup):
            target = ref.strip()
            if not target or target.startswith(
                ("http://", "https://", "//", "data:", "mailto:")
            ):
                continue
            candidate = (
                (root / target.lstrip("/")) if target.startswith("/")
                else (html.parent / target)
            ).resolve()
            if candidate.suffix in _JS_SUFFIXES and candidate.is_file():
                entries.append(candidate)
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


_HTML_LOCAL_REF_RE = re.compile(
    r"""<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"'#?]+)["']""",
    re.IGNORECASE,
)


def _dangling_html_refs(root: Path) -> list[str]:
    """Local ``<script src>`` / ``<link href>`` targets that do not exist.

    A guaranteed runtime 404 on every page load, and — unlike the "unwired
    components" heuristic below — an unambiguous, mechanically repairable
    defect: either write the file or drop the tag. Kept separate precisely
    because the two were previously conflated, and the resulting gap string
    ("wire up your components") pointed the fix loop at the opposite repair
    from the one needed.

    Only same-project relative targets are considered; absolute URLs, protocol-
    relative URLs, data: URIs and root-absolute paths (which a dev server may
    map elsewhere) are all skipped. Pure and offline.
    """
    out: list[str] = []
    for f in _iter_files(root):
        if f.suffix.lower() not in (".html", ".htm"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_html = str(f.relative_to(root)).replace("\\", "/")
        for ref in _HTML_LOCAL_REF_RE.findall(text):
            target = ref.strip()
            if not target or target.startswith(("http://", "https://", "//", "data:", "/", "mailto:")):
                continue
            candidate = (f.parent / target).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue  # escapes the project; not ours to judge
            if not candidate.is_file():
                out.append(f"{rel_html} -> {target}")
            if len(out) >= 20:
                return out
    return out


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
    if not reachable:
        # No entry was found AT ALL, so the analysis did not run — this is
        # "could not determine", not "nothing is wired". Reporting it as the
        # latter accused two real deliveries: a static site whose entry is
        # `<script src>` in HTML, and a static-site GENERATOR whose graph root
        # is `scripts/build.mjs`, referenced only from package.json "scripts".
        # Neither matches a bundler entry name, so the walk started from an
        # empty set and every component was trivially "orphaned".
        # This is gate_posture rule 1 (a gate that could not run never blocks)
        # applied inside the check, so it holds in release posture too. It
        # cannot mask the defect this exists for: a stub ENTRY is still an
        # entry, so `reachable` is non-empty whenever that defect is present.
        return None
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
    # Failures that would have flipped ``passed`` under release posture but were
    # demoted to findings under lab posture (failing LLM-authored tests, a ruff
    # style failure, thin checklist coverage). ``detail`` is populated
    # identically either way, so ``error_gaps()`` still feeds the fix loop the
    # same repair strings — advisory means "does not block", never "not
    # repaired". Empty by default, so every existing consumer is unaffected.
    advisory_failures: list[str] = field(default_factory=list)

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
            "advisory_failures": list(self.advisory_failures),
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


@dataclass(slots=True)
class _ProofCommandResult:
    returncode: int
    stdout: str
    stderr: str
    backend: str = "local"
    timed_out: bool = False


@dataclass(slots=True)
class _ProofCommandContext:
    runner: Any | None
    stack: str
    used_backend: str = ""
    docker_available: bool = False
    warnings: list[str] = field(default_factory=list)


_NODE_SANDBOX_STACKS = {
    "react", "react_vite", "vite", "nextjs", "next", "astro", "remix",
    "svelte", "sveltekit", "vue", "nuxt", "solid", "tauri", "desktop",
    "phaser", "node", "node_express", "express",
}

_PYTHON_PROOF_STACKS = {
    "cli", "python", "python_cli", "fastapi", "flask", "django",
    "rag", "workflow", "mcp", "agent_pack",
}


def _sandbox_stack(stack: str) -> str:
    low = (stack or "").lower()
    if low in _NODE_SANDBOX_STACKS:
        return "node"
    if low in ("static", "static_html"):
        return "static"
    if low == "swift":
        # security/sandbox.py has no swift docker image; _resolve_image falls back
        # to the default template for docker, but on a Docker-less host this maps
        # to the hardened local-subprocess path (which runs `swift build` against
        # the machine's toolchain). Naming it explicitly documents the routing.
        return "swift"
    return "python"


def _new_sandbox_runner(execution_backend: str):
    if execution_backend == "inline":
        return None
    try:
        from skyn3t.config.settings import Settings
        from skyn3t.security.sandbox import SandboxRunner

        return SandboxRunner(settings=Settings(execution_backend=execution_backend))
    except Exception:  # noqa: BLE001 - proof can still run deterministic local checks
        return None


def _proof_command_context(execution_backend: str, stack: str) -> _ProofCommandContext:
    return _ProofCommandContext(
        runner=_new_sandbox_runner(execution_backend),
        stack=_sandbox_stack(stack),
    )


def _sandbox_available(ctx: _ProofCommandContext, execution_backend: str) -> bool:
    if execution_backend not in ("docker", "auto"):
        return False
    if _DOCKER_IMPORTABLE and _docker_daemon_ok():
        return True
    runner = ctx.runner
    if runner is not None and hasattr(runner, "docker_available"):
        try:
            return bool(runner.docker_available())
        except Exception:  # noqa: BLE001
            return False
    return False


def _run_proof_command(
    ctx: _ProofCommandContext | None,
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    network: bool = False,
) -> _ProofCommandResult:
    """Run a proof command through SandboxRunner when configured, else locally."""
    runner = ctx.runner if ctx is not None else None
    from skyn3t.security.secrets import filter_env

    safe_env = filter_env(env)
    if runner is not None:
        import asyncio

        async def _run_sandbox() -> Any:
            return await runner.run(
                command,
                cwd=cwd,
                timeout=timeout,
                stack=ctx.stack if ctx is not None else None,
                env=safe_env,
                network=network,
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop_running = False
        else:
            loop_running = True
        if loop_running:
            import concurrent.futures

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    res = ex.submit(lambda: asyncio.run(_run_sandbox())).result(
                        timeout=timeout + 5
                    )
            except Exception as exc:  # noqa: BLE001 - proof commands report failure
                return _ProofCommandResult(
                    127, "", f"sandbox exec failed: {exc}", backend="sandbox"
                )
        else:
            try:
                res = asyncio.run(_run_sandbox())
            except Exception as exc:  # noqa: BLE001 - proof commands report failure
                return _ProofCommandResult(127, "", f"sandbox exec failed: {exc}", backend="sandbox")
        backend = str(getattr(res, "backend", "") or "")
        if ctx is not None and backend:
            ctx.used_backend = backend
        warning = getattr(res, "warning", None)
        if ctx is not None and warning:
            ctx.warnings.append(str(warning))
        return _ProofCommandResult(
            returncode=int(getattr(res, "exit_code", 1)),
            stdout=str(getattr(res, "stdout", "") or ""),
            stderr=str(getattr(res, "stderr", "") or ""),
            backend=backend or "sandbox",
            timed_out=bool(getattr(res, "timed_out", False)),
        )

    import subprocess

    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=safe_env,
        )
        return _ProofCommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return _ProofCommandResult(124, stdout, stderr, timed_out=True)
    except (OSError, ValueError) as exc:
        return _ProofCommandResult(127, "", f"exec failed: {exc}")


def _use_container_command_names(ctx: _ProofCommandContext | None) -> bool:
    if ctx is None or ctx.runner is None:
        return False
    runner = ctx.runner
    if not hasattr(runner, "docker_available"):
        return True
    backend = str(getattr(getattr(runner, "settings", None), "execution_backend", "") or "")
    return bool(ctx.docker_available or backend == "docker")


def _safe_requirements_file(pdir: Path) -> Path | None:
    req = pdir / "requirements.txt"
    if not req.is_file():
        return None
    try:
        lines = req.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    specs = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if (
            line.startswith(("-", "git+", "http://", "https://", "file:"))
            or "/" in line
            or "\\" in line
        ):
            return None
        specs.append(line)
    if not specs or len(specs) > 24:
        return None
    return req


def _safe_requirement_names(pdir: Path) -> list[str]:
    req = _safe_requirements_file(pdir)
    if req is None:
        return []
    names: list[str] = []
    try:
        lines = req.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~;\[]", line, maxsplit=1)[0].strip()
        if name:
            names.append(name.replace("-", "_").lower())
    return names


def _python_requirements_importable(pdir: Path) -> bool:
    names = _safe_requirement_names(pdir)
    if not names:
        return False
    import importlib.util

    common = {"python_dotenv": "dotenv", "beautifulsoup4": "bs4", "pillow": "PIL"}
    for name in names:
        mod = common.get(name, name).split(".", 1)[0]
        if importlib.util.find_spec(mod) is None:
            return False
    return True


def _install_python_deps(
    pdir: Path,
    cmd_ctx: _ProofCommandContext | None,
    *,
    timeout: int,
) -> tuple[bool, bool, str]:
    """Install a small generated Python dependency manifest for proof commands.

    Returns ``(ran, ok, summary)``. Unsupported or unsafe manifests are a soft
    skip, not a proof failure. The command uses the same sandbox/filtered-env
    path as boot/tests and gets only bounded network access.
    """
    req = _safe_requirements_file(pdir)
    if req is None:
        return (False, False, "no safe requirements.txt to install")
    import os

    py = _python_executable()
    use_container_names = _use_container_command_names(cmd_ctx)
    if py is None and not use_container_names:
        return (False, False, "no python interpreter available")
    py_cmd = "python" if use_container_names else str(py)
    budget = max(30, min(120, int(timeout)))
    proc = _run_proof_command(
        cmd_ctx,
        [py_cmd, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(req)],
        cwd=pdir,
        timeout=budget,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        network=True,
    )
    if proc.timed_out:
        return (True, False, f"pip install timed out after {budget}s")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        return (True, True, out[-500:] or "pip install ok")
    if proc.returncode == 127:
        return (False, False, "pip could not be launched")
    return (True, False, out[-500:] or "pip install failed")


def _proof_install_python_deps_default() -> bool:
    try:
        from skyn3t.config.settings import get_settings
        return bool(getattr(get_settings(), "proof_install_python_deps", True))
    except Exception:  # noqa: BLE001
        return True


def _proof_python_deps_timeout_default() -> int:
    try:
        from skyn3t.config.settings import get_settings
        return int(getattr(get_settings(), "proof_python_deps_timeout", 120))
    except Exception:  # noqa: BLE001
        return 120


def _should_install_python_deps(
    pdir: Path,
    stack: str,
    *,
    run_tests: bool,
    run_build: bool,
) -> bool:
    if not (run_tests or run_build):
        return False
    low = (stack or "").lower()
    return low in _PYTHON_PROOF_STACKS or _has_python_tests(pdir)


def _attach_proof_environment(
    detail: dict[str, Any],
    *,
    execution_backend: str,
    cmd_ctx: _ProofCommandContext,
    sandbox_available: bool,
    run_tests: bool,
    run_build: bool,
) -> None:
    reasons: list[str] = []
    if execution_backend in ("auto", "docker") and not sandbox_available:
        reasons.append("docker sandbox unavailable; used hardened local proof commands")
    if run_build and detail.get("build") == "skipped":
        summary = str(detail.get("build_summary") or "").strip()
        # "There is no build step" is not degraded EVIDENCE — it is a complete
        # answer. A static site legitimately has no build/typecheck script, so
        # counting it as degraded capped every such delivery's score for
        # producing exactly the artifact it was asked for. Only a build that
        # could not RUN (missing toolchain, unavailable sandbox) is degradation.
        if "no build" not in summary.lower():
            reasons.append("build skipped" + (f": {summary}" if summary else ""))
    # Web and Swift projects can have a valid native test suite even when the
    # generic Python test probe has nothing to run.  Do not downgrade objective
    # Node/Swift test evidence simply because that unrelated probe soft-skipped.
    native_tests_passed = any(
        detail.get(name) == "passed" for name in ("node_tests", "swift_tests", "swift_ios_tests")
    )
    if run_tests and detail.get("tests") == "skipped" and not native_tests_passed:
        summary = str(detail.get("test_summary") or "").strip()
        reasons.append("tests skipped" + (f": {summary}" if summary else ""))
    if detail.get("python_deps") == "failed":
        summary = str(detail.get("python_deps_summary") or "").strip()
        reasons.append("python dependency install failed" + (f": {summary}" if summary else ""))
    if run_build and detail.get("ruff") == "skipped":
        summary = str(detail.get("ruff_summary") or "").strip()
        reasons.append("Ruff quality check skipped" + (f": {summary}" if summary else ""))
    command_backend = cmd_ctx.used_backend or ("local" if cmd_ctx.runner is None else "not_run")
    detail["proof_environment"] = {
        "execution_backend": execution_backend,
        "command_backend": command_backend,
        "sandbox_available": sandbox_available,
        "degraded": bool(reasons),
        "degraded_reasons": reasons,
    }


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
    # Opted-in Python quality check. The formatter repairs common generated
    # layout defects; anything that remains is a real source problem and should
    # reach the code-improver with Ruff's file/line diagnostics intact.
    if d.get("ruff") == "failed" and d.get("ruff_summary"):
        gaps.append(f"RUFF FAILED — fix these Python quality diagnostics:\n{d['ruff_summary']}")
    # Real entrypoint import traceback (400-char tail from _entrypoint_check).
    if d.get("boot_error"):
        gaps.append(f"BOOT/IMPORT ERROR — fix the entrypoint so it imports cleanly:\n{d['boot_error']}")
    # Unresolved relative imports — each entry is "<importer> -> <spec>".
    for imp in (d.get("unresolved_imports") or []):
        gaps.append(f"UNRESOLVED IMPORT — create the missing target or fix the path: {imp}")
    # Dangling <script src>/<link href> — each entry is "<page> -> <target>".
    # States BOTH valid repairs, because the wrong one is tempting: a model that
    # only sees "missing file" tends to invent a stub module, when the delivered
    # entry often already does the work via another script and the tag is simply
    # dead. Naming both keeps the improver from guessing.
    for ref in (d.get("dangling_html_refs") or []):
        gaps.append(
            "DANGLING HTML REFERENCE — this page references a file that does not "
            f"exist, so it 404s on every load: {ref}. Either write that file with "
            "real content, or delete the <script>/<link> tag if another already "
            "loaded module provides the behaviour."
        )
    # Definite ESM named-import/export contract violations.  Put the resolved
    # module first in a stable sentence shape consumed by CodeImproverAgent, so
    # repair targets never have to infer a relative specifier's true location.
    for mismatch in (d.get("named_export_mismatches") or []):
        if not isinstance(mismatch, dict):
            continue
        module = str(mismatch.get("module") or "").strip()
        importer = str(mismatch.get("importer") or "").strip()
        specifier = str(mismatch.get("specifier") or "").strip()
        missing_names = mismatch.get("missing") or []
        missing = ", ".join(str(name) for name in missing_names if str(name).strip())
        if not module or not importer or not missing:
            continue
        gaps.append(
            f"NAMED EXPORT MISMATCH — module {module} is missing named export(s) "
            f"{missing}; imported by {importer} via {specifier}. Export the requested "
            "binding(s), or update the importer to use the module's actual API."
        )
    # Generated components exist but the entry reaches none of them.
    if d.get("unwired_components"):
        gaps.append(f"UNWIRED ENTRY — wire the entry to render the real app: {d['unwired_components']}")
    # Python syntax errors — each entry is "<file>: <SyntaxError>".
    for se in (syntax_errors or []):
        gaps.append(f"SYNTAX ERROR — fix: {se}")
    # Anti-fake gap producers (never flip the verdict; enrich the fix-loop).
    for pm in (d.get("placeholder_markers") or []):
        gaps.append(
            "PLACEHOLDER MARKER — remove the incomplete-code placeholder and write "
            f"the COMPLETE file: {pm}"
        )
    for rel in (d.get("persisted_write_receipts") or []):
        gaps.append(
            "PERSISTED-WRITE RECEIPT — replace history metadata with the real "
            f"complete file contents: {rel}"
        )
    for feat in (d.get("missing_features") or []):
        gaps.append(
            f"BRIEF FEATURE MISSING — the brief asks for '{feat}' but no trace of it "
            "appears in the delivered code or README; implement it (or the brief's "
            "wording for it)."
        )
    if d.get("scaffold_stub"):
        gaps.append(f"SCAFFOLD STUB — {d['scaffold_stub']}")
    return gaps


# Generated/installed trees that are NOT the app's own source — scanning them
# (e.g. the minified `.next/` build output) scraped pseudo-imports like `${r}`
# into package.json and polluted the import/substance signals.
_NON_SOURCE_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "node_modules", ".next", "dist", "build",
    ".vite", ".astro", "out", "coverage", ".turbo", ".cache", "vendor", ".svelte-kit",
    ".skyn3t-pytest",
    # Swift Package Manager build output / caches — not the app's own source.
    ".build", ".swiftpm",
})


def _iter_files(root: Path):
    """Yield authored files without ever descending into generated trees.

    ``Path.rglob`` filters a path only *after* walking it.  Proof calls this
    helper repeatedly, so a delivered React project's ``node_modules`` tree
    used to be traversed over and over even though every result was discarded.
    Pruning ``dirnames`` at the walk boundary keeps proof work proportional to
    the app's authored files while preserving the same exclusion policy.
    """
    try:
        for current, dirnames, filenames in os.walk(
            root, topdown=True, followlinks=False
        ):
            dirnames[:] = [
                name for name in dirnames if name not in _NON_SOURCE_DIRS
            ]
            current_path = Path(current)
            for name in filenames:
                candidate = current_path / name
                if candidate.is_file():
                    yield candidate
    except OSError:
        return


_RECEIPT_SCAN_IGNORED_DIRS = _NON_SOURCE_DIRS - {"build", "dist", "out"}


def _iter_receipt_scan_files(root: Path):
    """Yield authored and deploy-output files, excluding dependencies/caches."""
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(
            part in _RECEIPT_SCAN_IGNORED_DIRS
            for part in p.relative_to(root).parts
        ):
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


# --- anti-fake gap producers (wave-2 items 44/47/48) -------------------------
# Three deterministic, offline checks that ADD repair hints for the fix-loop.
# House rules: NONE flips ``passed`` on its own, NONE mutates or deletes code.
# proof_run() records their output into ``detail`` and ``extract_error_gaps``
# surfaces them as gap strings the code-improver can act on.

# Source/markup files worth scanning for authored defects (never build output).
_ANTIFAKE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".html", ".css", ".astro", ".swift", ".go", ".rs", ".md",
)
# Code-only suffixes (README/markup excluded) for the originality comparison.
_ANTIFAKE_CODE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".astro", ".swift", ".go", ".rs",
)

# Banned incomplete-code / placeholder idioms (Bolt full-file contract). Matched
# case-insensitively as literal substrings — each is precise enough that a bare
# "TODO" or the word "unchanged" alone never trips it.
_PLACEHOLDER_MARKERS = (
    "rest of code",
    "rest of the code",
    "unchanged */",
    "... existing code",
    "existing code ...",
    "todo: implement",
    "lorem ipsum",
)


def scan_placeholder_markers(root: str | Path) -> list[str]:
    """Flag delivered source that ships an incomplete-code placeholder.

    Returns ``"<relpath>: <marker>"`` strings (deduped, capped). Never mutates or
    deletes anything — gaps only. The elided-file idioms it catches are the
    dangling-import bug class the Bolt full-file contract forbids at prompt time.
    """
    root = Path(root)
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    for f in _iter_files(root):
        if f.suffix.lower() not in _ANTIFAKE_SUFFIXES:
            continue
        try:
            if f.stat().st_size > 500_000:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low = text.lower()
        rel = str(f.relative_to(root)).replace("\\", "/")
        for marker in _PLACEHOLDER_MARKERS:
            if marker in low and (rel, marker) not in seen:
                seen.add((rel, marker))
                out.append(f"{rel}: {marker}")
        if len(out) >= 40:
            break
    return out


def scan_persisted_write_receipts(root: str | Path) -> list[str]:
    """Find receipt-only files left by historical codegen replay corruption."""
    root = Path(root)
    out: list[str] = []
    for f in _iter_receipt_scan_files(root):
        try:
            if f.stat().st_size > PERSISTED_WRITE_RECEIPT_MAX_BYTES:
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if is_persisted_write_receipt_body(body):
            out.append(str(f.relative_to(root)).replace("\\", "/"))
        if len(out) >= 40:
            break
    return out


# Generic scaffolding vocabulary that is never a distinguishing FEATURE — the
# reality-checker stoplist (precision over recall).
_FEATURE_STOPWORDS = frozenset({
    "app", "apps", "application", "website", "webapp", "site", "page", "pages",
    "make", "build", "builds", "create", "creates", "creating", "generate",
    "using", "with", "that", "this", "then", "your", "user", "users",
    "simple", "basic", "clean", "nice", "modern", "cool", "great", "good",
    "feature", "features", "support", "supports", "allow", "allows", "able",
    "data", "list", "lists", "thing", "things", "stuff", "some", "each",
    "should", "would", "will", "into", "from", "have", "which", "where",
    "when", "what", "they", "them", "also", "like", "want", "need", "must",
    "project", "system", "tool", "tools", "platform", "dashboard", "interface",
    "frontend", "backend", "fullstack", "responsive", "beautiful", "minimal",
    "html", "css", "javascript", "typescript", "python", "react", "node",
    "vite", "phaser", "nextjs", "express", "fastapi", "swift", "game",
})


def _feature_tokens(brief: str) -> list[str]:
    """Concrete content words from the brief (deduped, order-preserved, capped).

    Multi-char words only, stoplisted against generic scaffolding vocabulary — so
    only distinguishing feature nouns/verbs remain (precision over recall)."""
    tokens: list[str] = []
    seen: set[str] = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#-]{3,}", (brief or "").lower()):
        if w in _FEATURE_STOPWORDS or w in seen:
            continue
        seen.add(w)
        tokens.append(w)
        if len(tokens) >= 15:
            break
    return tokens


def _feature_present(token: str, corpus: str) -> bool:
    """Is the feature word (or its obvious singular) present in the corpus?"""
    if token in corpus:
        return True
    # Cheap de-pluralization so 'recipes'->'recip(e)', 'priorities'->'priority'.
    for suf in ("ies", "es", "s"):
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            stem = token[: -len(suf)] + ("y" if suf == "ies" else "")
            if stem in corpus:
                return True
    return False


def missing_brief_features(root: str | Path, brief: str) -> list[str]:
    """Brief feature words with NO trace in the delivered source/README.

    Deterministic reality check (agency-agents): the concrete nouns/verbs the
    brief promised should appear SOMEWHERE in what was built. Missing ones are
    strong 'codegen silently dropped a requirement' signals. Precision-first:
    only distinguishing content words, de-pluralized substring match, capped."""
    tokens = _feature_tokens(brief)
    if not tokens:
        return []
    root = Path(root)
    parts: list[str] = []
    for f in _iter_files(root):
        if f.suffix.lower() not in _ANTIFAKE_SUFFIXES:
            continue
        try:
            if f.stat().st_size > 500_000:
                continue
            parts.append(f.read_text(encoding="utf-8", errors="ignore").lower())
        except OSError:
            continue
    corpus = "\n".join(parts)
    if not corpus:
        return []
    missing = [t for t in tokens if not _feature_present(t, corpus)]
    return missing[:8]


def _shingles(text: str, n: int = 8) -> set[tuple[str, ...]]:
    words = re.findall(r"\S+", text)
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _shingle_overlap(a: str, b: str, n: int = 8) -> float:
    """Jaccard overlap of the two texts' n-word shingle sets (0..1)."""
    sa, sb = _shingles(a, n), _shingles(b, n)
    if not sa or not sb:
        return 0.0
    union = len(sa | sb)
    return (len(sa & sb) / union) if union else 0.0


_OFFLINE_STARTER_MARKERS = (
    "generated offline by skyn3t",
    "runnable vite + react starter",
    "runnable next.js",
    "runnable static site",
    "runnable astro site",
    "runnable remix app",
    "runnable expo app",
)
_OFFLINE_PLACEHOLDER_INTERACTIONS = (
    "count is",
    "setcount",
    "click me",
)


def detect_offline_starter_stub(root: str | Path, stack: str = "") -> str | None:
    """Flag the default offline starter UI even when extra files were added.

    The shingle-based scaffold detector is intentionally conservative and skips
    trees with extra files. A common bad delivery keeps the scaffold entrypoint
    (counter button / "Click me" CTA / offline starter copy) while adding a
    settings file or manifest; that still shipped the starter instead of the
    brief.
    """
    root = Path(root)
    try:
        for f in _iter_files(root):
            if f.suffix.lower() not in _ANTIFAKE_SUFFIXES:
                continue
            try:
                if f.stat().st_size > 500_000:
                    continue
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            low = txt.lower()
            if not any(marker in low for marker in _OFFLINE_STARTER_MARKERS):
                continue
            if not any(marker in low for marker in _OFFLINE_PLACEHOLDER_INTERACTIONS):
                continue
            rel = str(f.relative_to(root)).replace("\\", "/")
            label = f" {stack}" if stack else ""
            return (
                f"delivered the offline{label} starter in {rel} "
                "(counter/Click me placeholder) - the build shipped the starter "
                "instead of implementing the brief"
            )
    except OSError:
        return None
    return None


def detect_scaffold_stub(
    root: str | Path, stack: str, brief: str = "", *, threshold: float = 0.7
) -> str | None:
    """Return a gap note when the delivered tree is essentially the pristine
    stack scaffold (the documented 'shipped the stub' class), else None.

    Compares the delivered CODE files against a freshly generated
    ``scaffold_for(stack, ...)`` via 8-word-shingle overlap. Fires ONLY when the
    delivery adds NO substantial code beyond the scaffold AND its shared code
    files are near-identical to the scaffold — so a real app (which replaces or
    adds code) is never flagged. Never mutates anything; None on any error."""
    starter_stub = detect_offline_starter_stub(root, stack)
    if starter_stub:
        return starter_stub
    try:
        from skyn3t.agents._common import slugify
        from skyn3t.agents._scaffold import scaffold_for
    except Exception:  # noqa: BLE001
        return None
    root = Path(root)
    app_name = slugify(brief or "app", "app")
    try:
        pristine = scaffold_for(stack, app_name, brief or app_name)
    except Exception:  # noqa: BLE001 - unknown stack / scaffold error: skip
        return None
    scaffold_code = {
        rel.replace("\\", "/"): content
        for rel, content in pristine.items()
        if Path(rel).suffix.lower() in _ANTIFAKE_CODE_SUFFIXES
    }
    if not scaffold_code:
        return None
    delivered: dict[str, str] = {}
    for f in _iter_files(root):
        if f.suffix.lower() not in _ANTIFAKE_CODE_SUFFIXES:
            continue
        try:
            if f.stat().st_size > 500_000:
                continue
            delivered[str(f.relative_to(root)).replace("\\", "/")] = f.read_text(
                encoding="utf-8", errors="ignore")
        except OSError:
            continue
    if not delivered:
        return None
    # A real app adds its OWN code files beyond the scaffold's set.
    extra = [
        rel for rel, content in delivered.items()
        if rel not in scaffold_code and len(content.strip()) >= 40
    ]
    if extra:
        return None
    overlaps = [
        _shingle_overlap(delivered[rel], content)
        for rel, content in scaffold_code.items()
        if rel in delivered
    ]
    if not overlaps:
        return None
    mean = sum(overlaps) / len(overlaps)
    if mean >= threshold:
        return (
            f"delivered tree is essentially the pristine {stack} scaffold "
            f"(mean 8-word-shingle overlap {mean:.2f} across {len(overlaps)} "
            "source file(s), no app code added) — the build shipped the stub "
            "instead of implementing the brief"
        )
    return None


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
    # Frequent generated app imports. Pin the common, stable majors instead of
    # falling to nondeterministic "latest".
    "next-auth": "^4.24.7", "react-dnd": "^16.0.1",
    "react-dnd-html5-backend": "^16.0.1", "@tanstack/react-query": "^5.51.1",
    "jose": "^5.6.3", "monaco-editor": "^0.50.0", "react-redux": "^9.1.2",
    "redux": "^5.0.1",
}


def _js_keyword_is_code(text: str, pos: int) -> bool:
    """True when ``pos`` is outside JS strings and comments.

    The npm reconciler intentionally uses lightweight regexes, but generated apps
    often include prose like ``"click import '" + name + "'"``. Without this guard,
    that copy looks like a real import specifier.
    """
    state: str | None = None
    escaped = False
    i = 0
    while i < pos:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "line_comment":
            if ch in "\r\n":
                state = None
            i += 1
            continue
        if state == "block_comment":
            if ch == "*" and nxt == "/":
                state = None
                i += 2
            else:
                i += 1
            continue
        if state in {"'", '"', "`"}:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == state:
                state = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            state = "line_comment"
            i += 2
        elif ch == "/" and nxt == "*":
            state = "block_comment"
            i += 2
        elif ch in {"'", '"', "`"}:
            state = ch
            i += 1
        else:
            i += 1
    return state is None


def _pkg_name(spec: str) -> str:
    """Bare specifier -> npm package name ('react-dom/client'->'react-dom',
    '@scope/pkg/sub'->'@scope/pkg')."""
    if spec.startswith("@/"):
        return ""
    if ":" in spec:
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
                or re.search(r"[${}()<>\[\]'\"`!*~,;:]", name)
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


# PEP 517 build isolation may need to fetch the declared build backend. A
# disconnected registry is a degraded proof environment, not evidence that the
# generated package itself is malformed; an ordinary non-zero wheel build still
# remains a real delivery failure.
_OFFLINE_PIP_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "could not resolve host",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "proxyerror",
    "connecttimeout",
    "read timed out",
)


def _pip_build_is_offline(output: str) -> bool:
    return any(marker in (output or "").lower() for marker in _OFFLINE_PIP_MARKERS)


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


def sanitize_package_json_deps(root: str | Path) -> list[str]:
    """Remove dependency keys npm will reject.

    Trims fixable leading/trailing whitespace and drops unfixable names such as
    path aliases or prose fragments. Returns the removed/renamed keys.
    """
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

    changed: list[str] = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = pkg.get(section)
        if not isinstance(deps, dict):
            continue
        rebuilt: dict[str, Any] = {}
        for name, version in deps.items():
            if not isinstance(name, str):
                changed.append(str(name))
                continue
            trimmed = name.strip()
            candidate = {section: {trimmed: version}}
            if trimmed and not _invalid_npm_package_names(candidate):
                normalized_version = version
                if isinstance(version, str) and re.fullmatch(
                    r"\s*[~^]?0\.0\.0(?:-0)?\s*", version
                ):
                    # Code generators sometimes emit a semver-shaped placeholder
                    # that npm treats as a real, usually nonexistent release.
                    # Prefer a curated compatible pin; otherwise let npm resolve
                    # a real published version so proof can test compatibility.
                    normalized_version = _KNOWN_NPM_VERSIONS.get(trimmed, "latest")
                rebuilt[trimmed] = normalized_version
                if trimmed != name or normalized_version != version:
                    changed.append(name)
                continue
            changed.append(name)
        if rebuilt != deps:
            pkg[section] = rebuilt

    if not changed:
        return []
    try:
        pkg_path.write_text(_json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return []
    return sorted(set(changed))


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
        for match in _BARE_IMPORT_RE.finditer(text):
            if not _js_keyword_is_code(text, match.start()):
                continue
            spec = match.group(1)
            if spec.startswith(("node:", "virtual:")):
                continue
            name = _pkg_name(spec)
            if (
                name
                and name not in _NODE_BUILTINS
                and not _invalid_npm_package_names({"dependencies": {name: "latest"}})
            ):
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
    r"|<style\s+jsx"                                                   # styled-jsx (client-only)
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
        changed.append(rel.as_posix())
    return changed


_AT_IMPORT_RE = re.compile(r"""(?:from|import|require\()\s*['"]@/""")


# Tauri Cargo features the model commonly hallucinates (e.g. fs-read-text-file) break
# `cargo build`. The tauri-stack proof only builds the FRONTEND, so a broken Rust shell
# ships as go — this repair swaps an invalid feature set for a known-good superset.
_VALID_TAURI_FEATURES = frozenset({
    "api-all", "custom-protocol", "default", "devtools", "macos-private-api", "isolation",
    "dialog-all", "dialog-open", "dialog-save", "dialog-ask", "dialog-confirm", "dialog-message",
    "fs-all", "fs-read-file", "fs-write-file", "fs-read-dir", "fs-create-dir", "fs-copy-file",
    "fs-remove-file", "fs-remove-dir", "fs-rename-file", "fs-extract-api",
    "path-all", "shell-all", "shell-open", "shell-execute", "shell-sidecar",
    "os-all", "process-all", "process-command-api", "process-exit", "process-relaunch",
    "clipboard-all", "clipboard-read-text", "clipboard-write-text",
    "http-all", "http-request", "notification-all", "global-shortcut-all",
    "window-all", "window-center", "window-close", "window-create", "window-hide",
    "window-maximize", "window-minimize", "window-set-title", "window-set-focus",
    "window-set-size", "window-set-position", "window-show", "window-start-dragging",
    "system-tray", "updater", "protocol-all", "protocol-asset", "icon-png", "icon-ico",
})
# When the model hallucinates the shell, reset to the matched permissive pair: the
# `api-all` Cargo feature + allowlist {"all": true}. Tauri REQUIRES the Cargo features
# to match the allowlist, so a fixed pair is the only reliably-buildable combo.
_SAFE_TAURI_FEATURES = '["api-all"]'
_TAURI_FEATURES_RE = re.compile(r"(tauri\s*=\s*\{[^}]*?features\s*=\s*)\[([^\]]*)\]")


def reconcile_tauri_cargo_features(root: str | Path) -> list[str]:
    """Make a Tauri shell buildable when the model hallucinated it. If src-tauri's
    Cargo.toml has any invalid tauri feature, reset the features to ``api-all`` AND set
    the tauri.conf.json allowlist to ``{"all": true}`` — Tauri requires the two to
    match, so this matched pair is the reliable fix. No-op for a valid non-Tauri project
    or an already-valid feature set. Never raises."""
    cargo = Path(root) / "src-tauri" / "Cargo.toml"
    if not cargo.is_file():
        return []
    try:
        text = cargo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    m = _TAURI_FEATURES_RE.search(text)
    if not m:
        return []
    feats = [f.strip().strip('"\'') for f in m.group(2).split(",") if f.strip()]
    if not any(f and f not in _VALID_TAURI_FEATURES for f in feats):
        return []  # all valid — leave both files alone
    changed: list[str] = []
    new_text = text[:m.start()] + m.group(1) + _SAFE_TAURI_FEATURES + text[m.end():]
    try:
        cargo.write_text(new_text, encoding="utf-8")
        changed.append("src-tauri/Cargo.toml")
    except OSError:
        return changed
    # Align the allowlist with api-all so Tauri's feature<->allowlist check passes.
    conf = Path(root) / "src-tauri" / "tauri.conf.json"
    if conf.is_file():
        try:
            data = json.loads(conf.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                data.setdefault("tauri", {})["allowlist"] = {"all": True}
                conf.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                changed.append("src-tauri/tauri.conf.json")
        except (OSError, ValueError):
            pass
    return changed


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


def ensure_vitest_alias_config(root: str | Path) -> list[str]:
    """Make ``@/`` imports in Vitest suites resolve like the application.

    Astro reads aliases from ``astro.config`` and TypeScript reads ``paths``, but
    standalone Vitest reads neither. Create a small Vitest config only when the
    project has Vitest, an aliased test import, and no Vite/Vitest config of its
    own. Existing executable configs are never overwritten.
    """
    root = Path(root)
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return []
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(pkg, dict):
        return []
    declared = {
        name
        for section in ("dependencies", "devDependencies")
        if isinstance(pkg.get(section), dict)
        for name in pkg[section]
    }
    raw_scripts = pkg.get("scripts")
    scripts: dict[str, Any] = raw_scripts if isinstance(raw_scripts, dict) else {}
    if "vitest" not in declared and not any(
        "vitest" in str(command) for command in scripts.values()
    ):
        return []
    config_names = (
        "vitest.config.js", "vitest.config.mjs", "vitest.config.ts",
        "vitest.config.mts", "vite.config.js", "vite.config.mjs",
        "vite.config.ts", "vite.config.mts",
    )
    if any((root / name).is_file() for name in config_names):
        return []
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    uses_alias = False
    for test_file in _iter_files(tests_dir):
        if test_file.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
            continue
        try:
            if _AT_IMPORT_RE.search(
                test_file.read_text(encoding="utf-8", errors="replace")
            ):
                uses_alias = True
                break
        except OSError:
            continue
    if not uses_alias:
        return []
    alias_target = "./src" if (root / "src").is_dir() else "."
    target = root / "vitest.config.mjs"
    content = (
        "import { fileURLToPath } from 'node:url';\n"
        "import { defineConfig } from 'vitest/config';\n\n"
        "export default defineConfig({\n"
        "  resolve: {\n"
        "    alias: {\n"
        f"      '@': fileURLToPath(new URL('{alias_target}', import.meta.url)),\n"
        "    },\n"
        "  },\n"
        "});\n"
    )
    try:
        target.write_text(content, encoding="utf-8")
    except OSError:
        return []
    return [target.name]


_IMPORT_TYPE_RE = re.compile(r"^\s*import\s+type\s")
_EXPORT_TYPE_RE = re.compile(r"^\s*export\s+type\s+\w")
_TS_SIMPLE_TYPE = (
    r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?"
    r"(?:<[^()\n;=]*>)?(?:\[\])?"
)
_TS_PARAM_TYPE_RE = re.compile(rf"\b([A-Za-z_$][\w$]*)(?:\?)?\s*:\s*{_TS_SIMPLE_TYPE}")
_TS_ARROW_PARAMS_RE = re.compile(r"\(([^()\n]*)\)(\s*(?::\s*[^=;\n]+)?\s*=>)")
_TS_FUNCTION_PARAMS_RE = re.compile(r"(\bfunction\s+[A-Za-z_$][\w$]*\s*)\(([^()\n]*)\)")
_TS_VAR_TYPE_RE = re.compile(rf"^(\s*(?:const|let|var)\s+[A-Za-z_$][\w$]*)\s*:\s*{_TS_SIMPLE_TYPE}\s*=")
_TS_NON_NULL_RE = re.compile(r"(?<=[\]\)])!(?=[\.\),;])")


def _balanced_line(s: str) -> bool:
    return (s.count("(") == s.count(")") and s.count("[") == s.count("]")
            and s.count("{") == s.count("}"))


def _strip_inline_ts_syntax_in_js_line(line: str) -> tuple[str, bool]:
    """Conservatively erase common TS-only syntax from one plain JS/JSX line."""
    original = line

    def _strip_params(params: str) -> str:
        # ``{ source: localName }`` and ``[first]`` are valid JavaScript
        # destructuring. A colon in that syntax is an alias, not a TypeScript
        # annotation, so leave destructured parameter lists untouched rather
        # than risking a source-corrupting "repair".
        if any(marker in params for marker in ("{", "}", "[", "]")):
            return params
        return _TS_PARAM_TYPE_RE.sub(r"\1", params)

    def _arrow_repl(match: re.Match[str]) -> str:
        suffix = re.sub(r":\s*[^=;\n]+(?=\s*=>)", "", match.group(2))
        return f"({_strip_params(match.group(1))}){suffix}"

    def _function_repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}({_strip_params(match.group(2))})"

    line = _TS_ARROW_PARAMS_RE.sub(_arrow_repl, line)
    line = _TS_FUNCTION_PARAMS_RE.sub(_function_repl, line)
    line = _TS_VAR_TYPE_RE.sub(r"\1 =", line)
    line = _TS_NON_NULL_RE.sub("", line)
    return line, line != original


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
            fixed, did_fix = _strip_inline_ts_syntax_in_js_line(ln)
            if did_fix:
                removed = True
            out.append(fixed)
        if removed:
            try:
                f.write_text("".join(out), encoding="utf-8")
            except OSError:
                continue
            changed.append(f.relative_to(root).as_posix())
    return changed


_SOURCE_FENCE_SUFFIXES = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".html", ".py",
    ".go", ".rs", ".java", ".vue", ".svelte", ".astro",
})
_CODE_FENCE_LINE_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*$")


def _looks_like_source_path_header(line: str, rel: str) -> bool:
    clean = line.strip().replace("\\", "/")
    return clean in {rel, Path(rel).name}


def strip_markdown_fences_in_source_files(root: str | Path) -> list[str]:
    """Remove accidental markdown wrappers from source files.

    Some low-cost models emit a file as ``path/to/File.jsx`` + fenced code, and
    the agentic writer can persist that wrapper literally. Only strip when the
    first nonblank line is an optional matching path header followed by a code
    fence and the last nonblank line closes the fence.
    """
    root = Path(root)
    changed: list[str] = []
    for f in _iter_files(root):
        if f.suffix not in _SOURCE_FENCE_SUFFIXES:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError:
            continue
        if not lines:
            continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        start = 0
        while start < len(lines) and not lines[start].strip():
            start += 1
        if start >= len(lines):
            continue
        fence_at = start
        if _looks_like_source_path_header(lines[start], rel):
            fence_at = start + 1
        if fence_at >= len(lines) or not _CODE_FENCE_LINE_RE.match(lines[fence_at]):
            continue
        end = len(lines) - 1
        while end > fence_at and not lines[end].strip():
            end -= 1
        if end <= fence_at or not lines[end].strip().startswith("```"):
            continue
        new_lines = lines[:start] + lines[fence_at + 1:end] + lines[end + 1:]
        try:
            f.write_text("".join(new_lines), encoding="utf-8")
        except OSError:
            continue
        changed.append(rel)
    return changed


_REACT_VITE_TSX_REPAIR_STACKS = frozenset({"react", "react_vite", "react_ts", "vite"})
_MODULE_SCRIPT_SRC_RE = re.compile(
    r'(<script\b[^>]*\btype=["\']module["\'][^>]*\bsrc=["\'])([^"\']+)(["\'][^>]*>\s*</script>)',
    re.IGNORECASE,
)


def _jsx_source_contaminated(text: str) -> bool:
    head = text.lstrip("\ufeff \t\r\n")[:1200]
    return (
        "```" in head
        or "\nimport type " in head
        or "\nexport type " in head
        or re.search(r"\buse[A-Z][A-Za-z0-9_]*\s*<", head) is not None
        or re.search(r"\([^()\n]*:\s*[A-Za-z_$][\w$]*(?:[<\),])", head) is not None
        or ")!" in head
    )


def repair_react_vite_entrypoint_to_tsx(project_dir: str | Path, stack: str = "") -> list[str]:
    """Point Vite HTML at a clean TSX twin when the JSX entry tree is contaminated.

    This is intentionally narrow: it only edits React/Vite-ish stacks, only when
    the HTML currently points at a ``.jsx`` module, only when a corresponding
    ``.tsx`` entry exists, and only when the current JSX entry or ``src/App.jsx``
    shows obvious markdown/TypeScript contamination.
    """
    if (stack or "").lower() not in _REACT_VITE_TSX_REPAIR_STACKS:
        return []
    root = Path(project_dir)
    index = root / "index.html"
    try:
        html = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    match = _MODULE_SCRIPT_SRC_RE.search(html)
    if not match:
        return []
    src = match.group(2)
    current_rel = src.split("?", 1)[0].lstrip("/")
    if not current_rel.endswith(".jsx"):
        return []
    current = root / current_rel
    candidate_rel = current_rel[:-4] + "tsx"
    candidate = root / candidate_rel
    if not candidate.is_file():
        fallback = root / "src" / "main.tsx"
        if not fallback.is_file():
            return []
        candidate = fallback
        candidate_rel = str(fallback.relative_to(root)).replace("\\", "/")
    contaminated = False
    for path in (current, root / "src" / "App.jsx"):
        try:
            contaminated = contaminated or _jsx_source_contaminated(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
    if not contaminated:
        return []
    replacement = "/" + candidate_rel
    new_html = html[:match.start(2)] + replacement + html[match.end(2):]
    if new_html == html:
        return []
    try:
        index.write_text(new_html, encoding="utf-8")
    except OSError:
        return []
    return ["index.html"]


_LUCIDE_IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*['\"]lucide-react['\"]")
_COMMON_LUCIDE_EXPORTS = frozenset({
    "Activity", "AlertCircle", "ArrowLeft", "ArrowRight", "ArrowUpRight", "Award",
    "BarChart", "Bell", "BookOpen", "Bot", "Box", "Calendar", "Check", "CheckCircle",
    "ChevronDown", "Circle", "Clock", "Code", "Download", "ExternalLink", "Eye",
    "FileText", "Flag", "Folder", "Gauge", "Heart", "Home", "Image", "Info",
    "Loader2", "Mail", "MapPin", "Menu", "MessageCircle", "MessageSquare", "Minus",
    "Plus", "Quote", "Search", "Send", "Settings", "Shield", "Sparkles", "Square",
    "Star", "Sun", "Target", "Trophy", "Upload", "User", "Users", "Wrench", "X",
    "Zap",
})
_LUCIDE_ICON_ALIASES = {
    "GolfIcon": "Flag",
    "GeneratorIcon": "Zap",
    "PipeIcon": "Circle",
}


def _lucide_real_exports(root: Path) -> set[str]:
    """Real lucide-react icon names from the INSTALLED package's d.ts (each icon is a
    `declare const Name`). Empty when not installed — then we can't validate, so no-op."""
    dts = root / "node_modules" / "lucide-react" / "dist" / "lucide-react.d.ts"
    try:
        text = dts.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    names = set(re.findall(r"declare const ([A-Z][A-Za-z0-9]*)\b", text))
    names.update(re.findall(r"\bas\s+([A-Z][A-Za-z0-9]*)\b", text))
    return names


def reconcile_lucide_icons(root: str | Path) -> list[str]:
    """Replace hallucinated lucide-react icon imports (e.g. ``GeneratorIcon``, which the
    package doesn't export) with a real icon — in the import AND its JSX usages — so the
    build doesn't fail with "X is not exported from 'lucide-react'". Tries the name minus
    a trailing 'Icon', else a safe generic. No-op when lucide isn't installed (can't
    validate against real exports). Never raises."""
    root = Path(root)
    real = _lucide_real_exports(root)
    if not real:
        real = set(_COMMON_LUCIDE_EXPORTS)
    fallback = next((c for c in ("Circle", "Square", "Star", "Box") if c in real), "")
    if not fallback:
        return []
    changed: list[str] = []
    for f in _iter_files(root):
        if f.suffix not in (".jsx", ".tsx", ".js", ".ts"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "lucide-react" not in text:
            continue
        repls: dict[str, str] = {}
        for m in _LUCIDE_IMPORT_RE.finditer(text):
            for raw in m.group(1).split(","):
                src = raw.split(" as ")[0].strip()
                if src and src not in real and src not in repls:
                    alias = _LUCIDE_ICON_ALIASES.get(src)
                    base = src[:-4] if (src.endswith("Icon") and src[:-4] in real) else None
                    repls[src] = alias if alias in real else (base or fallback)
        if not repls:
            continue
        new_text = text
        for src, rep in repls.items():
            new_text = re.sub(rf"\b{re.escape(src)}\b", rep, new_text)
        # Replacing two hallucinated names with the same fallback (or one already
        # imported) yields a duplicate specifier — `{ Circle, Circle }` — which is a
        # syntax error. Dedupe each lucide import's name list (usages can repeat fine).
        def _dedupe_import(m: re.Match) -> str:
            seen: set[str] = set()
            kept: list[str] = []
            for raw in m.group(1).split(","):
                nm = raw.strip()
                if not nm:
                    continue
                key = nm.split(" as ")[-1].strip()
                if key not in seen:
                    seen.add(key)
                    kept.append(nm)
            return "import { " + ", ".join(kept) + " } from 'lucide-react'"

        new_text = _LUCIDE_IMPORT_RE.sub(_dedupe_import, new_text)
        if new_text != text:
            try:
                f.write_text(new_text, encoding="utf-8")
            except OSError:
                continue
            changed.append(f.relative_to(root).as_posix())
    return changed


def repair_phaser_html_entrypoint(project_dir: str | Path, stack: str = "") -> list[str]:
    """Ensure a Phaser project boots the Phaser entry module.

    Generic Vite HTML can be "valid" while still loading a stale React shell
    (`/src/main.jsx`) and leaving the real game in `/src/main.js` as dead code.
    This repair is intentionally Phaser-only and only fires when `src/main.js`
    actually contains the game boot.
    """
    if (stack or "").lower() != "phaser":
        return []
    root = Path(project_dir)
    index = root / "index.html"
    main = root / "src" / "main.js"
    try:
        html = index.read_text(encoding="utf-8", errors="replace")
        main_js = main.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "new Phaser.Game" not in main_js:
        return []

    module_re = re.compile(
        r'<script\b[^>]*\btype=["\']module["\'][^>]*\bsrc=["\']([^"\']+)["\'][^>]*>\s*</script>',
        re.IGNORECASE,
    )

    def _loaded_boot_module(match: re.Match[str]) -> bool:
        src = match.group(1).split("?", 1)[0].lstrip("/")
        if src.startswith("./"):
            src = src[2:]
        try:
            text = (root / src).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "new Phaser.Game" in text

    if any(_loaded_boot_module(m) for m in module_re.finditer(html)):
        return []

    new_html, n = module_re.subn(
        '<script type="module" src="/src/main.js"></script>',
        html,
        count=1,
    )
    if n == 0:
        if "</body>" in html:
            new_html = html.replace(
                "</body>",
                '  <script type="module" src="/src/main.js"></script>\n  </body>',
            )
        else:
            new_html = html + '\n<script type="module" src="/src/main.js"></script>\n'
    if 'id="game-container"' not in new_html and "id='game-container'" not in new_html:
        if '<div id="root"></div>' in new_html:
            new_html = new_html.replace('<div id="root"></div>', '<div id="game-container"></div>')
        elif "<body>" in new_html:
            new_html = new_html.replace("<body>", '<body><div id="game-container"></div>', 1)
        else:
            new_html = '<div id="game-container"></div>\n' + new_html
    if new_html == html:
        return []
    try:
        index.write_text(new_html, encoding="utf-8")
    except OSError:
        return []
    return ["index.html"]


def _packaged_fastapi_main_module(root: Path) -> str | None:
    """Return the sole declared ``src`` package exposing ``main.app``, if any."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib
    except ImportError:  # pragma: no cover - SkyN3t requires Python 3.11+
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    tool = data.get("tool") if isinstance(data, dict) else None
    hatch = tool.get("hatch") if isinstance(tool, dict) else None
    build = hatch.get("build") if isinstance(hatch, dict) else None
    targets = build.get("targets") if isinstance(build, dict) else None
    wheel = targets.get("wheel") if isinstance(targets, dict) else None
    packages = wheel.get("packages", []) if isinstance(wheel, dict) else []
    if not isinstance(packages, list):
        return None
    candidates: list[str] = []
    for declared in packages:
        if not isinstance(declared, str):
            continue
        declared_path = Path(declared)
        if declared_path.is_absolute() or ".." in declared_path.parts:
            continue
        package_dir = root / declared_path
        main = package_dir / "main.py"
        if not (package_dir / "__init__.py").is_file() or not main.is_file():
            continue
        try:
            body = main.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not re.search(r"(?m)^\s*app\s*=", body):
            continue
        parts = Path(declared).parts
        if "src" not in parts:
            continue
        module_parts = parts[parts.index("src") + 1 :]
        if module_parts:
            candidates.append(".".join(module_parts))
    return candidates[0] if len(candidates) == 1 else None


def reconcile_packaged_fastapi_entrypoint(
    project_dir: str | Path, stack: str = ""
) -> list[str]:
    """Make a stale root FastAPI scaffold delegate to its packaged app.

    Agentic code can generate a complete ``src/<package>`` service while an
    earlier root ``main.py`` scaffold survives. If both expose a FastAPI app,
    running ``python main.py`` serves a different API than the tested/package
    application. Restrict this repair to an explicit single Hatch ``src``
    package and a root ``app = FastAPI(...)`` implementation, then retain the
    root command as a thin compatibility launcher for the canonical ASGI app.
    """
    if (stack or "").lower() != "fastapi":
        return []
    root = Path(project_dir)
    root_main = root / "main.py"
    module = _packaged_fastapi_main_module(root)
    if module is None or not root_main.is_file():
        return []
    try:
        body = root_main.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if f"from {module}.main import app" in body:
        return []
    if "FastAPI" not in body or not re.search(r"(?m)^\s*app\s*=\s*FastAPI\s*\(", body):
        return []
    launcher = f'''"""Compatibility launcher for the packaged ASGI application."""

from {module}.main import app


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
'''
    try:
        root_main.write_text(launcher, encoding="utf-8")
    except OSError:
        return []
    return ["main.py"]


def _ruff_import_sorting_is_configured(project_dir: str | Path) -> bool:
    """Whether the project explicitly asks Ruff to enforce import ordering.

    We only normalize imports when the delivered project's own Ruff settings opt
    into the ``I`` rules. That keeps this repair narrowly scoped: it closes a
    promised ``ruff check .`` gap without imposing a new lint policy on projects
    that do not use Ruff import sorting.
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover - SkyN3t requires Python 3.11+
        return False

    root = Path(project_dir)
    for name in ("pyproject.toml", "ruff.toml", ".ruff.toml"):
        config = root / name
        if not config.is_file():
            continue
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            continue
        ruff = (
            data.get("tool", {}).get("ruff", {})
            if name == "pyproject.toml"
            else data
        )
        if not isinstance(ruff, dict):
            continue
        lint = ruff.get("lint", {})
        lint = lint if isinstance(lint, dict) else {}

        def rule_selected(value: Any) -> bool:
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, (list, tuple)):
                return False
            return any(
                isinstance(rule, str)
                and (rule.upper() == "ALL" or rule.upper().startswith("I"))
                for rule in values
            )

        selections = (
            lint.get("select"),
            lint.get("extend-select"),
            ruff.get("select"),
            ruff.get("extend-select"),
        )
        ignores = (
            lint.get("ignore"),
            lint.get("extend-ignore"),
            ruff.get("ignore"),
            ruff.get("extend-ignore"),
        )
        if any(rule_selected(value) for value in selections) and not any(
            rule_selected(value) for value in ignores
        ):
            return True
    return False


def _ruff_is_configured(project_dir: str | Path) -> bool:
    """Whether a project explicitly carries a Ruff configuration file."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - SkyN3t requires Python 3.11+
        return False
    root = Path(project_dir)
    for name in ("pyproject.toml", "ruff.toml", ".ruff.toml"):
        config = root / name
        if not config.is_file():
            continue
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            continue
        ruff = (
            data.get("tool", {}).get("ruff", {})
            if name == "pyproject.toml"
            else data
        )
        if isinstance(ruff, dict):
            return True
    return False


def _ruff_repairable_python_sources(root: Path) -> dict[str, bytes]:
    """Snapshot authored Python sources Ruff may safely mutate.

    TestAuthor acceptance contracts are deliberately byte-signed: their exact
    ``pytest.mark.skip(reason=...)`` marker is how the clone reconciler protects
    them. Ruff formatting may split that decorator across lines, so never route
    a signed contract through an auto-fixer.
    """
    from skyn3t.studio.acceptance_contract import is_system_acceptance_contract

    sources: dict[str, bytes] = {}
    for source in sorted(_iter_files(root), key=lambda path: path.as_posix()):
        if source.suffix.lower() not in {".py", ".pyi"}:
            continue
        if is_system_acceptance_contract(source, root):
            continue
        try:
            sources[source.relative_to(root).as_posix()] = source.read_bytes()
        except OSError:
            continue
    return sources


def sort_ruff_imports(project_dir: str | Path) -> list[str]:
    """Apply Ruff's safe import-order fixes when a Python project opts into them.

    Ruff is an optional SkyN3t development dependency, so an unavailable tool is
    a deliberate no-op. ``--no-cache`` ensures that proving/repairing an artifact
    never leaves a ``.ruff_cache`` directory in the delivered tree.
    """
    root = Path(project_dir)
    if not _ruff_import_sorting_is_configured(root):
        return []
    sources = _ruff_repairable_python_sources(root)
    if not sources:
        return []
    try:
        import subprocess

        from ruff import find_ruff_bin

        subprocess.run(
            [
                str(find_ruff_bin()),
                "check",
                "--no-cache",
                "--select",
                "I",
                "--fix",
                *sources,
            ],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (ImportError, OSError, subprocess.SubprocessError):
        return []
    changed: list[str] = []
    for rel, before in sources.items():
        try:
            if (root / rel).read_bytes() != before:
                changed.append(rel)
        except OSError:
            continue
    return sorted(changed)


def format_ruff_python_sources(project_dir: str | Path) -> list[str]:
    """Apply Ruff formatting when a project explicitly configures Ruff.

    The formatter handles safe layout defects that import sorting cannot, such
    as an otherwise-valid generated line exceeding the project's configured
    ``E501`` width. As above, this is opt-in and cache-free.
    """
    root = Path(project_dir)
    if not _ruff_is_configured(root):
        return []
    sources = _ruff_repairable_python_sources(root)
    if not sources:
        return []
    try:
        import subprocess

        from ruff import find_ruff_bin

        subprocess.run(
            [str(find_ruff_bin()), "format", "--no-cache", *sources],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (ImportError, OSError, subprocess.SubprocessError):
        return []
    changed: list[str] = []
    for rel, before in sources.items():
        try:
            if (root / rel).read_bytes() != before:
                changed.append(rel)
        except OSError:
            continue
    return sorted(changed)


_NODE_TEST_EXTS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")


def repair_node_test_script(project_dir: str | Path) -> list[str]:
    """Rewrite ``node --test <dir>`` to the glob form that actually discovers.

    Node 24 resolves a positional argument as a module entry point rather than
    walking it, so ``node --test tests`` dies with MODULE_NOT_FOUND naming the
    CommonJS loader and the runner reports exactly one failing "test". Measured
    against a known-good ESM suite, isolating the argument form from the test
    content:

        node --test tests             -> MODULE_NOT_FOUND, pass 0, fail 1
        node --test "tests/*.test.js" -> pass 1, fail 0

    So every delivered app scripted that way reported a failing suite no matter
    how good its tests were, and fed the repair loop an imaginary missing
    module instead of the real defect.

    Only a DIRECTORY positional is rewritten: a file path already works, and a
    bare ``node --test`` discovers on its own. A directory that does not exist,
    or holds no test files, is left alone — emitting a glob that matches
    nothing would exit 0 and report a PASS with zero tests, turning a broken
    suite into false green evidence. Idempotent: the rewritten positional is no
    longer a directory, so a second pass is a no-op."""
    import json as _json

    root = Path(project_dir)
    pkg_path = root / "package.json"
    try:
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(pkg, dict):
        return []
    scripts = pkg.get("scripts")
    if not isinstance(scripts, dict):
        return []
    script = str(scripts.get("test") or "").strip()
    parts = script.split()
    if len(parts) < 2 or parts[0] != "node" or "--test" not in parts:
        return []
    positionals = [p for p in parts[1:] if not p.startswith("-")]
    if len(positionals) != 1:
        # 0 = auto-discovery; >1 = an explicit list the author chose. Both work.
        return []
    target = positionals[0].strip("\"'").rstrip("/\\")
    if not target or (root / target).is_dir() is False:
        return []
    tdir = root / target
    exts = sorted({
        p.suffix for p in tdir.rglob("*")
        if p.is_file() and p.suffix in _NODE_TEST_EXTS and ".test." in p.name
    })
    if not exts:
        return []
    flags = [p for p in parts[1:] if p.startswith("-")]
    globs = [f'"{target}/**/*.test{ext}"' for ext in exts]
    rewritten = " ".join(["node", *flags, *globs])
    if rewritten == script:
        return []
    scripts["test"] = rewritten
    try:
        pkg_path.write_text(_json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return []
    return [f"test: {script} -> {rewritten}"]


def apply_deterministic_repairs(project_dir: str | Path, stack: str = "") -> dict[str, list[str]]:
    """Run every deterministic, idempotent, code-MUTATING build repair in one
    pass and return what changed. This is the single source of truth for the
    "make a delivered tree build-ready" repairs, shared by the main build
    pipeline (StudioRunner._deterministic_repairs, which layers the advisory
    contrast lint on top) AND the headless improve engine
    (ImproveEngine.improve) — so an improve that introduces a client component,
    a new dependency, or a missing local import gets the SAME auto-fixes a fresh
    build would, instead of silently shipping an app that won't build.

    Scaffold FIRST: a generated stub may itself import a package (e.g. ``import
    React from 'react'``), so declaring deps afterwards picks those up in the
    same pass — making the whole repair converge in one call. Safe to call
    repeatedly (a complete tree is a no-op). Never raises on an individual
    repair's expected filesystem errors (each function is already defensive)."""
    source_fences = strip_markdown_fences_in_source_files(project_dir)
    sanitized = sanitize_package_json_deps(project_dir)
    relinked = relink_unresolved_relative_imports(project_dir)
    stubbed = scaffold_missing_imports(project_dir, stack=stack)
    added = reconcile_npm_deps(project_dir)
    peers = reconcile_next_config_peers(project_dir)
    # Next.js App Router: prepend "use client" to interactive components so
    # `next build` doesn't fail static generation on event handlers/hooks.
    use_client = add_use_client_directives(project_dir)
    # Map the @/ import alias (write jsconfig paths) and strip stray TS-only
    # statements from .js files — two common cheap-model defects that otherwise
    # fail `next build` ("Can't resolve '@/...'" / "Expected '{', got 'type'").
    alias_cfg = ensure_path_alias_config(project_dir)
    vitest_alias_cfg = ensure_vitest_alias_config(project_dir)
    ts_stripped = strip_ts_type_in_js(project_dir)
    react_entry = repair_react_vite_entrypoint_to_tsx(project_dir, stack=stack)
    # Replace hallucinated lucide-react icon imports (e.g. GeneratorIcon) with real
    # ones — the model invents icon names that aren't exported, failing the build.
    lucide = reconcile_lucide_icons(project_dir)
    # Tauri desktop: fix hallucinated Cargo feature names so the Rust shell builds.
    tauri_cargo = reconcile_tauri_cargo_features(project_dir)
    # Phaser game builds: make sure index.html loads the game boot, not a stale
    # React/Vite shell that happens to compile.
    phaser_entry = repair_phaser_html_entrypoint(project_dir, stack=stack)
    # A generated FastAPI package may coexist with a stale root scaffold. Keep
    # `python main.py` and the packaged/tested ASGI surface aligned.
    fastapi_entry = reconcile_packaged_fastapi_entrypoint(project_dir, stack=stack)
    # Game stacks: synthesize a placeholder PNG for any referenced-but-missing
    # image asset (codegen loading an invented '/assets/sprites/gold.png' 404s at
    # runtime and CRASHES the scene — the tower-defence-retry-2 no_go). Creates
    # files only, never edits code (do-no-harm).
    from skyn3t.studio.asset_reconcile import reconcile_asset_refs
    assets = reconcile_asset_refs(project_dir, stack=stack)
    # Game stacks: the complement of asset_reconcile — guarantee every texture key
    # a render call USES is actually LOADED, synthesizing a placeholder + injecting
    # the loader for any invented/never-loaded key. This is the deterministic,
    # model-INDEPENDENT fix for the "sprites load but render as green boxes" bug
    # (happens on frontier models too), instead of only advising the fix-loop.
    from skyn3t.studio.texture_reconcile import reconcile_texture_keys
    textures = reconcile_texture_keys(project_dir, stack=stack)
    # A project that promises Ruff's import-order check should not ship trivially
    # auto-fixable import or formatting failures. These run after other repairs so
    # they see the final generated Python source.
    python_imports = sort_ruff_imports(project_dir)
    python_formatted = format_ruff_python_sources(project_dir)
    # `node --test <dir>` does not walk the directory on Node 24 — it resolves
    # the positional as a module entry point and dies MODULE_NOT_FOUND, so the
    # suite reports one failing "test" naming a CJS loader even for a pure-ESM
    # app whose tests are perfect. Rewrite it to the glob form that works.
    node_test_script = repair_node_test_script(project_dir)
    return {
        "node_test_script": node_test_script,
        "npm_deps_added": added,
        "npm_deps_sanitized": sanitized,
        "next_config_peers": peers,
        "imports_relinked": relinked,
        "imports_scaffolded": stubbed,
        "use_client_added": use_client,
        "source_fences_stripped": source_fences,
        "path_alias_config": alias_cfg,
        "vitest_alias_config": vitest_alias_cfg,
        "ts_in_js_stripped": ts_stripped,
        "react_entrypoint_repaired": react_entry,
        "lucide_icons_fixed": lucide,
        "tauri_cargo_fixed": tauri_cargo,
        "phaser_entrypoint_repaired": phaser_entry,
        "fastapi_entrypoint_unified": fastapi_entry,
        "assets_reconciled": assets.get("images_created", []),
        "assets_missing_audio": assets.get("missing_audio", []),
        "textures_loaded_added": textures.get("textures_loaded_added", []),
        "texture_placeholders_created": textures.get("placeholders_created", []),
        "python_imports_sorted": python_imports,
        "python_formatted": python_formatted,
    }


# --- mock-LLM proof seam (research item 42) ---------------------------------
# A generated project that CALLS an LLM cannot run its OWN tests headlessly
# without a live endpoint + key. For the TEST step only, we boot a local
# deterministic OpenAI/Anthropic-compatible mock (skyn3t.studio.mock_llm) and
# inject a base-url + dummy-key env overlay so those tests exercise real plumbing
# at ZERO API spend. Advisory + degrade-open everywhere: any failure (not an LLM
# app / mock can't start / network-isolated sandbox) => the tests run EXACTLY as
# before. The build step is never touched.

# npm/PyPI client packages that mean "this app calls an LLM" (exact dep-name match
# via _declared_dep_names — no substring false positives like `openai-tokenizer`).
_LLM_CLIENT_DEPS = frozenset({"openai", "anthropic", "@anthropic-ai/sdk"})
# Source references that reveal an LLM call even when the client is instantiated
# implicitly (no declared dep, e.g. a raw fetch to an OpenAI-compatible endpoint).
_LLM_SOURCE_MARKERS = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "openrouter.ai/api",
)
_LLM_SCAN_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def _project_declares_llm_client(root: Path) -> bool:
    """True if the project declares an openai/anthropic client dependency."""
    try:
        from skyn3t.studio.app_runner import _declared_dep_names
        return bool(_declared_dep_names(root) & _LLM_CLIENT_DEPS)
    except Exception:  # noqa: BLE001 - detection is best-effort, never blocks proof
        return False


def _project_references_llm(root: Path) -> bool:
    """True if any source/.env file references an OpenAI/OpenRouter/Anthropic base
    url or key — an LLM call even without a declared client dep."""
    for f in _iter_files(root):
        if f.suffix not in _LLM_SCAN_SUFFIXES and not f.name.startswith(".env"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(m in text for m in _LLM_SOURCE_MARKERS):
            return True
    return False


def _is_llm_calling_project(root: Path) -> bool:
    """Detect a project whose delivered app CALLS an LLM (client dep OR source
    reference). Pure, offline, never raises."""
    return _project_declares_llm_client(root) or _project_references_llm(root)


@dataclass(slots=True)
class _MockLLMSeam:
    """A running mock-LLM server plus the env overlay to point tests at it."""

    server: Any
    env: dict[str, str]

    def stop(self) -> None:
        try:
            self.server.stop()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass


def _mock_llm_default_enabled() -> bool:
    """The ``mock_llm_proof_enabled`` setting (default True). Never raises."""
    try:
        from skyn3t.config.settings import get_settings
        return bool(getattr(get_settings(), "mock_llm_proof_enabled", True))
    except Exception:  # noqa: BLE001
        return True


def _proof_posture_default() -> str:
    """The ``build_posture`` setting. Never raises.

    Defaults to ``"release"`` on any failure so a proof can only ever become
    *more* permissive through explicit configuration, never through an error.
    """
    try:
        from skyn3t.config.settings import get_settings
        raw = str(getattr(get_settings(), "build_posture", "release") or "release")
    except Exception:  # noqa: BLE001
        return "release"
    normalized = raw.strip().lower()
    return normalized if normalized in {"lab", "release"} else "release"


def _start_proof_mock_llm(
    pdir: Path, cmd_ctx: _ProofCommandContext | None, *, enabled: bool,
) -> _MockLLMSeam | None:
    """Boot the mock-LLM server + build the test-step env overlay, or None.

    Returns None (degrade-open, tests run unchanged) when: the seam is disabled,
    the project doesn't call an LLM, a network-isolated docker sandbox is in use
    (it can't reach the host-loopback mock, so injecting an unreachable base url
    would be worse than nothing), or the server fails to start.
    """
    if not enabled:
        return None
    # Only the local-subprocess proof path has host-network access to reach the
    # mock on 127.0.0.1; a `--network none` docker sandbox cannot.
    if cmd_ctx is not None and getattr(cmd_ctx, "docker_available", False):
        return None
    try:
        if not _is_llm_calling_project(pdir):
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        from skyn3t.security.secrets import MOCK_PROOF_VALUE
        from skyn3t.studio.mock_llm import MockLLMServer

        server = MockLLMServer()
        base = server.start()
    except Exception:  # noqa: BLE001 - never let the seam break the proof
        return None
    if not base:
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass
        return None
    # The OpenAI SDK targets `{base}/chat/completions`; Anthropic targets
    # `{base}/v1/messages`. So OpenAI/OpenRouter clients get a `/v1` base and the
    # Anthropic client gets the bare base. Dummy keys satisfy client constructors
    # that require a non-empty key (the mock ignores auth).
    env = {
        "OPENAI_BASE_URL": f"{base}/v1",
        "OPENAI_API_BASE": f"{base}/v1",
        "ANTHROPIC_BASE_URL": base,
        "MOCK_LLM_BASE_URL": base,
        "OPENROUTER_API_KEY": MOCK_PROOF_VALUE,
        "OPENAI_API_KEY": MOCK_PROOF_VALUE,
        "ANTHROPIC_API_KEY": MOCK_PROOF_VALUE,
    }
    return _MockLLMSeam(server=server, env=env)


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
    enable_mock_llm: bool | None = None,
    install_python_deps: bool | None = None,
    python_deps_timeout: int | None = None,
    brief: str = "",
    posture: str | None = None,
) -> ProofResult:
    """Run an objective proof of the build. Always returns a ProofResult.

    ``execution_backend``: "auto" | "docker" | "inline". Structural file scans are
    local, but proof commands (boot import, generated tests, npm install/build/test)
    route through :class:`SandboxRunner` unless inline mode is requested.

    ``enable_mock_llm``: gate the mock-LLM proof seam (None -> the
    ``mock_llm_proof_enabled`` setting). When active for an LLM-calling project, a
    local deterministic OpenAI/Anthropic mock backs the project's OWN test step so
    it runs headlessly at $0 (build steps are never affected; degrade-open).

    ``posture``: "lab" | "release" (None -> the ``build_posture`` setting). Under
    "lab", failures that are about QUALITY rather than whether the app RUNS —
    failing LLM-authored tests, a ruff style failure, thin checklist coverage —
    are recorded in ``advisory_failures`` instead of flipping ``passed``.
    ``detail`` is populated identically, so ``error_gaps()`` still feeds the fix
    loop. Everything that proves the delivery is broken (syntax errors, no
    substantive files, a dead entrypoint, unresolved imports, a failed native
    build) blocks in BOTH postures.
    """
    pdir = Path(project_dir)
    checklist = checklist or []
    lab_posture = (
        _proof_posture_default() if posture is None else str(posture).strip().lower()
    ) == "lab"
    advisory_failures: list[str] = []

    mode = "local"
    cmd_ctx = _proof_command_context(execution_backend, stack)
    sandbox_available = _sandbox_available(cmd_ctx, execution_backend)
    cmd_ctx.docker_available = sandbox_available

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
    # If a checklist exists, require at least half of it present. The checklist
    # is LLM-authored filenames, so codegen legitimately renaming a file reads as
    # a coverage miss — advisory under lab posture.
    if checklist and present < max(1, len(checklist) // 2):
        if lab_posture:
            advisory_failures.append("checklist")
        else:
            passed = False

    detail: dict[str, Any] = {
        "stack": stack,
        "entrypoints": [],
        "sandbox_available": sandbox_available,
    }

    install_py_deps = (
        _proof_install_python_deps_default()
        if install_python_deps is None
        else bool(install_python_deps)
    )
    if install_py_deps and _should_install_python_deps(
        pdir, stack, run_tests=run_tests, run_build=run_build
    ):
        if _python_requirements_importable(pdir):
            detail["python_deps"] = "already_available"
        else:
            dep_timeout = (
                _proof_python_deps_timeout_default()
                if python_deps_timeout is None
                else int(python_deps_timeout)
            )
            ran_deps, deps_ok, deps_summary = _install_python_deps(
                pdir, cmd_ctx, timeout=dep_timeout)
            if ran_deps:
                detail["python_deps"] = "installed" if deps_ok else "failed"
                if deps_summary:
                    detail["python_deps_summary"] = deps_summary
            else:
                detail["python_deps"] = "skipped"
                if deps_summary:
                    detail["python_deps_summary"] = deps_summary

    # Behaviour, not vibes (rule #3): a code project that has no runnable
    # entrypoint, or whose entrypoint cannot even be imported, has NOT been
    # proven — a missing/broken root was the exact failure the old static-only
    # proof greenlit. Web/static stacks count index.html as an entrypoint.
    entrypoints, boot_error = _entrypoint_check(pdir, stack, cmd_ctx)
    detail["entrypoints"] = entrypoints
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
    if total > 0:
        # JS/TS relative imports AND local Python cross-module imports — both are
        # guaranteed runtime/boot failures the syntax pass + entrypoint smoke miss,
        # and both are exactly the cross-slice wiring breaks parallel slicing can
        # leave (a slice importing a sibling another slice failed to write).
        # Preserve this static diagnosis even when the entrypoint smoke has
        # already failed: repair needs the concrete missing module, not only a
        # truncated traceback.
        broken_imports = _unresolved_local_imports(pdir) + _unresolved_python_imports(pdir)
        if broken_imports:
            passed = False
            detail["unresolved_imports"] = broken_imports[:10]
            detail.setdefault("reason", f"{len(broken_imports)} unresolved local import(s)")
            if "<imports>" not in missing:
                missing = [*missing, "<imports>"]

    # Static/browser-only sites commonly have no compiler command.  Verify the
    # ESM linkage contract offline so a missing named export fails here instead
    # of shipping a blank page and waiting for responsive browser proof.
    if total > 0:
        export_mismatches = _esm_named_export_mismatches(pdir)
        if export_mismatches:
            passed = False
            detail["named_export_mismatches"] = export_mismatches[:10]
            detail.setdefault(
                "reason",
                f"{len(export_mismatches)} ESM named import/export mismatch(es)",
            )
            if "<exports>" not in missing:
                missing = [*missing, "<exports>"]

    # A page that references a file nobody wrote 404s on every load. Checked
    # BEFORE the unwired-components heuristic below because the two used to be
    # conflated: a delivered site whose index.html pointed at four scripts that
    # were never written was reported as "entry reaches none of the components",
    # so the fix loop was told to wire up components that were already wired,
    # and never repaired the actual dangling tags.
    if total > 0:
        dangling = _dangling_html_refs(pdir)
        if dangling:
            passed = False
            detail["dangling_html_refs"] = dangling
            detail.setdefault(
                "reason",
                f"{len(dangling)} HTML reference(s) point at files that do not exist",
            )
            if "<html-refs>" not in missing:
                missing = [*missing, "<html-refs>"]

    # Behaviour, not vibes (rule #3): a delivered app that ships real components
    # but an entry which reaches NONE of them renders an unwired/stub entry, not
    # the app — it has NOT been proven.
    if passed and total > 0:
        stub_note = _unwired_components(pdir)
        if stub_note:
            # A reachability HEURISTIC, not delivery integrity: it depends on
            # recognising an entry convention, and every unfamiliar project
            # shape re-arms a false accusation. Advisory under lab posture —
            # detail is populated identically, so extract_error_gaps still
            # emits UNWIRED ENTRY and _fix_loop still runs. Advisory means
            # "does not block", never "not repaired". The unambiguous,
            # mechanically-repairable version of this defect is caught by
            # _dangling_html_refs above, which blocks in both postures.
            if lab_posture:
                advisory_failures.append("unwired_components")
            else:
                passed = False
            detail["unwired_components"] = stub_note
            detail.setdefault("reason", stub_note)
            if "<wired-entry>" not in missing:
                missing = [*missing, "<wired-entry>"]

    # Mock-LLM proof seam: for an LLM-calling project, boot the local mock so the
    # generated app's OWN tests (below) run headlessly at $0. Started once here,
    # torn down in the finally - it wraps both Python and Node test steps.
    mock_enabled = enable_mock_llm if enable_mock_llm is not None else _mock_llm_default_enabled()
    mock_seam = (
        _start_proof_mock_llm(pdir, cmd_ctx, enabled=mock_enabled)
        if (run_tests or run_build) and passed else None
    )
    mock_env = mock_seam.env if mock_seam is not None else None
    if mock_seam is not None:
        detail["mock_llm"] = "active"
    try:
        # Behaviour, not vibes: when it boots, actually RUN the project's own tests.
        # A real failure fails the proof (and routes into the fix loop). Inability to
        # run them (no runner / deps / no tests) is a soft skip, never a hard fail.
        if run_tests and passed:
            ran, tests_passed, summary = _run_generated_tests(
                pdir, stack, test_timeout, cmd_ctx, extra_env=mock_env)
            if ran:
                detail["tests"] = "passed" if tests_passed else "failed"
                detail["test_summary"] = summary
                if not tests_passed:
                    # LLM-authored tests are wrong far more often than the app
                    # is. Under lab posture record and still feed the fix loop
                    # (detail is unchanged, so error_gaps() is identical) rather
                    # than failing a delivery over a bad generated assertion.
                    if lab_posture:
                        advisory_failures.append("tests")
                    else:
                        passed = False
                    if "<tests>" not in missing:
                        missing = [*missing, "<tests>"]
            else:
                detail["tests"] = "skipped"
                if summary:
                    detail["test_summary"] = summary

        # Behaviour, not vibes: compile an emitted project through its native
        # package workflow. Node/web uses npm and Python packages build an isolated
        # wheel; a static manifest check alone can greenlight a tree that cannot be
        # packaged. Soft-skips only when the tool/registry is unavailable. The
        # mock-LLM seam is NEVER passed to the build step — only test steps.
        if run_build and passed:
            # Swift takes its OWN branch (`swift build`); it is NOT a node stack, so it
            # must not go through the npm install/build path.
            if (stack or "").lower() in _SWIFT_IOS_STACKS:
                ran, build_ok, summary = _run_swift_ios_build(pdir, build_timeout, cmd_ctx)
                if ran:
                    detail["build"] = "passed" if build_ok else "failed"
                    detail["build_summary"] = summary
                    if not build_ok:
                        passed = False
                        if "<build>" not in missing:
                            missing = [*missing, "<build>"]
                    elif run_tests:
                        t_ran, t_ok, t_sum = _run_swift_ios_tests(pdir, test_timeout, cmd_ctx)
                        if t_ran:
                            detail["swift_ios_tests"] = "passed" if t_ok else "failed"
                            detail["swift_ios_tests_summary"] = t_sum
                            if not t_ok:
                                passed = False
                                if "<swift-ios-tests>" not in missing:
                                    missing = [*missing, "<swift-ios-tests>"]
                        elif t_sum:
                            detail["swift_ios_tests_summary"] = t_sum
                else:
                    detail.setdefault("build", "skipped")
                    if summary:
                        detail["build_summary"] = summary
            elif (stack or "").lower() in _SWIFT_STACKS:
                ran, build_ok, summary = _run_swift_build(pdir, build_timeout, cmd_ctx)
                if ran:
                    detail["build"] = "passed" if build_ok else "failed"
                    detail["build_summary"] = summary
                    if not build_ok:
                        passed = False
                        if "<build>" not in missing:
                            missing = [*missing, "<build>"]
                    elif run_tests:
                        t_ran, t_ok, t_sum = _run_swift_tests(pdir, test_timeout, cmd_ctx)
                        if t_ran:
                            detail["swift_tests"] = "passed" if t_ok else "failed"
                            detail["swift_tests_summary"] = t_sum
                            if not t_ok:
                                passed = False
                                if "<swift-tests>" not in missing:
                                    missing = [*missing, "<swift-tests>"]
                else:
                    detail.setdefault("build", "skipped")
                    if summary:
                        detail["build_summary"] = summary
            elif _is_python_package(pdir) and (
                (stack or "").lower() in _PYTHON_PROOF_STACKS or not stack
            ):
                ran, build_ok, summary = _run_python_package_build(
                    pdir, build_timeout, cmd_ctx
                )
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
            else:
                ran, build_ok, summary = _run_node_build(pdir, stack, build_timeout, cmd_ctx)
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

        # A delivered Python project that explicitly configures Ruff promises a
        # quality contract beyond syntax and package assembly. The deterministic
        # repair pass handles safe import/formatting fixes first; this gate catches
        # remaining lint errors and routes their exact file/line diagnostics into
        # the repair loop instead of certifying a failing `ruff check .` command.
        if run_build and passed and _ruff_is_configured(pdir):
            ran, ruff_ok, summary = _run_ruff_check(
                pdir,
                timeout=max(20, min(90, int(build_timeout) // 3)),
                cmd_ctx=cmd_ctx,
            )
            if ran:
                detail["ruff"] = "passed" if ruff_ok else "failed"
                detail["ruff_summary"] = summary
                if not ruff_ok:
                    # Lint style is not delivery integrity: a working app must
                    # not be undeliverable because of import order.
                    if lab_posture:
                        advisory_failures.append("ruff")
                    else:
                        passed = False
                    if "<ruff>" not in missing:
                        missing = [*missing, "<ruff>"]
            else:
                detail["ruff"] = "skipped"
                if summary:
                    detail["ruff_summary"] = summary

        # Node tests are an independent declared validation surface. They must
        # still run when a project has no build script or build execution was
        # disabled; tying them to a successful npm build silently skipped native
        # ``node --test`` suites and test-only packages.
        if (
            run_tests
            and passed
            and (pdir / "package.json").is_file()
            and (stack or "").lower() not in _SWIFT_STACKS
        ):
            t_ran, t_ok, t_sum = _run_node_tests(
                pdir, test_timeout, cmd_ctx, extra_env=mock_env)
            if t_ran:
                detail["node_tests"] = "passed" if t_ok else "failed"
                detail["node_tests_summary"] = t_sum
                if not t_ok:
                    # Same reasoning as the generated-test step above: a failing
                    # LLM-authored test is a repair signal, not a failed delivery.
                    if lab_posture:
                        advisory_failures.append("node_tests")
                    else:
                        passed = False
                    if "<node-tests>" not in missing:
                        missing = [*missing, "<node-tests>"]
            elif t_sum:
                detail["node_tests_summary"] = t_sum
    finally:
        if mock_seam is not None:
            mock_seam.stop()

    if cmd_ctx.used_backend:
        detail["sandbox_backend"] = cmd_ctx.used_backend
        if cmd_ctx.used_backend == "docker":
            mode = "sandbox"
        elif cmd_ctx.warnings:
            detail["sandbox_warning"] = cmd_ctx.warnings[-1]

    _attach_proof_environment(
        detail,
        execution_backend=execution_backend,
        cmd_ctx=cmd_ctx,
        sandbox_available=sandbox_available,
        run_tests=run_tests,
        run_build=run_build,
    )

    # Persisted-write receipts are DELIVERY CORRUPTION, not a quality opinion: a
    # file whose body is codegen history metadata instead of the real contents is
    # broken output, so this blocks in every posture. It used to sit inside the
    # advisory block below whose comment promised "NONE flips `passed`" — the
    # code and the comment contradicted each other. Split out so both are true.
    if total > 0:
        try:
            receipt_files = scan_persisted_write_receipts(pdir)
            if receipt_files:
                detail["persisted_write_receipts"] = receipt_files[:20]
                passed = False
        except Exception:  # noqa: BLE001 - scanner failure must not break proof
            pass

    # Anti-fake gap producers (advisory, wave-2 items 44/47/48): record
    # incomplete-code placeholders, brief features with no trace in the delivery,
    # and a shipped-scaffold-stub. NONE flips `passed` — they only add gaps the
    # fix-loop consumes + observability. Each is defensive; never breaks the proof.
    if total > 0:
        try:
            markers = scan_placeholder_markers(pdir)
            if markers:
                detail["placeholder_markers"] = markers[:20]
        except Exception:  # noqa: BLE001 - advisory, never break the proof
            pass
        if brief:
            try:
                feats = missing_brief_features(pdir, brief)
                if feats:
                    detail["missing_features"] = feats
            except Exception:  # noqa: BLE001
                pass
        try:
            stub_note = detect_scaffold_stub(pdir, stack, brief)
            if stub_note:
                detail["scaffold_stub"] = stub_note
        except Exception:  # noqa: BLE001
            pass

    return ProofResult(
        passed=passed,
        mode=mode,
        files_total=total,
        files_substantive=substantive,
        checklist_total=len(checklist),
        checklist_present=present,
        missing=missing,
        syntax_errors=syntax_errors,
        advisory_failures=advisory_failures,
        score=score,
        detail=detail,
    )


# Stacks for which a runnable entrypoint is expected before we call it "proven".
_CODE_STACKS = (
    "cli", "python", "fastapi", "flask", "django", "node", "express",
    "nextjs", "vue", "sveltekit", "react_ts",
)


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

        # ---- native iOS SwiftUI / Xcode -----------------------------------
        if low in _SWIFT_IOS_STACKS:
            project = pdir / "App.xcodeproj" / "project.pbxproj"
            info = pdir / "App" / "Info.plist"
            swift_srcs = [
                f for f in _iter_files(pdir)
                if f.suffix == ".swift" and "App" in f.relative_to(pdir).parts
                and f.stat().st_size >= _NONEMPTY
            ]
            if not project.exists() or project.stat().st_size < _NONEMPTY:
                return (True, False, "swift_ios stack: App.xcodeproj/project.pbxproj missing")
            if not info.exists() or info.stat().st_size < _NONEMPTY:
                return (True, False, "swift_ios stack: App/Info.plist missing")
            if not swift_srcs:
                return (True, False, "swift_ios stack: no App/**/*.swift sources found")
            try:
                pbx = project.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pbx = ""
            if "PBXNativeTarget" not in pbx or "IPHONEOS_DEPLOYMENT_TARGET" not in pbx:
                return (True, False, "swift_ios stack: project lacks an iOS app target")
            return (True, True, "swift_ios stack: Xcode target + Info.plist + Swift sources present")

        # ---- swift (SwiftUI / Swift Package Manager) ---------------------
        if low == "swift":
            manifest = pdir / "Package.swift"
            if not manifest.exists() or manifest.stat().st_size < _NONEMPTY:
                return (True, False, "swift stack: Package.swift missing")
            swift_srcs = [
                f for f in _iter_files(pdir)
                if f.suffix == ".swift"
                and f.name != "Package.swift"
                and "Sources" in f.relative_to(pdir).parts
                and f.stat().st_size >= _NONEMPTY
            ]
            if not swift_srcs:
                return (True, False, "swift stack: no Sources/**/*.swift found")
            return (True, True, "swift stack: Package.swift + Sources present")

        # ---- mcp (Python stdio Model Context Protocol server) -----------
        if low == "mcp":
            server = pdir / "server.py"
            if not server.exists() or server.stat().st_size < _NONEMPTY:
                return (True, False, "mcp stack: server.py missing")
            try:
                text = server.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                text = ""
            # The server must actually speak MCP (reference the SDK) — a bare
            # server.py that isn't an MCP server does NOT satisfy this stack.
            if "mcp" not in text:
                return (True, False, "mcp stack: server.py does not reference the mcp SDK")
            return (True, True, "mcp stack: server.py present and references the mcp SDK")

        # ---- rag (FastAPI chat-with-your-documents app) -------------------
        if low == "rag":
            main_py = pdir / "main.py"
            if not main_py.exists() or main_py.stat().st_size < _NONEMPTY:
                return (True, False, "rag stack: main.py missing")
            try:
                text = main_py.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                text = ""
            # The app must actually be a FastAPI server — a bare main.py that
            # isn't an HTTP app does NOT satisfy this stack.
            if "fastapi" not in text:
                return (True, False, "rag stack: main.py does not reference fastapi")
            # The pure retrieval core is the stack's load-bearing split (the
            # sim-core pattern): its absence means untestable, tangled retrieval.
            core = pdir / "rag_core.py"
            if not core.exists() or core.stat().st_size < _NONEMPTY:
                return (True, False, "rag stack: rag_core.py (the pure retrieval core) missing")
            return (True, True, "rag stack: FastAPI main.py + rag_core.py present")

        # ---- workflow (FastAPI agent-workflow runner) ----------------------
        if low == "workflow":
            main_py = pdir / "main.py"
            if not main_py.exists() or main_py.stat().st_size < _NONEMPTY:
                return (True, False, "workflow stack: main.py missing")
            try:
                text = main_py.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                text = ""
            if "fastapi" not in text:
                return (True, False, "workflow stack: main.py does not reference fastapi")
            # The pure engine is the stack's load-bearing split (the sim-core
            # pattern): its absence means an untestable, tangled runner.
            core = pdir / "workflow_core.py"
            if not core.exists() or core.stat().st_size < _NONEMPTY:
                return (True, False,
                        "workflow stack: workflow_core.py (the pure engine) missing")
            return (True, True, "workflow stack: FastAPI main.py + workflow_core.py present")

        # ---- agent_pack (persona roster: markdown + python tooling) --------
        if low == "agent_pack":
            catalog = pdir / "catalog.json"
            if not catalog.exists() or catalog.stat().st_size < _NONEMPTY:
                return (True, False, "agent_pack stack: catalog.json missing")
            personas = [
                f for f in _iter_files(pdir)
                if f.suffix == ".md"
                and "agents" in f.relative_to(pdir).parts
                and f.stat().st_size >= _NONEMPTY
            ]
            if not personas:
                return (True, False,
                        "agent_pack stack: no agents/<division>/*.md persona files")
            tools = pdir / "pack_tools.py"
            if not tools.exists() or tools.stat().st_size < _NONEMPTY:
                return (True, False,
                        "agent_pack stack: pack_tools.py (lint/convert tooling) missing")
            return (True, True,
                    f"agent_pack stack: catalog + {len(personas)} persona(s) + tooling present")

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
        if low in ("nextjs", "astro", "remix", "vue", "sveltekit", "react_ts"):
            pkg = pdir / "package.json"
            if not pkg.exists():
                return (True, False, f"{low} stack: package.json missing")
            try:
                pkg_text = pkg.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                pkg_text = ""
            framework_dep = {
                "nextjs": "next",
                "astro": "astro",
                "remix": "@remix-run",
                "vue": "vue",
                "sveltekit": "@sveltejs/kit",
                "react_ts": "typescript",
            }[low]
            if framework_dep not in pkg_text:
                return (True, False, f"{low} stack: package.json does not depend on {framework_dep}")
            names = {f.name for f in _iter_files(pdir) if f.stat().st_size >= _NONEMPTY}
            # An entry/config artifact unique to the framework.
            markers = {
                "nextjs": ("page.jsx", "page.tsx", "next.config.js", "next.config.mjs"),
                "astro": ("astro.config.mjs", "astro.config.ts", "index.astro"),
                "remix": ("root.tsx", "root.jsx", "_index.tsx", "vite.config.ts"),
                "vue": ("App.vue", "main.ts", "vite.config.ts"),
                "sveltekit": ("+page.svelte", "svelte.config.js", "vite.config.ts"),
                "react_ts": ("App.tsx", "main.tsx", "vite.config.ts"),
            }[low]
            if not any(m in names for m in markers):
                return (True, False, f"{low} stack: no entry/config artifact ({', '.join(markers)}) found")
            return (True, True, f"{low} stack: package.json + framework entry present")

    except Exception:  # noqa: BLE001 — never let a stack check crash proof
        return (False, False, "stack check failed unexpectedly — fell back to generic")

    # Unknown/unrecognised stack: signal to the caller to use generic path.
    return (False, False, "")


def _entrypoint_check(
    pdir: Path,
    stack: str,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[list[str], str]:
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
    is_python = low in (
        "cli", "python", "fastapi", "flask", "django", "rag", "workflow",
        "agent_pack",
    ) or any(
        e.endswith(".py") for e in entrypoints
    )
    if not is_python:
        return (entrypoints, "")  # node/web: presence is the local-mode signal

    py = _python_executable()
    target = next((e for e in entrypoints if e.endswith(".py")), None)
    use_container_names = _use_container_command_names(cmd_ctx)
    if py is None and not use_container_names:
        return (entrypoints, "")  # no runtime -> presence-only (already syntax-checked)
    if target is None:
        return (entrypoints, "")  # no runtime -> presence-only (already syntax-checked)

    module = target[:-3].replace("/", ".")
    import os

    # -B + PYTHONDONTWRITEBYTECODE so the smoke import never leaves a
    # __pycache__/.pyc inside the delivered artifact (the proof must not pollute
    # what it proves).
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    py_cmd = "python" if use_container_names else str(py)
    project_paths = _python_project_paths(pdir, use_container_names=use_container_names)
    proc = _run_proof_command(
        cmd_ctx,
        [py_cmd, "-B", "-c",
         f"import sys; sys.path[:0] = {project_paths!r}; import importlib; importlib.import_module({module!r})"],
        cwd=pdir,
        timeout=20,
        env=env,
    )
    if proc.timed_out:
        # A hung/uncrunnable import is indeterminate, not a pass.
        return (entrypoints, "entrypoint import did not complete (hung or unrunnable)")
    if proc.returncode == 0:
        return (entrypoints, "")
    tail = (proc.stderr or proc.stdout or "")[-400:]
    # Missing third-party deps are not a code defect (offline env); the syntax
    # pass already validated the source compiles. An import failure from a
    # package that is part of the delivered tree, however, is a real wiring
    # failure and must not be hidden as an unavailable dependency.
    if "ModuleNotFoundError" in tail or "ImportError" in tail:
        if not _is_project_python_import_error(tail, pdir):
            return (entrypoints, "")
    return (entrypoints, tail.strip())


def _python_executable() -> str | None:
    """Use SkyN3t's interpreter before falling back to ambient ``PATH``.

    The generated project's dependencies and proof tools are installed alongside
    the running SkyN3t process. On Windows, ``python`` can otherwise resolve to
    an unrelated global interpreter (or the Store shim), which makes a healthy
    generated project look unavailable or test it against the wrong runtime.
    """
    if sys.executable:
        return sys.executable
    return shutil.which("python") or shutil.which("python3")


def _has_python_tests(pdir: Path) -> bool:
    for f in _iter_files(pdir):
        if f.suffix != ".py":
            continue
        name = f.name
        if name.startswith("test_") or name.endswith("_test.py") or "tests" in f.relative_to(pdir).parts:
            return True
    return False


def _python_project_paths(pdir: Path, *, use_container_names: bool) -> list[str]:
    """Return import roots for a generated Python project.

    Generated apps commonly use a ``src/`` layout. Both the entrypoint smoke
    check and pytest need that directory ahead of the project root; otherwise a
    healthy package is reported as missing and a broken package can be soft-
    skipped as an unavailable third-party dependency.
    """
    if use_container_names:
        paths = ["src"] if (pdir / "src").is_dir() else []
        return [*paths, "."]
    paths = [str(pdir / "src")] if (pdir / "src").is_dir() else []
    return [*paths, str(pdir)]


def _project_python_import_roots(pdir: Path) -> set[str]:
    """Top-level import names supplied by the delivered Python tree."""
    roots: set[str] = set()
    for base in (pdir, pdir / "src"):
        try:
            if not base.is_dir():
                continue
            for child in base.iterdir():
                name = child.name
                if not name or name.startswith(".") or name == "__pycache__":
                    continue
                if child.is_dir() and (child / "__init__.py").is_file():
                    roots.add(name)
                elif child.is_file() and child.suffix == ".py" and child.stem != "__init__":
                    roots.add(child.stem)
        except OSError:
            continue
    return roots


_MISSING_PYTHON_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError): No module named ['\"]([^'\"]+)['\"]"
)
_PYTHON_IMPORT_FROM_RE = re.compile(
    r"ImportError: cannot import name .*? from ['\"]([^'\"]+)['\"]"
)


def _is_project_python_import_error(output: str, pdir: Path) -> bool:
    """Whether an import traceback names an import supplied by this project.

    Collection errors from an absent external dependency remain an honest soft
    skip in an offline proof environment. A missing local package or a bad
    symbol import from one is code the project itself delivered, so it must be a
    proof failure and reach the repair loop.
    """
    roots = _project_python_import_roots(pdir)
    if not roots:
        return False
    modules = [
        *(_MISSING_PYTHON_MODULE_RE.findall(output or "")),
        *(_PYTHON_IMPORT_FROM_RE.findall(output or "")),
    ]
    return any(module.split(".", 1)[0] in roots for module in modules)


def _run_generated_tests(
    pdir: Path,
    stack: str,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, bool, str]:
    """Run the generated project's own test suite.

    Returns ``(ran, passed, summary)``. ``ran=False`` is a soft skip (no test
    runner, no deps, or no tests) and must NOT fail the proof. ``extra_env`` layers
    the mock-LLM seam's base-url + dummy-key overlay onto the test env. Never raises.
    """
    import os

    py = _python_executable()
    low = (stack or "").lower()
    is_python = low in (
        "cli", "python", "fastapi", "flask", "django", "rag", "workflow", "agent_pack")
    use_container_names = _use_container_command_names(cmd_ctx)
    if py is None and not use_container_names:
        return (False, False, "")
    if not (is_python or _has_python_tests(pdir)):
        return (False, False, "")  # node/web tests need an install step we skip
    if not _has_python_tests(pdir):
        return (False, False, "")

    extra = dict(extra_env or {})
    inherited_pythonpath = str(extra.pop("PYTHONPATH", "") or "")
    separator = ":" if use_container_names else os.pathsep
    project_pythonpath = separator.join(
        [*_python_project_paths(pdir, use_container_names=use_container_names),
         *([inherited_pythonpath] if inherited_pythonpath else [])]
    )
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": project_pythonpath,
        **extra,
    }
    py_cmd = "python" if use_container_names else str(py)
    # pytest must be importable; otherwise soft-skip rather than fail a build for
    # a missing dev tool in the environment.
    probe = _run_proof_command(
        cmd_ctx, [py_cmd, "-c", "import pytest"], cwd=pdir, timeout=15, env=env)
    if probe.timed_out or probe.returncode == 127:
        return (False, False, "")
    if probe.returncode != 0:
        if use_container_names:
            target = "/work/.skyn3t-pytest"
            host_target = pdir / ".skyn3t-pytest"
            if not host_target.exists():
                install = _run_proof_command(
                    cmd_ctx,
                    [
                        py_cmd,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--target",
                        target,
                        "pytest",
                    ],
                    cwd=pdir,
                    timeout=120,
                    env=env,
                    network=True,
                )
                if install.timed_out:
                    return (False, False, "pytest install timed out — tests skipped")
                if install.returncode != 0:
                    out = ((install.stdout or "") + (install.stderr or "")).strip()
                    return (False, False, (out[-500:] or "pytest install failed — tests skipped"))
            env["PYTHONPATH"] = f"{target}:{env.get('PYTHONPATH', '.')}"
            probe = _run_proof_command(
                cmd_ctx, [py_cmd, "-c", "import pytest"], cwd=pdir, timeout=15, env=env)
            if probe.returncode != 0:
                return (False, False, "pytest not installed — tests skipped")
        else:
            return (False, False, "pytest not installed — tests skipped")

    proc = _run_proof_command(
        cmd_ctx,
        [py_cmd, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "."],
        cwd=pdir,
        timeout=timeout,
        env=env,
    )
    if proc.timed_out:
        return (True, False, f"tests timed out after {timeout}s")
    if proc.returncode == 127:
        return (False, False, "")

    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = out[-500:]
    rc = proc.returncode
    if rc == 0:
        return (True, True, tail)
    if rc == 5:
        return (False, False, "no tests collected")  # soft skip
    # Collection/import errors from missing third-party deps aren't a code defect.
    # Do not apply that soft-skip to a package delivered in this project: that is
    # a source-layout or symbol-wiring defect and needs a real repair.
    if rc in (2, 3, 4) and ("ModuleNotFoundError" in out or "ImportError" in out):
        if not _is_project_python_import_error(out, pdir):
            return (False, False, "tests need uninstalled deps — skipped")
    return (True, False, tail)


# react_native is a node stack for proof purposes: its package.json ships a
# `typecheck` script (tsc --noEmit), so _run_node_build proves it with a type
# check rather than a long-running Expo dev server.
_NODE_STACKS = (
    "react", "react_vite", "react_native", "node", "node_express", "express",
    "nextjs", "astro", "remix", "vue", "sveltekit", "react_ts", "static",
    # Tauri desktop: the frontend is a Vite/React app — `npm run build` builds it
    # (the proof); the fixed src-tauri/ Rust shell is bundled separately.
    "tauri", "desktop",
    # Phaser 3 game: a Vite npm build (`vite build` -> dist/) is the proof.
    "phaser",
)


_NODE_TEST_RUNNERS = frozenset({
    "vitest",
    "jest",
    "mocha",
    "@testing-library/react",
    "@testing-library/vue",
    "ava",
    "@playwright/test",
    "playwright",
    "cypress",
    "tap",
    "uvu",
})
_NODE_TEST_COMMANDS = frozenset({
    "vitest",
    "jest",
    "mocha",
    "ava",
    "playwright",
    "cypress",
    "tap",
    "uvu",
})


def package_declares_node_tests(package: dict[str, Any]) -> bool:
    """Return whether ``package.json`` declares an executable Node test suite.

    Dependencies alone remain useful evidence for conventional runners, while
    command inspection covers dependency-free ``node --test`` and scripts that
    invoke Playwright/Cypress/tap/uvu through wrappers such as ``npx``.
    """
    scripts = package.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    command = str(scripts.get("test") or "").strip().lower()
    if not command:
        return False
    dependencies = {
        str(name).strip().lower()
        for section in ("dependencies", "devDependencies", "optionalDependencies")
        if isinstance(package.get(section), dict)
        for name in package[section]
    }
    if dependencies.intersection(_NODE_TEST_RUNNERS):
        return True
    if re.search(r"\bnode(?:\.exe)?\s+--test(?:\s|$)", command):
        return True
    command_tokens = {
        token
        for token in re.split(r"[^a-z0-9@/_.-]+", command)
        if token
    }
    return bool(command_tokens.intersection(_NODE_TEST_COMMANDS))


def _native_node_test_script(package: dict[str, Any]) -> bool:
    scripts = package.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    return bool(
        re.search(
            r"\bnode(?:\.exe)?\s+--test(?:\s|$)",
            str(scripts.get("test") or "").strip().lower(),
        )
    )


def _run_node_tests(
    pdir: Path,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, bool, str]:
    """Run the project's JS/TS test script when a real runner is declared.

    Returns ``(ran, passed, summary)``. When a suite actually runs, its result
    is objective proof evidence and callers gate delivery on a failure.

    ``ran=False`` (no runner / no script / launch failure / timeout) means
    'could not assess' and remains a soft skip. Runs under CI=1 so vitest/jest
    execute once instead of entering watch mode. Never raises."""
    import json as _json
    import os
    import shutil

    npm = shutil.which("npm")
    pkg_path = pdir / "package.json"
    use_container_names = _use_container_command_names(cmd_ctx)
    if npm is None and not use_container_names:
        return (False, False, "")
    if not pkg_path.exists():
        return (False, False, "")
    try:
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return (False, False, "")
    if not isinstance(pkg, dict) or "test" not in (pkg.get("scripts") or {}):
        return (False, False, "no test script")
    if not package_declares_node_tests(pkg):
        return (False, False, "no recognized test runner")
    if not (pdir / "node_modules").is_dir() and not _native_node_test_script(pkg):
        return (False, False, "test dependencies are not installed")
    env = {**os.environ, "CI": "1", "npm_config_audit": "false",
           "npm_config_fund": "false", **(extra_env or {})}
    npm_cmd = "npm" if use_container_names else str(npm)
    res = _run_proof_command(
        cmd_ctx, [npm_cmd, "test", "--silent"], cwd=pdir, timeout=timeout, env=env)
    if res.timed_out:
        return (True, False, "node tests timed out")
    if res.returncode == 127:
        return (False, False, "node tests failed to launch")
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


_CONTAINER_HOME = "/work/node_modules/.skyn3t-home"
_CONTAINER_CACHE_HOME = "/work/node_modules/.skyn3t-cache"
_CONTAINER_NPM_CACHE = "/work/node_modules/.skyn3t-npm-cache"
_CONTAINER_NPM_TMP = "/work/node_modules/.skyn3t-tmp"


def _node_build_env(*, container: bool = False) -> dict:
    """Build-time env for npm: CI flags + placeholder provider keys so a top-level
    SDK client init doesn't crash the build on a missing key (real key set at serve)."""
    env = npm_env()
    if container:
        # Host paths such as /Users/... do not exist inside linux containers.
        # Keep npm/Next/Rollup cache writes under node_modules so failed installs
        # can be discarded with the partial dependency tree.
        env["HOME"] = _CONTAINER_HOME
        env["XDG_CACHE_HOME"] = _CONTAINER_CACHE_HOME
        env["npm_config_cache"] = _CONTAINER_NPM_CACHE
        env["npm_config_tmp"] = _CONTAINER_NPM_TMP
    for k in _BUILD_PLACEHOLDER_KEYS:
        env.setdefault(k, "sk-build-placeholder")
    return env


def _node_npm_install_args(npm_cmd: str, action: str = "install", *, container: bool = False) -> list[str]:
    args = npm_install_args(npm_cmd, action)
    if not container:
        return args
    out: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--cache":
            skip_next = True
            continue
        out.append(arg)
    out += ["--cache", _CONTAINER_NPM_CACHE]
    return out


def _docker_npm_install_stamp_path(pdir: Path) -> Path:
    return npm_docker_install_stamp_path(pdir)


def _node_install_current(pdir: Path, *, container: bool = False) -> bool:
    if not container:
        return npm_install_current(pdir)
    stamp = _docker_npm_install_stamp_path(pdir)
    if not (pdir / "package.json").is_file() or not (pdir / "node_modules").is_dir():
        return False
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("fingerprint") == npm_install_fingerprint(pdir)


def _mark_node_install_current(pdir: Path, *, container: bool = False) -> None:
    if not container:
        mark_npm_install_current(pdir)
        return
    stamp = _docker_npm_install_stamp_path(pdir)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(
        json.dumps(
            {
                "fingerprint": npm_install_fingerprint(pdir),
                "backend": "docker",
                "container_os": "linux",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _discard_node_modules(pdir: Path) -> None:
    try:
        shutil.rmtree(pdir / "node_modules")
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 - cleanup must not hide the install failure
        pass


def _prepare_node_dependencies(
    pdir: Path,
    *,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[bool, bool, str]:
    """Install declared Node dependencies and stabilize the lockfile once."""
    npm = shutil.which("npm")
    use_container_names = _use_container_command_names(cmd_ctx)
    if npm is None and not use_container_names:
        return (False, False, "npm could not be launched")
    npm_cmd = "npm" if use_container_names else str(npm)
    env = _node_build_env(container=use_container_names)
    foreign_deps = "" if use_container_names else discard_foreign_node_modules(pdir)
    if _node_install_current(pdir, container=use_container_names):
        return (True, True, "npm install skipped (dependencies current)")

    inst = _run_proof_command(
        cmd_ctx,
        _node_npm_install_args(npm_cmd, "install", container=use_container_names),
        cwd=pdir,
        timeout=max(1, int(timeout)),
        env=env,
        network=True,
    )
    if inst.timed_out:
        _discard_node_modules(pdir)
        return (True, False, f"npm install timed out after {timeout}s")
    if inst.returncode == 127:
        return (False, False, "npm install could not be launched")
    if inst.returncode != 0:
        out = ((inst.stdout or "") + (inst.stderr or "")).strip()
        _discard_node_modules(pdir)
        if _npm_install_is_offline(out):
            return (False, False, "npm install failed (offline registry)")
        return (True, False, out[-700:])
    _mark_node_install_current(pdir, container=use_container_names)
    summary = "npm install ok"
    if foreign_deps:
        summary = f"{summary} after replacing {foreign_deps} node_modules"
    return (True, True, summary)


def stabilize_node_dependencies(
    project_dir: str | Path,
    *,
    execution_backend: str = "auto",
    stack: str = "",
    timeout: int = 300,
    run_tests: bool = True,
    run_build: bool = True,
) -> tuple[bool, bool, str]:
    """Install dependencies before a caller binds a source-tree digest.

    ``npm install`` is allowed to create or normalize a lockfile. Reverify uses
    this phase before its candidate snapshot, so the later proof remains strict
    about every source mutation without falsely blaming the expected lockfile.
    """
    pdir = Path(project_dir)
    package_path = pdir / "package.json"
    if not package_path.is_file() or not (run_tests or run_build):
        return (False, False, "no Node dependency phase required")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return (True, False, "package.json could not be parsed")
    if not isinstance(package, dict):
        return (True, False, "package.json is not an object")
    invalid_names = _invalid_npm_package_names(package)
    if invalid_names:
        return (True, False, "invalid npm package names: " + ", ".join(invalid_names[:8]))
    scripts = package.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    needs_build = run_build and any(name in scripts for name in ("build", "typecheck", "check"))
    needs_tests = run_tests and package_declares_node_tests(package)
    if not (needs_build or needs_tests):
        return (False, False, "no declared Node build or test command")

    cmd_ctx = _proof_command_context(execution_backend, stack)
    cmd_ctx.docker_available = _sandbox_available(cmd_ctx, execution_backend)
    install_budget = max(120, int(timeout * 0.6))
    return _prepare_node_dependencies(
        pdir,
        timeout=install_budget,
        cmd_ctx=cmd_ctx,
    )


def _run_node_build(
    pdir: Path,
    stack: str,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[bool, bool, str]:
    """Compile a node/web project for real: npm install + npm run build.

    Returns ``(ran, passed, summary)``. ``ran=False`` is a soft skip (no npm, no
    build script, or the install failed — e.g. offline) and must NOT fail the
    proof. A non-zero build IS a real failure. Never raises.
    """
    import json as _json
    import shutil

    pkg_path = pdir / "package.json"
    low = (stack or "").lower()
    if low not in _NODE_STACKS and not pkg_path.exists():
        return (False, False, "")
    npm = shutil.which("npm")
    use_container_names = _use_container_command_names(cmd_ctx)
    if npm is None and not use_container_names:
        return (False, False, "npm or package.json missing — build skipped")
    if not pkg_path.exists():
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

    env = _node_build_env(container=use_container_names)
    npm_cmd = "npm" if use_container_names else str(npm)
    # Install (bounded). A non-zero install is a REAL, build-breaking failure
    # (ERESOLVE / E404 / ETARGET / bad name) and must fail the proof so the
    # fix-loop sees the error. ONLY a genuine connectivity failure (offline
    # registry) soft-skips. Floor the budget at 120s so a slow-but-valid install
    # isn't starved into a false timeout.
    install_budget = max(120, int(timeout * 0.6))
    install_ran, install_ok, install_summary = _prepare_node_dependencies(
        pdir,
        timeout=install_budget,
        cmd_ctx=cmd_ctx,
    )
    if not install_ok:
        if not install_ran:
            return (False, False, f"{install_summary} — build skipped")
        return (True, False, install_summary)

    summaries = [install_summary]
    if npm_build_current(pdir, build_cmd):
        summaries.append(f"npm run {build_cmd} skipped (build current)")
    else:
        bld = _run_proof_command(
            cmd_ctx,
            [npm_cmd, "run", build_cmd],
            cwd=pdir,
            timeout=max(30, timeout - install_budget),
            env=env,
        )
        if bld.timed_out:
            return (True, False, f"npm run {build_cmd} timed out after build budget")
        if bld.returncode == 127:
            return (False, False, "")
        out = ((bld.stdout or "") + (bld.stderr or "")).strip()
        if bld.returncode != 0:
            # Surface file/symbol diagnostics so the fix-loop targets the real
            # compiler cause instead of receiving an unhelpful output tail.
            return (True, False, _distill_build_errors(out))
        mark_npm_build_current(pdir, build_cmd)
        summaries.append(out[-300:] if out else f"npm run {build_cmd} ok")

    # A production build can transpile while strict framework/TypeScript checks
    # still fail. Honor a separately declared validation script instead of
    # certifying the app from the build alone. Prefer the explicit typecheck
    # command; Astro projects conventionally expose the equivalent as `check`.
    validation_cmd = None
    if build_cmd != "typecheck" and "typecheck" in scripts:
        validation_cmd = "typecheck"
    elif build_cmd != "check" and "check" in scripts:
        validation_cmd = "check"
    if validation_cmd:
        validation = _run_proof_command(
            cmd_ctx,
            [npm_cmd, "run", validation_cmd],
            cwd=pdir,
            timeout=max(30, min(120, timeout // 3)),
            env=env,
        )
        if validation.timed_out:
            return (True, False, f"npm run {validation_cmd} timed out")
        if validation.returncode == 127:
            return (False, False, "")
        validation_out = (
            (validation.stdout or "") + (validation.stderr or "")
        ).strip()
        incomplete_prompt = (
            "requires the following dependency to be installed" in validation_out
            or "Continue?" in validation_out
        )
        if validation.returncode != 0 or incomplete_prompt:
            return (True, False, _distill_build_errors(validation_out))
        summaries.append(
            validation_out[-300:]
            if validation_out
            else f"npm run {validation_cmd} ok"
        )

    return (True, True, "; ".join(part for part in summaries if part))


def _is_python_package(pdir: Path) -> bool:
    """Whether the delivered tree declares a buildable Python package."""
    if (pdir / "setup.py").is_file() or (pdir / "setup.cfg").is_file():
        return True
    pyproject = pdir / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        import tomllib
    except ImportError:  # pragma: no cover - SkyN3t requires Python 3.11+
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    return isinstance(data.get("build-system"), dict) or isinstance(data.get("project"), dict)


def _run_python_package_build(
    pdir: Path,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[bool, bool, str]:
    """Build a Python package wheel without leaving artifacts in delivery.

    A temporary staged copy lives under the project so it is visible to a Docker
    proof mount, then is removed unconditionally. That prevents setuptools and
    other PEP 517 backends from leaving ``*.egg-info`` or build metadata in the
    delivered source. PEP 517 isolation remains enabled: it proves the package's
    declared build backend rather than borrowing SkyN3t's development environment.
    """
    if not _is_python_package(pdir):
        return (False, False, "no Python package metadata — build skipped")
    py = _python_executable()
    use_container_names = _use_container_command_names(cmd_ctx)
    if py is None and not use_container_names:
        return (False, False, "python interpreter missing — build skipped")
    try:
        import tempfile

        proof_root = Path(tempfile.mkdtemp(prefix=".skyn3t-wheel-proof-", dir=pdir))
    except OSError:
        return (False, False, "could not prepare Python wheel output — build skipped")
    try:
        staged_project = proof_root / "project"
        # The command backend mounts ``staged_project`` as the container
        # workspace. A sibling ``../wheels`` therefore resolves to the
        # container's read-only root, not to ``proof_root`` on the host.
        # Keep wheel output inside the staged copy; the entire proof root is
        # removed below, so delivery remains artifact-free.
        wheel_dir = staged_project / ".skyn3t-wheels"
        try:
            shutil.copytree(
                pdir,
                staged_project,
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    *_NON_SOURCE_DIRS,
                    ".skyn3t-wheel-proof-*",
                    "*.egg-info",
                ),
            )
            wheel_dir.mkdir()
        except OSError:
            return (False, False, "could not stage Python package build — build skipped")
        py_cmd = "python" if use_container_names else str(py)
        result = _run_proof_command(
            cmd_ctx,
            [
                py_cmd,
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-deps",
                "--wheel-dir",
                ".skyn3t-wheels",
                ".",
            ],
            cwd=staged_project,
            timeout=max(30, int(timeout)),
            env={
                **os.environ,
                "PIP_NO_INPUT": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            network=True,
        )
        if result.timed_out:
            return (True, False, f"pip wheel timed out after {max(30, int(timeout))}s")
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode == 127:
            return (False, False, "pip could not be launched — build skipped")
        if result.returncode != 0:
            if _pip_build_is_offline(output):
                return (False, False, "pip wheel failed (offline registry) — build skipped")
            return (True, False, output[-700:] or "pip wheel failed")
        if not any(wheel_dir.glob("*.whl")):
            return (True, False, "pip wheel reported success but produced no wheel")
        return (True, True, output[-300:] or "pip wheel ok")
    finally:
        shutil.rmtree(proof_root, ignore_errors=True)


def _run_ruff_check(
    pdir: Path,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[bool, bool, str]:
    """Run an opted-in project's Ruff contract through the proof command path."""
    if not _ruff_is_configured(pdir):
        return (False, False, "no Ruff configuration")
    use_container_names = _use_container_command_names(cmd_ctx)
    if use_container_names:
        command = ["ruff", "check", "--no-cache", "."]
    else:
        try:
            from ruff import find_ruff_bin
        except ImportError:
            return (False, False, "Ruff is unavailable — quality check skipped")
        command = [str(find_ruff_bin()), "check", "--no-cache", "."]
    result = _run_proof_command(
        cmd_ctx,
        command,
        cwd=pdir,
        timeout=max(10, int(timeout)),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.timed_out:
        return (True, False, f"ruff check timed out after {max(10, int(timeout))}s")
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode == 127:
        return (False, False, "Ruff could not be launched — quality check skipped")
    if result.returncode != 0:
        return (True, False, output[-700:] or "ruff check failed")
    return (True, True, output[-300:] or "ruff check passed")


# Swift is NOT a node stack: it has no package.json and builds via Swift Package
# Manager (`swift build`), not npm. Its own proof branch keeps it out of the
# node install/build path entirely.
_SWIFT_STACKS = ("swift",)
# Native iOS uses Xcode rather than SwiftPM. Keep a separate family so proof
# never routes a simulator build through swift build or npm.
_SWIFT_IOS_STACKS = ("swift_ios",)


def _run_swift_build(
    pdir: Path,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[bool, bool, str]:
    """Compile a Swift Package Manager project for real: ``swift build``.

    Returns ``(ran, passed, summary)``. ``ran=False`` is a soft skip (no swift
    toolchain, or it could not be launched — e.g. a CI box without Xcode) and
    must NOT fail the proof, so other machines don't false-fail. A non-zero build
    IS a real, delivery-breaking failure. Routes through the SAME command layer as
    the node build (SandboxRunner when configured, hardened local subprocess as
    the fallback). Never raises.
    """
    import shutil

    manifest = pdir / "Package.swift"
    use_container_names = _use_container_command_names(cmd_ctx)
    swift = shutil.which("swift")
    if swift is None and not use_container_names:
        return (False, False, "swift toolchain missing — build skipped")
    if not manifest.exists():
        return (False, False, "no Package.swift — build skipped")
    swift_cmd = "swift" if use_container_names else str(swift or "swift")
    swift_env = dict(os.environ)
    module_cache = pdir / ".skyn3t-swift-module-cache"
    try:
        module_cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    swift_env.setdefault("CLANG_MODULE_CACHE_PATH", str(module_cache))
    # `swift build` resolves the package graph + compiles. The scaffold ships no
    # external SwiftPM deps (compiles offline), but allow the network in case
    # codegen adds package dependencies.
    bld = _run_proof_command(
        cmd_ctx, [swift_cmd, "build"], cwd=pdir, timeout=timeout, env=swift_env,
        network=True)
    if bld.timed_out:
        return (True, False, f"swift build timed out after {timeout}s")
    if bld.returncode == 127:
        # swift could not even be launched -> environmental, soft-skip.
        return (False, False, "swift could not be launched — build skipped")
    out = ((bld.stdout or "") + (bld.stderr or "")).strip()
    if "sandbox-exec: sandbox_apply: Operation not permitted" in out:
        return (False, False, "swift sandbox-exec blocked by host sandbox — build skipped")
    if "ModuleCache: Operation not permitted" in out:
        return (False, False, "swift module cache blocked by host sandbox — build skipped")
    if bld.returncode == 0:
        return (True, True, out[-300:])
    return (True, False, out[-700:])


def _ios_xcode_project(pdir: Path) -> Path | None:
    preferred = pdir / "App.xcodeproj"
    if (preferred / "project.pbxproj").is_file():
        return preferred
    for candidate in pdir.glob("*.xcodeproj"):
        if (candidate / "project.pbxproj").is_file():
            return candidate
    return None


def _run_swift_ios_build(
    pdir: Path,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[bool, bool, str]:
    """Build a native iOS target with the simulator SDK when a Mac provides Xcode."""
    import shutil

    if _use_container_command_names(cmd_ctx):
        return (False, False, "xcodebuild requires a macOS host — build skipped")
    xcodebuild = shutil.which("xcodebuild")
    project = _ios_xcode_project(pdir)
    if xcodebuild is None:
        return (False, False, "xcodebuild missing — native iOS build skipped")
    if project is None:
        return (False, False, "no .xcodeproj — native iOS build skipped")
    result = _run_proof_command(
        cmd_ctx,
        [str(xcodebuild), "-project", project.name, "-scheme", "App", "-sdk",
         "iphonesimulator", "-destination", "generic/platform=iOS Simulator", "build"],
        cwd=pdir, timeout=timeout, env=dict(os.environ), network=False,
    )
    if result.timed_out:
        return (True, False, f"xcodebuild timed out after {timeout}s")
    if result.returncode == 127:
        return (False, False, "xcodebuild could not be launched — build skipped")
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    return (True, result.returncode == 0, out[-900:] or "xcodebuild build finished")


def _run_swift_ios_tests(
    pdir: Path,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[bool, bool, str]:
    """Run XCTest only when the Mac user selected an installed simulator destination."""
    import shutil

    if _use_container_command_names(cmd_ctx):
        return (False, False, "xcodebuild XCTest requires a macOS host")
    xcodebuild = shutil.which("xcodebuild")
    project = _ios_xcode_project(pdir)
    destination = os.getenv("SKYN3T_IOS_TEST_DESTINATION", "").strip()
    if xcodebuild is None or project is None:
        return (False, False, "")
    if not destination:
        return (False, False, "set SKYN3T_IOS_TEST_DESTINATION to run XCTest on an installed simulator")
    result = _run_proof_command(
        cmd_ctx,
        [str(xcodebuild), "-project", project.name, "-scheme", "App", "-destination", destination, "test"],
        cwd=pdir, timeout=timeout, env=dict(os.environ), network=False,
    )
    if result.timed_out:
        return (True, False, "xcodebuild test timed out")
    if result.returncode == 127:
        return (False, False, "xcodebuild test could not launch")
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    return (True, result.returncode == 0, out[-900:] or "xcodebuild test finished")


def _run_swift_tests(
    pdir: Path,
    timeout: int,
    cmd_ctx: _ProofCommandContext | None = None,
) -> tuple[bool, bool, str]:
    """Run ``swift test`` when a test target is declared.

    Returns ``(ran, passed, summary)``. An executed suite is delivery-gating
    proof evidence.

    ``ran=False`` (no toolchain / no test target / launch failure / timeout) means
    'could not assess' and remains a soft skip. Never raises."""
    import shutil

    manifest = pdir / "Package.swift"
    use_container_names = _use_container_command_names(cmd_ctx)
    swift = shutil.which("swift")
    if swift is None and not use_container_names:
        return (False, False, "")
    if not manifest.exists():
        return (False, False, "")
    try:
        if "testTarget" not in manifest.read_text(encoding="utf-8", errors="replace"):
            return (False, False, "no test target")
    except OSError:
        return (False, False, "")
    swift_cmd = "swift" if use_container_names else str(swift or "swift")
    swift_env = dict(os.environ)
    module_cache = pdir / ".skyn3t-swift-module-cache"
    try:
        module_cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    swift_env.setdefault("CLANG_MODULE_CACHE_PATH", str(module_cache))
    res = _run_proof_command(
        cmd_ctx, [swift_cmd, "test"], cwd=pdir, timeout=timeout, env=swift_env,
        network=True)
    if res.timed_out:
        return (True, False, "swift test timed out")
    if res.returncode == 127:
        return (False, False, "swift test failed to launch")
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    if "sandbox-exec: sandbox_apply: Operation not permitted" in out:
        return (False, False, "swift sandbox-exec blocked by host sandbox")
    if "ModuleCache: Operation not permitted" in out:
        return (False, False, "swift module cache blocked by host sandbox")
    return (True, res.returncode == 0, out[-500:])


@lru_cache(maxsize=1)
def _docker_daemon_ok() -> bool:
    """Best-effort docker daemon ping. Never raises."""
    if not _DOCKER_IMPORTABLE:
        return False
    client = None
    try:  # pragma: no cover - environment dependent
        import docker  # type: ignore

        client = docker.from_env(timeout=5)
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
