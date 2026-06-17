"""Repository symbol graph and token-bounded context retriever (2.0 P0).

Builds a symbol graph (functions / classes / imports + relationships) of a
codebase. Uses tree-sitter when available for precise parsing; otherwise
degrades to a robust regex-based extractor so ``get_repo_map`` ALWAYS returns
a useful map (design rule #6).

2.0 P1 — Incremental Merkle / AST indexing: files are content-hashed and a
directory Merkle root summarizes the tree, so a ``RepoMapIndex`` only re-parses
files whose hash changed between scans.

Import has zero side effects: no parsing or disk reads happen at import time.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # optional heavy deps — prefer the 3.13-compatible language pack
    try:
        import tree_sitter_language_pack as tree_sitter_languages  # type: ignore
    except Exception:  # noqa: BLE001
        import tree_sitter_languages  # type: ignore
    from tree_sitter import Node  # type: ignore  # noqa: F401

    _TS_AVAILABLE = True
except Exception:
    tree_sitter_languages = None  # type: ignore
    _TS_AVAILABLE = False


# Language detection by extension.
_LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
}

_DEFAULT_IGNORE = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".egg-info", ".idea", ".tox",
}

_CODE_EXTS = set(_LANG_BY_EXT.keys())


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


@dataclass
class Symbol:
    name: str
    kind: str  # "function" | "class" | "method" | "import"
    file: str
    line: int = 0
    signature: str = ""
    parent: str = ""  # enclosing class for methods


@dataclass
class FileMap:
    path: str
    language: str
    sha256: str
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"# {self.path}"]
        if self.imports:
            shown = ", ".join(sorted(set(self.imports))[:12])
            lines.append(f"imports: {shown}")
        for s in self.symbols:
            if s.kind == "import":
                continue
            prefix = "  " if s.kind == "method" else ""
            sig = s.signature or s.name
            lines.append(f"{prefix}{s.kind} {sig}  (L{s.line})")
        return "\n".join(lines)


@dataclass
class RepoMap:
    root: str
    files: List[FileMap] = field(default_factory=list)
    backend: str = "regex"

    @property
    def merkle_root(self) -> str:
        return _merkle_root({f.path: f.sha256 for f in self.files})

    def all_symbols(self) -> List[Symbol]:
        out: List[Symbol] = []
        for f in self.files:
            out.extend(f.symbols)
        return out

    def to_context(self, max_tokens: int = 2000) -> str:
        """Render a token-bounded textual map, densest files first."""
        header = f"Repo map ({self.backend}) rooted at {self.root}"
        parts: List[str] = [header]
        budget = max(64, int(max_tokens)) - _estimate_tokens(header)
        # Order files by symbol richness so the most informative come first.
        ordered = sorted(
            self.files,
            key=lambda f: len([s for s in f.symbols if s.kind != "import"]),
            reverse=True,
        )
        for fm in ordered:
            block = fm.summary()
            cost = _estimate_tokens(block)
            if cost > budget:
                # Try a truncated version that still fits.
                truncated = _truncate_to_tokens(block, budget)
                if _estimate_tokens(truncated) >= 8:
                    parts.append(truncated)
                break
            parts.append(block)
            budget -= cost
            if budget <= 8:
                break
        return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Hashing / Merkle (P1 incremental indexing)
# --------------------------------------------------------------------------
def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                h.update(block)
    except Exception:
        return ""
    return h.hexdigest()


def _merkle_root(file_hashes: Dict[str, str]) -> str:
    """Deterministic Merkle root over (path, hash) pairs."""
    if not file_hashes:
        return hashlib.sha256(b"").hexdigest()
    leaves = [
        hashlib.sha256(f"{p}:{h}".encode("utf-8")).digest()
        for p, h in sorted(file_hashes.items())
    ]
    while len(leaves) > 1:
        nxt: List[bytes] = []
        for i in range(0, len(leaves), 2):
            a = leaves[i]
            b = leaves[i + 1] if i + 1 < len(leaves) else a
            nxt.append(hashlib.sha256(a + b).digest())
        leaves = nxt
    return leaves[0].hex()


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    max_chars = max(0, max_tokens * 4)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0]


def detect_language(path: str) -> Optional[str]:
    return _LANG_BY_EXT.get(Path(path).suffix.lower())


# --------------------------------------------------------------------------
# Symbol extraction
# --------------------------------------------------------------------------
class _RegexExtractor:
    """Language-aware-ish regex symbol extractor (fallback)."""

    _PY_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))")
    _PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([^)]*)")
    _PY_CLASS = re.compile(r"^(\s*)class\s+([A-Za-z_]\w*)")

    _JS_IMPORT = re.compile(r"""^\s*import\s+.*?from\s+['"]([^'"]+)['"]""")
    _JS_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
    _JS_FUNC = re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)"
    )
    _JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")
    _JS_ARROW = re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:async\s*)?\([^)]*\)\s*=>"
    )

    _GENERIC_FUNC = re.compile(
        r"^\s*(?:pub\s+)?(?:async\s+)?(?:func|fn)\s+([A-Za-z_]\w*)"
    )
    _GENERIC_CLASS = re.compile(
        r"^\s*(?:pub\s+)?(?:class|struct|interface|type|impl)\s+([A-Za-z_]\w*)"
    )

    def extract(self, path: str, text: str, language: Optional[str]) -> FileMap:
        sha = hash_text(text)
        fm = FileMap(path=path, language=language or "text", sha256=sha)
        lines = text.splitlines()
        if language == "python":
            self._extract_python(path, lines, fm)
        elif language in {"javascript", "typescript", "tsx"}:
            self._extract_js(path, lines, fm)
        else:
            self._extract_generic(path, lines, fm)
        return fm

    def _extract_python(self, path: str, lines: List[str], fm: FileMap) -> None:
        class_stack: List[Tuple[int, str]] = []  # (indent, name)
        for ln, line in enumerate(lines, start=1):
            mi = self._PY_IMPORT.match(line)
            if mi:
                mod = mi.group(1) or mi.group(2) or ""
                for part in mod.split(","):
                    part = part.strip().split(" as ")[0].strip()
                    if part:
                        fm.imports.append(part)
                        fm.symbols.append(
                            Symbol(part, "import", path, ln)
                        )
                continue
            mc = self._PY_CLASS.match(line)
            if mc:
                indent = len(mc.group(1))
                name = mc.group(2)
                while class_stack and class_stack[-1][0] >= indent:
                    class_stack.pop()
                class_stack.append((indent, name))
                fm.symbols.append(Symbol(name, "class", path, ln, name))
                continue
            md = self._PY_DEF.match(line)
            if md:
                indent = len(md.group(1))
                name = md.group(2)
                args = md.group(3).strip()
                while class_stack and class_stack[-1][0] >= indent:
                    class_stack.pop()
                parent = class_stack[-1][1] if class_stack else ""
                kind = "method" if parent else "function"
                fm.symbols.append(
                    Symbol(
                        name,
                        kind,
                        path,
                        ln,
                        f"{name}({args})",
                        parent,
                    )
                )

    def _extract_js(self, path: str, lines: List[str], fm: FileMap) -> None:
        for ln, line in enumerate(lines, start=1):
            mi = self._JS_IMPORT.match(line)
            if mi:
                fm.imports.append(mi.group(1))
                fm.symbols.append(Symbol(mi.group(1), "import", path, ln))
            for mr in self._JS_REQUIRE.finditer(line):
                fm.imports.append(mr.group(1))
            mc = self._JS_CLASS.match(line)
            if mc:
                fm.symbols.append(Symbol(mc.group(1), "class", path, ln, mc.group(1)))
                continue
            mf = self._JS_FUNC.match(line)
            if mf:
                fm.symbols.append(
                    Symbol(mf.group(1), "function", path, ln, f"{mf.group(1)}({mf.group(2).strip()})")
                )
                continue
            ma = self._JS_ARROW.match(line)
            if ma:
                fm.symbols.append(Symbol(ma.group(1), "function", path, ln, ma.group(1)))

    def _extract_generic(self, path: str, lines: List[str], fm: FileMap) -> None:
        for ln, line in enumerate(lines, start=1):
            mf = self._GENERIC_FUNC.match(line)
            if mf:
                fm.symbols.append(Symbol(mf.group(1), "function", path, ln, mf.group(1)))
                continue
            mc = self._GENERIC_CLASS.match(line)
            if mc:
                fm.symbols.append(Symbol(mc.group(1), "class", path, ln, mc.group(1)))


class _TreeSitterExtractor:
    """Precise extractor backed by tree-sitter (when available)."""

    _PARSERS: Dict[str, object] = {}

    def _parser(self, language: str):
        if language in self._PARSERS:
            return self._PARSERS[language]
        try:
            parser = tree_sitter_languages.get_parser(language)  # type: ignore
        except Exception:
            parser = None
        self._PARSERS[language] = parser
        return parser

    def available_for(self, language: Optional[str]) -> bool:
        if not language or not _TS_AVAILABLE:
            return False
        return self._parser(language) is not None

    def extract(self, path: str, text: str, language: str) -> FileMap:
        sha = hash_text(text)
        fm = FileMap(path=path, language=language, sha256=sha)
        parser = self._parser(language)
        if parser is None:
            return fm
        try:
            tree = parser.parse(text.encode("utf-8", errors="replace"))
        except Exception:
            return fm
        src = text.encode("utf-8", errors="replace")
        self._walk(tree.root_node, src, path, fm, parent="")
        return fm

    def _node_text(self, node, src: bytes) -> str:
        try:
            return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _child_name(self, node, src: bytes) -> str:
        for child in node.children:
            if child.type in {"identifier", "type_identifier", "name", "constant"}:
                return self._node_text(child, src)
        # fall back to a 'name' field if present
        try:
            nm = node.child_by_field_name("name")
            if nm is not None:
                return self._node_text(nm, src)
        except Exception:
            pass
        return ""

    def _walk(self, node, src: bytes, path: str, fm: FileMap, parent: str) -> None:
        t = node.type
        line = node.start_point[0] + 1
        if t in {
            "function_definition", "function_declaration", "method_definition",
            "function_item", "method_declaration", "arrow_function",
        }:
            name = self._child_name(node, src) or "<anon>"
            kind = "method" if parent else "function"
            fm.symbols.append(Symbol(name, kind, path, line, name, parent))
            # descend for nested defs but keep current parent
            for c in node.children:
                self._walk(c, src, path, fm, parent)
            return
        if t in {
            "class_definition", "class_declaration", "struct_item",
            "interface_declaration", "impl_item",
        }:
            name = self._child_name(node, src) or "<anon>"
            fm.symbols.append(Symbol(name, "class", path, line, name))
            for c in node.children:
                self._walk(c, src, path, fm, parent=name)
            return
        if t in {
            "import_statement", "import_from_statement", "import_declaration",
            "use_declaration",
        }:
            mod = self._node_text(node, src).strip()
            fm.imports.append(mod[:120])
            fm.symbols.append(Symbol(mod[:120], "import", path, line))
            return
        for c in node.children:
            self._walk(c, src, path, fm, parent)


# --------------------------------------------------------------------------
# File scanning
# --------------------------------------------------------------------------
def _iter_code_files(
    root: Path, ignore: Sequence[str], max_files: int
) -> List[Path]:
    ignore_set = set(ignore)
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in ignore_set and not d.endswith(".egg-info")
        ]
        for fn in filenames:
            if Path(fn).suffix.lower() in _CODE_EXTS:
                out.append(Path(dirpath) / fn)
                if len(out) >= max_files:
                    return out
    return out


def _build_file_map(
    path: Path, root: Path, ts: _TreeSitterExtractor, rx: _RegexExtractor
) -> Optional[FileMap]:
    rel = str(path.relative_to(root)) if _is_relative(path, root) else str(path)
    language = detect_language(str(path))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if ts.available_for(language):
        fm = ts.extract(rel, text, language)  # type: ignore[arg-type]
        if fm.symbols or fm.imports:
            return fm
        # tree-sitter produced nothing useful -> regex backstop
    return rx.extract(rel, text, language)


def _is_relative(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def build_repo_map(
    directory: str,
    ignore: Optional[Sequence[str]] = None,
    max_files: int = 2000,
) -> RepoMap:
    """Scan ``directory`` and build a full :class:`RepoMap`."""
    root = Path(directory).resolve()
    rmap = RepoMap(root=str(root), backend="tree_sitter" if _TS_AVAILABLE else "regex")
    if not root.exists():
        return rmap
    ts = _TreeSitterExtractor()
    rx = _RegexExtractor()
    files = _iter_code_files(root, ignore or _DEFAULT_IGNORE, max_files)
    used_ts = False
    for p in files:
        fm = _build_file_map(p, root, ts, rx)
        if fm is not None:
            rmap.files.append(fm)
            if fm.language and ts.available_for(fm.language) and fm.symbols:
                used_ts = True
    rmap.backend = "tree_sitter" if (used_ts and _TS_AVAILABLE) else "regex"
    return rmap


def get_repo_map(directory: str, max_tokens: int = 2000) -> str:
    """Token-bounded textual repo map — the P0 context retriever entrypoint."""
    return build_repo_map(directory).to_context(max_tokens=max_tokens)


# --------------------------------------------------------------------------
# Incremental Merkle / AST index (P1)
# --------------------------------------------------------------------------
class RepoMapIndex:
    """Incremental index: only re-parses files whose content hash changed.

    Call :meth:`scan` repeatedly; subsequent scans reuse cached
    :class:`FileMap`s for unchanged files and return the set of changed paths.
    """

    def __init__(
        self,
        directory: str,
        ignore: Optional[Sequence[str]] = None,
        max_files: int = 2000,
    ) -> None:
        self.root = Path(directory).resolve()
        self.ignore = list(ignore or _DEFAULT_IGNORE)
        self.max_files = max_files
        self._maps: Dict[str, FileMap] = {}
        self._hashes: Dict[str, str] = {}
        self._ts = _TreeSitterExtractor()
        self._rx = _RegexExtractor()
        self.last_merkle_root = ""

    @property
    def backend(self) -> str:
        return "tree_sitter" if _TS_AVAILABLE else "regex"

    def scan(self) -> Dict[str, List[str]]:
        """Re-scan the tree. Returns {'changed':[], 'removed':[], 'unchanged':[]}."""
        changed: List[str] = []
        unchanged: List[str] = []
        seen: set = set()
        if not self.root.exists():
            return {"changed": [], "removed": list(self._maps.keys()), "unchanged": []}
        files = _iter_code_files(self.root, self.ignore, self.max_files)
        for p in files:
            rel = str(p.relative_to(self.root)) if _is_relative(p, self.root) else str(p)
            seen.add(rel)
            digest = hash_file(p)
            if not digest:
                continue
            if self._hashes.get(rel) == digest and rel in self._maps:
                unchanged.append(rel)
                continue
            fm = _build_file_map(p, self.root, self._ts, self._rx)
            if fm is not None:
                self._maps[rel] = fm
                self._hashes[rel] = digest
                changed.append(rel)
        removed = [rel for rel in list(self._maps.keys()) if rel not in seen]
        for rel in removed:
            self._maps.pop(rel, None)
            self._hashes.pop(rel, None)
        self.last_merkle_root = _merkle_root(self._hashes)
        return {"changed": changed, "removed": removed, "unchanged": unchanged}

    def repo_map(self) -> RepoMap:
        rmap = RepoMap(root=str(self.root), backend=self.backend)
        rmap.files = list(self._maps.values())
        return rmap

    def get_repo_map(self, max_tokens: int = 2000) -> str:
        return self.repo_map().to_context(max_tokens=max_tokens)

    @property
    def merkle_root(self) -> str:
        return _merkle_root(self._hashes)


__all__ = [
    "Symbol",
    "FileMap",
    "RepoMap",
    "RepoMapIndex",
    "build_repo_map",
    "get_repo_map",
    "hash_file",
    "hash_text",
    "detect_language",
]
