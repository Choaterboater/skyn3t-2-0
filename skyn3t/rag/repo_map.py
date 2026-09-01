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
import stat
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any

from skyn3t.worktree import SOURCE_TREE_EXCLUDED_DIR_NAMES

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
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
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
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "c_sharp",
    ".php": "php",
    # Component files still benefit from the JavaScript regex backstop when a
    # dedicated tree-sitter grammar is unavailable.
    ".svelte": "javascript",
    ".vue": "javascript",
    ".astro": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "css",
    ".sass": "css",
    ".less": "css",
    ".json": "json",
    ".jsonc": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".mdx": "markdown",
    ".txt": "text",
}

_DEFAULT_IGNORE = {
    *SOURCE_TREE_EXCLUDED_DIR_NAMES,
    ".cache",
    ".egg-info",
    ".idea",
    ".output",
    ".tox",
    "target",
    "vendor",
}
_DEFAULT_IGNORE_ROOT_FILES = frozenset({
    "skyn3t_manifest.json",
    "skyn3t-observability.json",
})
_DEFAULT_IGNORE_ROOT_RELATIVE_DIRS = frozenset({
    (".skyn3t", "proof-ladder"),
    (".skyn3t", "visual-proof"),
})
_DEFAULT_IGNORE_ROOT_RELATIVE_FILES = frozenset({
    (".skyn3t", "product.json"),
})

_CODE_EXTS = set(_LANG_BY_EXT.keys())
_CONTEXT_PACK_SCHEMA_VERSION = 1
_MAX_CONTEXT_TOKENS = 65_536
_MAX_QUERY_CHARS = 32_768
_MAX_QUERY_TERMS = 64
_MAX_CODE_FILE_BYTES = 1_048_576
_MAX_TOTAL_HASH_BYTES = 67_108_864
_MAX_TRAVERSAL_ENTRIES = 40_000
_MAX_SYMBOLS_PER_FILE = 2048
_MAX_IMPORTS_PER_FILE = 512
_MAX_FILE_SEARCH_TERMS = 1024
_MAX_SYMBOL_NAME_CHARS = 256
_MAX_SYMBOL_SIGNATURE_CHARS = 512
_MAX_IMPORT_CHARS = 256
_MAX_SEARCH_TERM_CHARS = 64
_MAX_CONTEXT_PATH_CHARS = 1024
_MAX_TOTAL_INDEX_ITEMS = 262_144
_MAX_TOTAL_SEARCH_TERMS = 262_144
_MAX_TREE_SITTER_NODES = 100_000
_MAX_TREE_SITTER_DEPTH = 256
_MAX_TREE_SITTER_NAME_CHILDREN = 128
_RANKING_ALGORITHM = "lexical-path-symbol-content-v2"
_PARSER_CACHE_SCHEMA = "repo-map-parser-v3"
_FILE_MAP_CACHE_MAX = 4096
_FILE_MAP_CACHE_MAX_WEIGHT = 262_144
_CONTEXT_PACK_CACHE_MAX = 128
_TREE_SITTER_SYMBOL_LANGUAGES = frozenset({
    "python",
    "javascript",
    "typescript",
    "tsx",
    "go",
    "rust",
    "java",
    "ruby",
    "c",
    "cpp",
    "swift",
    "kotlin",
    "c_sharp",
    "php",
})
_SEARCH_TERM_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_QUERY_STOPWORDS = frozenset({
    "a", "add", "an", "and", "as", "at", "be", "better", "build", "by",
    "change", "create", "fix", "for", "from", "implement", "improve", "in",
    "into", "it", "make", "of", "on", "or", "please", "repair", "show",
    "that", "the", "this", "to", "update", "use", "with",
})


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _bounded_token_budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("max_tokens must be an integer")
    return max(0, min(value, _MAX_CONTEXT_TOKENS))


def _bounded_hash_budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("max_total_hash_bytes must be an integer")
    return max(1, min(value, _MAX_TOTAL_HASH_BYTES))


def _query_terms(query: str) -> tuple[str, ...]:
    """Return a deterministic, bounded set of useful lexical query terms."""
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    expanded = _CAMEL_BOUNDARY_RE.sub(" ", query[:_MAX_QUERY_CHARS])
    terms: list[str] = []
    seen: set[str] = set()
    for raw in _SEARCH_TERM_RE.findall(expanded):
        term = raw.casefold()[:_MAX_SEARCH_TERM_CHARS]
        if len(term) < 2 or term in _QUERY_STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    return tuple(terms)


def _search_tokens(value: str, *, limit: int = 512) -> set[str]:
    expanded = _CAMEL_BOUNDARY_RE.sub(" ", value[:_MAX_QUERY_CHARS])
    return {
        token.casefold()[:_MAX_SEARCH_TERM_CHARS]
        for token in _SEARCH_TERM_RE.findall(expanded)[:limit]
        if len(token) >= 2
    }


def _lexical_score(
    value: str,
    terms: tuple[str, ...],
    *,
    exact_weight: int,
    substring_weight: int,
    token_limit: int = 512,
) -> int:
    folded = value.casefold()
    tokens = _search_tokens(value, limit=token_limit)
    score = 0
    for term in terms:
        if term in tokens:
            score += exact_weight
        elif len(term) >= 4 and term in folded:
            score += substring_weight
    return score


def _context_fragment(value: str, *, max_chars: int = 320) -> str:
    out: list[str] = []
    used = 0
    for char in value:
        codepoint = ord(char)
        if codepoint < 32 or codepoint == 127:
            fragment = f"\\x{codepoint:02x}"
        else:
            fragment = char
        remaining = max_chars - used
        if remaining <= 0:
            break
        out.append(fragment[:remaining])
        used += min(len(fragment), remaining)
    return "".join(out)


def _file_search_terms(text: str) -> tuple[tuple[str, ...], bool]:
    """Extract a bounded lexical bag for markup, styles, config, and code."""
    terms: list[str] = []
    seen: set[str] = set()
    for match in _SEARCH_TERM_RE.finditer(text):
        raw = match.group(0)
        candidates = [raw, *_CAMEL_BOUNDARY_RE.sub(" ", raw).split()]
        for candidate in candidates:
            term = candidate.casefold()[:_MAX_SEARCH_TERM_CHARS]
            if len(term) < 2 or term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= _MAX_FILE_SEARCH_TERMS:
                return tuple(terms), True
    return tuple(terms), False


def _promote_requested_search_terms(
    file_map: FileMap,
    text: str,
    query_terms: tuple[str, ...],
) -> FileMap:
    """Keep exact requested terms even when the generic lexical bag truncates.

    The structural parse cache remains query-independent: callers pass a cloned
    cached/fresh map here and only the scan-local copy is changed.
    """
    if not query_terms:
        return file_map
    wanted = set(query_terms)
    found: set[str] = set()
    for match in _SEARCH_TERM_RE.finditer(text):
        raw = match.group(0)
        for candidate in (raw, *_CAMEL_BOUNDARY_RE.sub(" ", raw).split()):
            term = candidate.casefold()[:_MAX_SEARCH_TERM_CHARS]
            if term in wanted:
                found.add(term)
        if len(found) == len(wanted):
            break
    if not found:
        return file_map
    promoted = [term for term in query_terms if term in found]
    promoted.extend(term for term in file_map.search_terms if term not in found)
    file_map.search_terms = tuple(promoted[:_MAX_FILE_SEARCH_TERMS])
    return file_map


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
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    search_terms: tuple[str, ...] = ()
    backend: str = "regex"
    index_truncated: bool = False
    parser_failed: bool = False

    def add_symbol(self, symbol: Symbol) -> None:
        if len(self.symbols) >= _MAX_SYMBOLS_PER_FILE:
            self.index_truncated = True
            return
        self.symbols.append(
            replace(
                symbol,
                name=symbol.name[:_MAX_SYMBOL_NAME_CHARS],
                signature=symbol.signature[:_MAX_SYMBOL_SIGNATURE_CHARS],
                parent=symbol.parent[:_MAX_SYMBOL_NAME_CHARS],
            )
        )

    def add_import(self, value: str) -> None:
        if len(self.imports) >= _MAX_IMPORTS_PER_FILE:
            self.index_truncated = True
            return
        self.imports.append(value[:_MAX_IMPORT_CHARS])

    def summary(
        self,
        *,
        query_terms: tuple[str, ...] = (),
        max_chars: int | None = None,
    ) -> str:
        path_fragment = _context_fragment(
            self.path,
            max_chars=min(
                _MAX_CONTEXT_PATH_CHARS,
                max(0, (max_chars or _MAX_CONTEXT_PATH_CHARS) - 2),
            ),
        )
        lines = [f"# {path_fragment}"]
        used_chars = len(lines[0])

        def append_line(line: str) -> bool:
            nonlocal used_chars
            cost = 1 + len(line)
            if max_chars is not None and used_chars + cost > max_chars:
                return False
            lines.append(line)
            used_chars += cost
            return True

        if self.imports:
            shown = ", ".join(
                _context_fragment(value, max_chars=120)
                for value in sorted(set(self.imports))[:12]
            )
            append_line(f"imports: {shown}")
        symbols = [symbol for symbol in self.symbols if symbol.kind != "import"]
        if query_terms:
            symbols = [
                symbol
                for _index, symbol in sorted(
                    enumerate(symbols),
                    key=lambda item: (
                        -_lexical_score(
                            (
                                f"{item[1].name} {item[1].signature} "
                                f"{item[1].parent}"
                            ),
                            query_terms,
                            exact_weight=20,
                            substring_weight=8,
                            token_limit=128,
                        ),
                        item[0],
                    ),
                )
            ]
        for s in symbols:
            prefix = "  " if s.kind == "method" else ""
            sig = _context_fragment(s.signature or s.name)
            if not append_line(f"{prefix}{s.kind} {sig}  (L{s.line})"):
                break
        return "\n".join(lines)


def _bound_file_map(
    file_map: FileMap,
    *,
    remaining_index_items: int,
    remaining_search_terms: int,
    query_terms: tuple[str, ...] = (),
) -> tuple[int, int, bool]:
    symbol_limit = min(len(file_map.symbols), max(0, remaining_index_items))
    remaining_after_symbols = max(0, remaining_index_items - symbol_limit)
    import_limit = min(len(file_map.imports), remaining_after_symbols)
    term_limit = min(len(file_map.search_terms), max(0, remaining_search_terms))
    truncated = (
        symbol_limit < len(file_map.symbols)
        or import_limit < len(file_map.imports)
        or term_limit < len(file_map.search_terms)
    )
    if truncated:
        file_map.index_truncated = True
        symbols = file_map.symbols
        imports = file_map.imports
        search_terms = file_map.search_terms
        if query_terms:
            symbols = [
                symbol
                for _index, symbol in sorted(
                    enumerate(symbols),
                    key=lambda item: (
                        -_lexical_score(
                            (
                                f"{item[1].name} {item[1].signature} "
                                f"{item[1].parent}"
                            ),
                            query_terms,
                            exact_weight=20,
                            substring_weight=8,
                            token_limit=128,
                        ),
                        item[0],
                    ),
                )
            ]
            imports = [
                value
                for _index, value in sorted(
                    enumerate(imports),
                    key=lambda item: (
                        -_lexical_score(
                            item[1],
                            query_terms,
                            exact_weight=6,
                            substring_weight=3,
                            token_limit=64,
                        ),
                        item[0],
                    ),
                )
            ]
            wanted_terms = set(query_terms)
            search_terms = tuple(
                term
                for _index, term in sorted(
                    enumerate(search_terms),
                    key=lambda item: (
                        -(item[1] in wanted_terms),
                        item[0],
                    ),
                )
            )
        file_map.symbols = symbols[:symbol_limit]
        file_map.imports = imports[:import_limit]
        file_map.search_terms = search_terms[:term_limit]
    return (
        len(file_map.symbols) + len(file_map.imports),
        len(file_map.search_terms),
        truncated,
    )


def _fair_budget_share(remaining: int, remaining_files: int) -> int:
    """Reserve a deterministic structural-index floor for every later file."""
    if remaining <= 0 or remaining_files <= 0:
        return 0
    return remaining // remaining_files


_CACHE_LOCK = RLock()
_FILE_MAP_CACHE: OrderedDict[tuple[str, str, str], FileMap] = OrderedDict()
_FILE_MAP_CACHE_WEIGHT = 0


def _file_map_cache_weight(file_map: FileMap) -> int:
    item_weight = (
        1
        + len(file_map.symbols)
        + len(file_map.imports)
        + len(file_map.search_terms)
    )
    character_weight = (
        len(file_map.path)
        + sum(
            len(symbol.name)
            + len(symbol.signature)
            + len(symbol.parent)
            for symbol in file_map.symbols
        )
        + sum(len(value) for value in file_map.imports)
        + sum(len(value) for value in file_map.search_terms)
    )
    return item_weight + (character_weight + 63) // 64


def _clone_file_map(file_map: FileMap, path: str) -> FileMap:
    return FileMap(
        path=path,
        language=file_map.language,
        sha256=file_map.sha256,
        symbols=[
            Symbol(
                name=symbol.name,
                kind=symbol.kind,
                file=path,
                line=symbol.line,
                signature=symbol.signature,
                parent=symbol.parent,
            )
            for symbol in file_map.symbols
        ],
        imports=list(file_map.imports),
        search_terms=tuple(file_map.search_terms),
        backend=file_map.backend,
        index_truncated=file_map.index_truncated,
        parser_failed=file_map.parser_failed,
    )


@dataclass
class _PendingFileMapWrites:
    """Scan-local cache publication buffer bounded like the destination LRU."""

    maps: OrderedDict[tuple[str, str, str], FileMap] = field(
        default_factory=OrderedDict
    )
    weight: int = 0

    def add(self, key: tuple[str, str, str], file_map: FileMap) -> None:
        stored = _clone_file_map(file_map, "")
        stored_weight = _file_map_cache_weight(stored)
        if stored_weight > _FILE_MAP_CACHE_MAX_WEIGHT:
            return
        previous = self.maps.pop(key, None)
        if previous is not None:
            self.weight -= _file_map_cache_weight(previous)
        self.maps[key] = stored
        self.weight += stored_weight
        while (
            len(self.maps) > _FILE_MAP_CACHE_MAX
            or self.weight > _FILE_MAP_CACHE_MAX_WEIGHT
        ):
            _old_key, evicted = self.maps.popitem(last=False)
            self.weight -= _file_map_cache_weight(evicted)


def _parsed_file_cache_get(
    key: tuple[str, str, str],
    *,
    path: str,
) -> FileMap | None:
    with _CACHE_LOCK:
        cached = _FILE_MAP_CACHE.get(key)
        if cached is None:
            return None
        _FILE_MAP_CACHE.move_to_end(key)
        return _clone_file_map(cached, path)


def _parsed_file_cache_put(key: tuple[str, str, str], file_map: FileMap) -> None:
    global _FILE_MAP_CACHE_WEIGHT
    stored = _clone_file_map(file_map, "")
    weight = _file_map_cache_weight(stored)
    if weight > _FILE_MAP_CACHE_MAX_WEIGHT:
        return
    with _CACHE_LOCK:
        previous = _FILE_MAP_CACHE.get(key)
        if previous is not None:
            _FILE_MAP_CACHE_WEIGHT -= _file_map_cache_weight(previous)
        _FILE_MAP_CACHE[key] = stored
        _FILE_MAP_CACHE_WEIGHT += weight
        _FILE_MAP_CACHE.move_to_end(key)
        while (
            len(_FILE_MAP_CACHE) > _FILE_MAP_CACHE_MAX
            or _FILE_MAP_CACHE_WEIGHT > _FILE_MAP_CACHE_MAX_WEIGHT
        ):
            _old_key, evicted = _FILE_MAP_CACHE.popitem(last=False)
            _FILE_MAP_CACHE_WEIGHT -= _file_map_cache_weight(evicted)


def _flush_parsed_file_cache_writes(
    writes: _PendingFileMapWrites,
) -> None:
    """Publish scan misses only after lookups finish, avoiding LRU cascades."""
    for key, file_map in writes.maps.items():
        _parsed_file_cache_put(key, file_map)


def _tree_sitter_decision_cache_get(
    key: tuple[str, str, str],
) -> bool | None:
    """Return whether a known regex fallback followed a truncated tree walk."""
    with _CACHE_LOCK:
        if key not in _TREE_SITTER_EMPTY_CACHE:
            return None
        _TREE_SITTER_EMPTY_CACHE.move_to_end(key)
        return _TREE_SITTER_EMPTY_CACHE[key]


def _tree_sitter_decision_cache_put(
    key: tuple[str, str, str],
    *,
    truncated: bool,
) -> None:
    with _CACHE_LOCK:
        _TREE_SITTER_EMPTY_CACHE[key] = truncated
        _TREE_SITTER_EMPTY_CACHE.move_to_end(key)
        while len(_TREE_SITTER_EMPTY_CACHE) > _FILE_MAP_CACHE_MAX:
            _TREE_SITTER_EMPTY_CACHE.popitem(last=False)


def _flush_tree_sitter_empty_cache_writes(
    writes: Sequence[tuple[tuple[str, str, str], bool]],
) -> None:
    pending: OrderedDict[tuple[str, str, str], bool] = OrderedDict()
    for key, truncated in writes:
        pending[key] = truncated
    for key, truncated in pending.items():
        _tree_sitter_decision_cache_put(key, truncated=truncated)


@dataclass
class RepoMap:
    root: str
    files: list[FileMap] = field(default_factory=list)
    backend: str = "regex"
    scan_truncated: bool = False
    structural_cache_hits: int = 0
    bytes_hashed: int = 0
    hash_budget_bytes: int = _MAX_TOTAL_HASH_BYTES
    index_items: int = 0
    search_terms_indexed: int = 0

    @property
    def merkle_root(self) -> str:
        return _merkle_root({f.path: f.sha256 for f in self.files})

    def all_symbols(self) -> list[Symbol]:
        out: list[Symbol] = []
        for f in self.files:
            out.extend(f.symbols)
        return out

    @staticmethod
    def _query_score(fm: FileMap, terms: tuple[str, ...]) -> int:
        if not terms:
            return 0
        symbol_scores = sorted(
            (
                _lexical_score(
                    f"{symbol.name} {symbol.signature} {symbol.parent}",
                    terms,
                    exact_weight=20,
                    substring_weight=8,
                    token_limit=128,
                )
                for symbol in fm.symbols
                if symbol.kind != "import"
            ),
            reverse=True,
        )[:4]
        import_text = " ".join(fm.imports[:128])
        searchable = set(fm.search_terms)
        return (
            _lexical_score(
                fm.path,
                terms,
                exact_weight=24,
                substring_weight=12,
                token_limit=128,
            )
            + sum(symbol_scores)
            + _lexical_score(
                import_text,
                terms,
                exact_weight=6,
                substring_weight=3,
                token_limit=256,
            )
            + sum(10 for term in terms if term in searchable)
        )

    def _ordered_files(self, query: str = "") -> list[FileMap]:
        terms = _query_terms(query)

        def rank(fm: FileMap) -> tuple[int, int, int, str, str]:
            symbol_count = sum(1 for symbol in fm.symbols if symbol.kind != "import")
            return (
                -self._query_score(fm, terms),
                -symbol_count,
                -len(fm.imports),
                fm.path.casefold(),
                fm.path,
            )

        return sorted(self.files, key=rank)

    def _render_context(
        self,
        *,
        header: str,
        max_tokens: int,
        query: str = "",
    ) -> tuple[str, int]:
        """Render a strict character-bounded context and selected-file count."""
        budget = _bounded_token_budget(max_tokens)
        char_limit = budget * 4
        parts: list[str] = [header[:char_limit]]
        used_chars = len(parts[0])
        selected = 0
        query_terms = _query_terms(query)
        for fm in self._ordered_files(query):
            separator_cost = 2
            available = char_limit - used_chars - separator_cost
            if available < 32:
                break
            # One generated mega-module must not starve every other relevant
            # file from the navigation pack.
            per_file_limit = min(available, max(128, char_limit // 4))
            block = fm.summary(
                query_terms=query_terms,
                max_chars=per_file_limit,
            )
            if len(block) > per_file_limit:
                block = block[:per_file_limit].rsplit("\n", 1)[0]
                if len(block) < 32:
                    continue
            parts.append(block)
            selected += 1
            used_chars += separator_cost + len(block)
        return "\n\n".join(parts), selected

    def to_context(self, max_tokens: int = 2000, *, query: str = "") -> str:
        """Render a token-bounded textual map, query-relevant files first."""
        header = f"Repo map ({self.backend}) rooted at {self.root}"
        context, _selected = self._render_context(
            header=header,
            max_tokens=max_tokens,
            query=query,
        )
        return context


def _repo_backend(files: Sequence[FileMap]) -> str:
    backends = {file_map.backend for file_map in files}
    if not backends:
        return "regex"
    if len(backends) == 1:
        return next(iter(backends))
    return "mixed"


@dataclass(frozen=True, slots=True)
class RepoContextPack:
    """Compact, provenance-bound navigation context for one requested change."""

    schema_version: int
    context_key: str
    backend: str
    source_merkle_root: str
    product_contract_version: int | None
    requested_change_sha256: str
    max_tokens: int
    estimated_tokens: int
    files_considered: int
    files_selected: int
    ranking_algorithm: str
    scan_truncated: bool
    cacheable: bool
    cache_hit: bool
    structural_cache_hits: int
    bytes_hashed: int
    hash_budget_bytes: int
    index_items: int
    search_terms_indexed: int
    context: str

    def summary(self) -> dict[str, object]:
        """Return bounded metadata suitable for events and task payloads."""
        return {
            "schema_version": self.schema_version,
            "context_key": self.context_key,
            "backend": self.backend,
            "source_merkle_root": self.source_merkle_root,
            "product_contract_version": self.product_contract_version,
            "requested_change_sha256": self.requested_change_sha256,
            "max_tokens": self.max_tokens,
            "estimated_tokens": self.estimated_tokens,
            "files_considered": self.files_considered,
            "files_selected": self.files_selected,
            "ranking_algorithm": self.ranking_algorithm,
            "scan_truncated": self.scan_truncated,
            "cacheable": self.cacheable,
            "cache_hit": self.cache_hit,
            "structural_cache_hits": self.structural_cache_hits,
            "bytes_hashed": self.bytes_hashed,
            "hash_budget_bytes": self.hash_budget_bytes,
            "index_items": self.index_items,
            "search_terms_indexed": self.search_terms_indexed,
        }


_CONTEXT_PACK_CACHE: OrderedDict[str, RepoContextPack] = OrderedDict()
_TREE_SITTER_EMPTY_CACHE: OrderedDict[tuple[str, str, str], bool] = OrderedDict()


def clear_repo_context_caches() -> None:
    """Clear bounded process-local repo-context caches."""
    global _FILE_MAP_CACHE_WEIGHT
    with _CACHE_LOCK:
        _FILE_MAP_CACHE.clear()
        _FILE_MAP_CACHE_WEIGHT = 0
        _CONTEXT_PACK_CACHE.clear()
        _TREE_SITTER_EMPTY_CACHE.clear()


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


def _merkle_root(file_hashes: dict[str, str]) -> str:
    """Deterministic Merkle root over (path, hash) pairs."""
    if not file_hashes:
        return hashlib.sha256(b"").hexdigest()
    leaves = [
        hashlib.sha256(f"{p}:{h}".encode()).digest()
        for p, h in sorted(file_hashes.items())
    ]
    while len(leaves) > 1:
        nxt: list[bytes] = []
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


def detect_language(path: str) -> str | None:
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

    def extract(self, path: str, text: str, language: str | None) -> FileMap:
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

    def _extract_python(self, path: str, lines: list[str], fm: FileMap) -> None:
        class_stack: list[tuple[int, str]] = []  # (indent, name)
        for ln, line in enumerate(lines, start=1):
            if fm.index_truncated:
                break
            mi = self._PY_IMPORT.match(line)
            if mi:
                mod = mi.group(1) or mi.group(2) or ""
                for part in mod.split(","):
                    part = part.strip().split(" as ")[0].strip()
                    if part:
                        fm.add_import(part)
                        fm.add_symbol(
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
                fm.add_symbol(Symbol(name, "class", path, ln, name))
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
                fm.add_symbol(
                    Symbol(
                        name,
                        kind,
                        path,
                        ln,
                        f"{name}({args})",
                        parent,
                    )
                )

    def _extract_js(self, path: str, lines: list[str], fm: FileMap) -> None:
        for ln, line in enumerate(lines, start=1):
            if fm.index_truncated:
                break
            mi = self._JS_IMPORT.match(line)
            if mi:
                fm.add_import(mi.group(1))
                fm.add_symbol(Symbol(mi.group(1), "import", path, ln))
            for mr in self._JS_REQUIRE.finditer(line):
                fm.add_import(mr.group(1))
            mc = self._JS_CLASS.match(line)
            if mc:
                fm.add_symbol(Symbol(mc.group(1), "class", path, ln, mc.group(1)))
                continue
            mf = self._JS_FUNC.match(line)
            if mf:
                fm.add_symbol(
                    Symbol(mf.group(1), "function", path, ln, f"{mf.group(1)}({mf.group(2).strip()})")
                )
                continue
            ma = self._JS_ARROW.match(line)
            if ma:
                fm.add_symbol(Symbol(ma.group(1), "function", path, ln, ma.group(1)))

    def _extract_generic(self, path: str, lines: list[str], fm: FileMap) -> None:
        for ln, line in enumerate(lines, start=1):
            if fm.index_truncated:
                break
            mf = self._GENERIC_FUNC.match(line)
            if mf:
                fm.add_symbol(Symbol(mf.group(1), "function", path, ln, mf.group(1)))
                continue
            mc = self._GENERIC_CLASS.match(line)
            if mc:
                fm.add_symbol(Symbol(mc.group(1), "class", path, ln, mc.group(1)))


class _TreeSitterExtractor:
    """Precise extractor backed by tree-sitter (when available)."""

    def __init__(self) -> None:
        # Parser objects are mutable and not documented as thread-safe. Improve
        # builds packs in worker threads, so every extractor owns its parsers.
        self._parsers: dict[str, object] = {}

    def _parser(self, language: str):
        if language in self._parsers:
            return self._parsers[language]
        try:
            parser = tree_sitter_languages.get_parser(language)  # type: ignore
        except Exception:
            parser = None
        self._parsers[language] = parser
        return parser

    def available_for(self, language: str | None) -> bool:
        if (
            not language
            or language not in _TREE_SITTER_SYMBOL_LANGUAGES
            or not _TS_AVAILABLE
        ):
            return False
        return self._parser(language) is not None

    def extract(self, path: str, text: str, language: str) -> FileMap:
        sha = hash_text(text)
        fm = FileMap(
            path=path,
            language=language,
            sha256=sha,
            backend="tree_sitter",
        )
        parser = self._parser(language)
        if parser is None:
            fm.parser_failed = True
            return fm
        try:
            tree = parser.parse(text.encode("utf-8", errors="replace"))
        except Exception:
            fm.parser_failed = True
            return fm
        src = text.encode("utf-8", errors="replace")
        try:
            self._walk(tree.root_node, src, path, fm, parent="")
        except Exception:
            # Optional parsing must never take down localization. The caller
            # will use the bounded regex backstop and will not negative-cache
            # a transient parser/traversal failure.
            fm.parser_failed = True
            fm.index_truncated = True
        return fm

    def _node_text(self, node, src: bytes) -> str:
        try:
            return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _child_name(self, node, src: bytes) -> str:
        try:
            nm = node.child_by_field_name("name")
            if nm is not None:
                return self._node_text(nm, src)
        except Exception:
            pass
        children, _truncated = self._bounded_children(
            node,
            _MAX_TREE_SITTER_NAME_CHILDREN,
        )
        for child in children:
            if child.type in {"identifier", "type_identifier", "name", "constant"}:
                return self._node_text(child, src)
        return ""

    @staticmethod
    def _bounded_children(node, limit: int) -> tuple[list[Any], bool]:
        """Read no more than ``limit`` child handles from an untrusted AST."""
        bounded_limit = max(0, limit)
        try:
            child_count = max(0, int(node.child_count))
            children: list[Any] = []
            for index in range(min(child_count, bounded_limit)):
                child = node.child(index)
                if child is not None:
                    children.append(child)
            return children, child_count > bounded_limit
        except Exception:
            raw_children = node.children
            return list(raw_children[:bounded_limit]), len(raw_children) > bounded_limit

    def _walk(self, node, src: bytes, path: str, fm: FileMap, parent: str) -> None:
        stack = [(node, parent, 0)]
        visited = 0
        while stack:
            if fm.index_truncated:
                break
            current, current_parent, depth = stack.pop()
            if visited >= _MAX_TREE_SITTER_NODES:
                fm.index_truncated = True
                break
            visited += 1
            if depth > _MAX_TREE_SITTER_DEPTH:
                fm.index_truncated = True
                continue

            t = current.type
            line = current.start_point[0] + 1
            child_parent = current_parent
            descend = True
            if t in {
                "function_definition", "function_declaration", "method_definition",
                "function_item", "method_declaration", "arrow_function",
            }:
                name = self._child_name(current, src) or "<anon>"
                kind = "method" if current_parent else "function"
                fm.add_symbol(
                    Symbol(name, kind, path, line, name, current_parent)
                )
            elif t in {
                "class_definition", "class_declaration", "struct_item",
                "interface_declaration", "impl_item",
            }:
                name = self._child_name(current, src) or "<anon>"
                fm.add_symbol(Symbol(name, "class", path, line, name))
                child_parent = name
            elif t in {
                "import_statement", "import_from_statement", "import_declaration",
                "use_declaration",
            }:
                mod = self._node_text(current, src).strip()
                fm.add_import(mod[:120])
                fm.add_symbol(Symbol(mod[:120], "import", path, line))
                descend = False

            if fm.index_truncated or not descend:
                continue
            children, children_truncated = self._bounded_children(
                current,
                _MAX_TREE_SITTER_NODES - visited,
            )
            if children_truncated:
                fm.index_truncated = True
                continue
            stack.extend(
                (child, child_parent, depth + 1)
                for child in reversed(children)
            )


# --------------------------------------------------------------------------
# File scanning
# --------------------------------------------------------------------------
def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _iter_code_files(
    root: Path, ignore: Sequence[str], max_files: int
) -> tuple[list[Path], bool]:
    ignore_set = {item.casefold() for item in ignore}
    out: list[Path] = []
    traversal_error = False
    entry_count = 0
    pending_directories = [root]

    while pending_directories:
        base = pending_directories.pop()
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(base) as scanner:
                for entry in scanner:
                    entry_count += 1
                    if entry_count > _MAX_TRAVERSAL_ENTRIES:
                        return out, True
                    entries.append(entry)
        except OSError:
            traversal_error = True
            continue

        child_directories: list[Path] = []
        for entry in sorted(
            entries,
            key=lambda value: (value.name.casefold(), value.name),
        ):
            candidate = base / entry.name
            relative_parts = tuple(
                part.casefold()
                for part in candidate.relative_to(root).parts
            )
            try:
                alias = entry.is_symlink() or _is_link_like(candidate)
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                traversal_error = True
                continue
            if is_directory:
                folded = entry.name.casefold()
                if (
                    folded in ignore_set
                    or folded.endswith(".egg-info")
                    or alias
                    or relative_parts in _DEFAULT_IGNORE_ROOT_RELATIVE_DIRS
                ):
                    continue
                child_directories.append(candidate)
                continue
            if not is_file or alias:
                continue
            if (
                base == root
                and entry.name.casefold() in _DEFAULT_IGNORE_ROOT_FILES
            ):
                continue
            if Path(entry.name).suffix.lower() in _CODE_EXTS:
                # Generated projects are untrusted navigation input. Never let
                # a symlink smuggle source from outside the project into a
                # hosted model prompt or local agent context.
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    traversal_error = True
                    continue
                if not _is_relative(resolved, root):
                    traversal_error = True
                    continue
                relative = candidate.relative_to(root).as_posix()
                if relative_parts in _DEFAULT_IGNORE_ROOT_RELATIVE_FILES:
                    continue
                if (
                    len(relative) > _MAX_CONTEXT_PATH_CHARS
                    or any(ord(char) < 32 or ord(char) == 127 for char in relative)
                ):
                    traversal_error = True
                    continue
                out.append(candidate)
                if len(out) >= max_files:
                    return out, traversal_error
        pending_directories.extend(reversed(child_directories))
    return out, traversal_error


@dataclass(frozen=True, slots=True)
class _CodeFileRead:
    text: str | None
    sha256: str
    oversized: bool = False
    bytes_hashed: int = 0
    hash_complete: bool = True


def _relative_path(path: Path, root: Path) -> str:
    return (
        path.relative_to(root).as_posix()
        if _is_relative(path, root)
        else path.as_posix()
    )


def _unreadable_file_map(path: Path, root: Path) -> FileMap:
    rel = _relative_path(path, root)
    try:
        stat = path.lstat()
        metadata = f"{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        metadata = "missing"
    return FileMap(
        path=rel,
        language=detect_language(str(path)) or "unknown",
        sha256=hash_text(f"unreadable-v1:{rel}:{metadata}"),
        backend="unreadable",
    )


def _build_file_map(
    path: Path,
    root: Path,
    ts: _TreeSitterExtractor,
    rx: _RegexExtractor,
    *,
    file_read: _CodeFileRead | None = None,
    use_cache: bool = False,
    cache_stats: dict[str, int] | None = None,
    cache_writes: _PendingFileMapWrites | None = None,
    empty_cache_writes: (
        list[tuple[tuple[str, str, str], bool]] | None
    ) = None,
    query_terms: tuple[str, ...] = (),
) -> FileMap | None:
    rel = _relative_path(path, root)
    language = detect_language(str(path))
    loaded = file_read or _read_code_file(path, root=root)
    if loaded is None:
        return None
    if loaded.oversized:
        return FileMap(
            path=rel,
            language=language or "text",
            sha256=loaded.sha256,
            backend="hash_only",
        )
    text = loaded.text
    if text is None:
        return None

    def scan_local(file_map: FileMap) -> FileMap:
        if not query_terms:
            return file_map
        return _promote_requested_search_terms(
            _clone_file_map(file_map, rel),
            text,
            query_terms,
        )

    def cached_map(backend: str) -> tuple[tuple[str, str, str], FileMap | None]:
        key = (
            _PARSER_CACHE_SCHEMA,
            loaded.sha256,
            f"{language or 'text'}:{backend}",
        )
        if not use_cache:
            return key, None
        cached = _parsed_file_cache_get(key, path=rel)
        if cached is not None and cache_stats is not None:
            cache_stats["hits"] = cache_stats.get("hits", 0) + 1
        return key, cached

    tree_sitter_available = ts.available_for(language)
    empty_decision_key = (
        _PARSER_CACHE_SCHEMA,
        loaded.sha256,
        language or "text",
    )
    tree_decision = (
        _tree_sitter_decision_cache_get(empty_decision_key)
        if use_cache and tree_sitter_available
        else None
    )
    known_empty_tree = tree_decision is not None
    tree_attempt_incomplete = bool(tree_decision)
    tree_attempt_parser_failed = False
    if tree_sitter_available and not known_empty_tree:
        tree_cache_key, cached = cached_map("tree_sitter")
        if cached is not None:
            return scan_local(cached)
        fm = ts.extract(rel, text, language)  # type: ignore[arg-type]
        tree_attempt_incomplete = fm.index_truncated or fm.parser_failed
        tree_attempt_parser_failed = fm.parser_failed
        if (fm.symbols or fm.imports) and not (
            fm.index_truncated or fm.parser_failed
        ):
            fm.sha256 = loaded.sha256
            fm.search_terms, terms_truncated = _file_search_terms(text)
            if terms_truncated:
                fm.index_truncated = True
            fm.backend = "tree_sitter"
            if use_cache:
                if cache_writes is None:
                    _parsed_file_cache_put(tree_cache_key, fm)
                else:
                    cache_writes.add(tree_cache_key, fm)
            return scan_local(fm)
        # tree-sitter produced nothing useful -> regex backstop
        if use_cache and not fm.parser_failed:
            if empty_cache_writes is None:
                _tree_sitter_decision_cache_put(
                    empty_decision_key,
                    truncated=fm.index_truncated,
                )
            else:
                empty_cache_writes.append(
                    (empty_decision_key, fm.index_truncated)
                )
    regex_cache_key, cached = cached_map("regex")
    if cached is not None:
        if tree_attempt_incomplete:
            cached.index_truncated = True
            cached.parser_failed = tree_attempt_parser_failed
        return scan_local(cached)
    fm = rx.extract(rel, text, language)
    fm.sha256 = loaded.sha256
    fm.search_terms, terms_truncated = _file_search_terms(text)
    if terms_truncated:
        fm.index_truncated = True
    fm.backend = "regex"
    if use_cache:
        if cache_writes is None:
            _parsed_file_cache_put(regex_cache_key, fm)
        else:
            cache_writes.add(regex_cache_key, fm)
    if tree_attempt_incomplete:
        fm.index_truncated = True
        fm.parser_failed = tree_attempt_parser_failed
    return scan_local(fm)


def _open_confined_descriptor(path: Path, root: Path | None) -> int:
    """Open a leaf without following symlinks in any project-relative segment."""
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_NONBLOCK"):
        file_flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        file_flags |= getattr(os, "O_CLOEXEC", 0)

    if (
        root is not None
        and hasattr(os, "O_DIRECTORY")
        and os.open in getattr(os, "supports_dir_fd", set())
    ):
        relative = path.relative_to(root)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise OSError("unsafe project-relative path")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
        opened_directories: list[int] = []
        try:
            current = os.open(root, directory_flags)
            opened_directories.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, directory_flags, dir_fd=current)
                opened_directories.append(current)
            return os.open(relative.parts[-1], file_flags, dir_fd=current)
        finally:
            for descriptor in reversed(opened_directories):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    if root is not None:
        resolved = path.resolve(strict=True)
        if not _is_relative(resolved, root):
            raise OSError("path escaped repository root")
    return os.open(path, file_flags)


def _read_code_file(
    path: Path,
    *,
    root: Path | None = None,
    max_hash_bytes: int = _MAX_TOTAL_HASH_BYTES,
) -> _CodeFileRead | None:
    """Hash a regular file once while bounding retained parser input bytes."""
    if _is_link_like(path):
        return None
    try:
        descriptor = _open_confined_descriptor(path, root)
        with os.fdopen(descriptor, "rb") as handle:
            initial_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(initial_stat.st_mode):
                return None
            byte_budget = max(0, max_hash_bytes)
            digest = hashlib.sha256()
            retained = bytearray()
            oversized = False
            bytes_hashed = 0
            while bytes_hashed < byte_budget:
                block = handle.read(min(65_536, byte_budget - bytes_hashed))
                if not block:
                    break
                digest.update(block)
                bytes_hashed += len(block)
                if not oversized and len(retained) + len(block) <= _MAX_CODE_FILE_BYTES:
                    retained.extend(block)
                else:
                    oversized = True
                    retained.clear()
            final_stat = os.fstat(handle.fileno())
        current_path_stat = path.stat(follow_symlinks=False)
    except Exception:
        return None
    stable_identity = (
        initial_stat.st_dev,
        initial_stat.st_ino,
        initial_stat.st_size,
        initial_stat.st_mtime_ns,
    ) == (
        final_stat.st_dev,
        final_stat.st_ino,
        final_stat.st_size,
        final_stat.st_mtime_ns,
    ) == (
        current_path_stat.st_dev,
        current_path_stat.st_ino,
        current_path_stat.st_size,
        current_path_stat.st_mtime_ns,
    )
    hash_complete = stable_identity and final_stat.st_size <= bytes_hashed
    if not hash_complete:
        digest.update(
            (
                f"\0partial:{final_stat.st_size}:{final_stat.st_mtime_ns}:"
                f"{bytes_hashed}"
            ).encode()
        )
        oversized = True
        retained.clear()
    return _CodeFileRead(
        text=(
            None
            if oversized
            else bytes(retained).decode("utf-8", errors="replace")
        ),
        sha256=digest.hexdigest(),
        oversized=oversized,
        bytes_hashed=bytes_hashed,
        hash_complete=hash_complete,
    )


def _is_relative(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def build_repo_map(
    directory: str,
    ignore: Sequence[str] | None = None,
    max_files: int = 2000,
    *,
    use_parse_cache: bool = False,
    max_total_hash_bytes: int = _MAX_TOTAL_HASH_BYTES,
    query: str = "",
) -> RepoMap:
    """Scan ``directory`` and build a full :class:`RepoMap`."""
    if isinstance(max_files, bool) or not isinstance(max_files, int):
        raise TypeError("max_files must be an integer")
    if max_files < 1:
        raise ValueError("max_files must be positive")
    hash_budget = _bounded_hash_budget(max_total_hash_bytes)
    query_terms = _query_terms(query)
    root = Path(directory).resolve()
    rmap = RepoMap(root=str(root), hash_budget_bytes=hash_budget)
    if not root.is_dir():
        rmap.scan_truncated = True
        return rmap
    ts = _TreeSitterExtractor()
    rx = _RegexExtractor()
    ignore_patterns: Sequence[str] = tuple(ignore or _DEFAULT_IGNORE)
    files, traversal_error = _iter_code_files(
        root,
        ignore_patterns,
        max_files + 1,
    )
    if traversal_error:
        rmap.scan_truncated = True
    if len(files) > max_files:
        rmap.scan_truncated = True
        files = files[:max_files]
    cache_stats = {"hits": 0}
    cache_writes = _PendingFileMapWrites()
    empty_cache_writes: list[tuple[tuple[str, str, str], bool]] = []
    for file_index, p in enumerate(files):
        remaining_hash_bytes = max(0, hash_budget - rmap.bytes_hashed)
        loaded = _read_code_file(
            p,
            root=root,
            max_hash_bytes=remaining_hash_bytes,
        )
        if loaded is not None:
            rmap.bytes_hashed += loaded.bytes_hashed
            if not loaded.hash_complete:
                rmap.scan_truncated = True
        fm = (
            _build_file_map(
                p,
                root,
                ts,
                rx,
                file_read=loaded,
                use_cache=use_parse_cache,
                cache_stats=cache_stats,
                cache_writes=cache_writes,
                empty_cache_writes=empty_cache_writes,
                query_terms=query_terms,
            )
            if loaded is not None
            else None
        )
        if fm is None:
            rmap.files.append(_unreadable_file_map(p, root))
            rmap.scan_truncated = True
            continue
        remaining_files = len(files) - file_index
        used_items, used_terms, aggregate_truncated = _bound_file_map(
            fm,
            remaining_index_items=_fair_budget_share(
                _MAX_TOTAL_INDEX_ITEMS - rmap.index_items,
                remaining_files,
            ),
            remaining_search_terms=_fair_budget_share(
                _MAX_TOTAL_SEARCH_TERMS - rmap.search_terms_indexed,
                remaining_files,
            ),
            query_terms=query_terms,
        )
        rmap.index_items += used_items
        rmap.search_terms_indexed += used_terms
        if aggregate_truncated:
            rmap.scan_truncated = True
        rmap.files.append(fm)
        if fm.index_truncated:
            rmap.scan_truncated = True
    if use_parse_cache:
        _flush_parsed_file_cache_writes(cache_writes)
        _flush_tree_sitter_empty_cache_writes(empty_cache_writes)
    rmap.structural_cache_hits = cache_stats["hits"]
    rmap.backend = _repo_backend(rmap.files)
    return rmap


def build_repo_context_pack(
    directory: str,
    *,
    query: str,
    product_contract_version: int | None = None,
    max_tokens: int = 2000,
    ignore: Sequence[str] | None = None,
    max_files: int = 2000,
    max_total_hash_bytes: int = _MAX_TOTAL_HASH_BYTES,
) -> RepoContextPack:
    """Build a query-ranked context pack with stable, compact provenance.

    The identity binds the exact requested change, Product Contract version,
    source Merkle root, parser backend, and context budget. It is an
    observability/cache identity, not a cryptographic attestation.
    """
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if (
        product_contract_version is not None
        and (
            isinstance(product_contract_version, bool)
            or not isinstance(product_contract_version, int)
            or product_contract_version < 1
        )
    ):
        raise ValueError("product_contract_version must be a positive integer or None")
    if isinstance(max_files, bool) or not isinstance(max_files, int):
        raise TypeError("max_files must be an integer")
    bounded_files = max(1, min(max_files, 2000))
    bounded_tokens = _bounded_token_budget(max_tokens)
    hash_budget = _bounded_hash_budget(max_total_hash_bytes)
    rmap = build_repo_map(
        directory,
        ignore=ignore,
        max_files=bounded_files,
        use_parse_cache=True,
        max_total_hash_bytes=hash_budget,
        query=query,
    )
    request_digest = hash_text(query)
    contract_label = (
        str(product_contract_version)
        if product_contract_version is not None
        else "none"
    )
    key_material = "\n".join((
        f"repo-context-pack-v{_CONTEXT_PACK_SCHEMA_VERSION}",
        rmap.merkle_root,
        contract_label,
        request_digest,
        rmap.backend,
        _RANKING_ALGORITHM,
        str(bounded_tokens),
        str(bounded_files),
        str(hash_budget),
        str(int(rmap.scan_truncated)),
    ))
    context_key = hash_text(key_material)
    cacheable = not rmap.scan_truncated
    if cacheable:
        with _CACHE_LOCK:
            cached = _CONTEXT_PACK_CACHE.get(context_key)
            if cached is not None:
                _CONTEXT_PACK_CACHE.move_to_end(context_key)
                return replace(
                    cached,
                    cache_hit=True,
                    structural_cache_hits=rmap.structural_cache_hits,
                    bytes_hashed=rmap.bytes_hashed,
                    index_items=rmap.index_items,
                    search_terms_indexed=rmap.search_terms_indexed,
                )
    header = (
        f"Repo map ({rmap.backend}) context-pack-v{_CONTEXT_PACK_SCHEMA_VERSION}\n"
        f"source={rmap.merkle_root}\n"
        f"contract={contract_label} request={request_digest} "
        f"truncated={int(rmap.scan_truncated)}"
    )
    context, selected = rmap._render_context(
        header=header,
        max_tokens=bounded_tokens,
        query=query,
    )
    pack = RepoContextPack(
        schema_version=_CONTEXT_PACK_SCHEMA_VERSION,
        context_key=context_key,
        backend=rmap.backend,
        source_merkle_root=rmap.merkle_root,
        product_contract_version=product_contract_version,
        requested_change_sha256=request_digest,
        max_tokens=bounded_tokens,
        estimated_tokens=_estimate_tokens(context),
        files_considered=len(rmap.files),
        files_selected=selected,
        ranking_algorithm=_RANKING_ALGORITHM,
        scan_truncated=rmap.scan_truncated,
        cacheable=cacheable,
        cache_hit=False,
        structural_cache_hits=rmap.structural_cache_hits,
        bytes_hashed=rmap.bytes_hashed,
        hash_budget_bytes=hash_budget,
        index_items=rmap.index_items,
        search_terms_indexed=rmap.search_terms_indexed,
        context=context,
    )
    if cacheable:
        with _CACHE_LOCK:
            _CONTEXT_PACK_CACHE[context_key] = pack
            _CONTEXT_PACK_CACHE.move_to_end(context_key)
            while len(_CONTEXT_PACK_CACHE) > _CONTEXT_PACK_CACHE_MAX:
                _CONTEXT_PACK_CACHE.popitem(last=False)
    return pack


def get_repo_map(
    directory: str,
    max_tokens: int = 2000,
    *,
    query: str = "",
) -> str:
    """Token-bounded textual repo map — the P0 context retriever entrypoint."""
    return build_repo_map(
        directory,
        query=query,
    ).to_context(max_tokens=max_tokens, query=query)


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
        ignore: Sequence[str] | None = None,
        max_files: int = 2000,
        *,
        max_total_hash_bytes: int = _MAX_TOTAL_HASH_BYTES,
    ) -> None:
        if isinstance(max_files, bool) or not isinstance(max_files, int):
            raise TypeError("max_files must be an integer")
        if max_files < 1:
            raise ValueError("max_files must be positive")
        self.root = Path(directory).resolve()
        self.ignore = list(ignore or _DEFAULT_IGNORE)
        self.max_files = max_files
        self.max_total_hash_bytes = _bounded_hash_budget(max_total_hash_bytes)
        self._maps: dict[str, FileMap] = {}
        self._hashes: dict[str, str] = {}
        self._ts = _TreeSitterExtractor()
        self._rx = _RegexExtractor()
        self.last_merkle_root = ""
        self.scan_truncated = False
        self.bytes_hashed = 0
        self.index_items = 0
        self.search_terms_indexed = 0
        self._aggregate_truncated_paths: set[str] = set()

    @property
    def backend(self) -> str:
        return _repo_backend(list(self._maps.values()))

    def scan(self) -> dict[str, list[str]]:
        """Re-scan the tree. Returns {'changed':[], 'removed':[], 'unchanged':[]}."""
        changed: list[str] = []
        unchanged: list[str] = []
        seen: set = set()
        if not self.root.exists():
            removed = sorted(self._maps)
            self._maps.clear()
            self._hashes.clear()
            self.scan_truncated = False
            self.bytes_hashed = 0
            self.index_items = 0
            self.search_terms_indexed = 0
            self._aggregate_truncated_paths.clear()
            self.last_merkle_root = _merkle_root({})
            return {"changed": [], "removed": removed, "unchanged": []}
        files, traversal_error = _iter_code_files(
            self.root,
            self.ignore,
            self.max_files + 1,
        )
        self.scan_truncated = len(files) > self.max_files
        if traversal_error:
            self.scan_truncated = True
        files = files[:self.max_files]
        self.bytes_hashed = 0
        self.index_items = 0
        self.search_terms_indexed = 0
        previously_aggregate_truncated = self._aggregate_truncated_paths
        self._aggregate_truncated_paths = set()
        cache_writes = _PendingFileMapWrites()
        empty_cache_writes: list[tuple[tuple[str, str, str], bool]] = []
        for file_index, p in enumerate(files):
            rel = (
                p.relative_to(self.root).as_posix()
                if _is_relative(p, self.root)
                else p.as_posix()
            )
            loaded = _read_code_file(
                p,
                root=self.root,
                max_hash_bytes=max(
                    0,
                    self.max_total_hash_bytes - self.bytes_hashed,
                ),
            )
            if loaded is None:
                self.scan_truncated = True
                placeholder = _unreadable_file_map(p, self.root)
                seen.add(rel)
                if (
                    self._hashes.get(rel) == placeholder.sha256
                    and rel in self._maps
                ):
                    unchanged.append(rel)
                    continue
                self._maps[rel] = placeholder
                self._hashes[rel] = placeholder.sha256
                changed.append(rel)
                continue
            self.bytes_hashed += loaded.bytes_hashed
            if not loaded.hash_complete:
                self.scan_truncated = True
            digest = loaded.sha256
            seen.add(rel)
            content_unchanged = (
                self._hashes.get(rel) == digest and rel in self._maps
            )
            fm: FileMap | None
            if (
                content_unchanged
                and rel not in previously_aggregate_truncated
                and not self._maps[rel].parser_failed
            ):
                fm = self._maps[rel]
            else:
                fm = _build_file_map(
                    p,
                    self.root,
                    self._ts,
                    self._rx,
                    file_read=loaded,
                    use_cache=True,
                    cache_writes=cache_writes,
                    empty_cache_writes=empty_cache_writes,
                )
            if fm is not None:
                remaining_files = len(files) - file_index
                used_items, used_terms, aggregate_truncated = _bound_file_map(
                    fm,
                    remaining_index_items=_fair_budget_share(
                        _MAX_TOTAL_INDEX_ITEMS - self.index_items,
                        remaining_files,
                    ),
                    remaining_search_terms=_fair_budget_share(
                        _MAX_TOTAL_SEARCH_TERMS - self.search_terms_indexed,
                        remaining_files,
                    ),
                )
                self.index_items += used_items
                self.search_terms_indexed += used_terms
                if aggregate_truncated:
                    self._aggregate_truncated_paths.add(rel)
                self._maps[rel] = fm
                self._hashes[rel] = digest
                if fm.index_truncated:
                    self.scan_truncated = True
                if content_unchanged:
                    unchanged.append(rel)
                else:
                    changed.append(rel)
        _flush_parsed_file_cache_writes(cache_writes)
        _flush_tree_sitter_empty_cache_writes(empty_cache_writes)
        removed = [rel for rel in list(self._maps.keys()) if rel not in seen]
        for rel in removed:
            self._maps.pop(rel, None)
            self._hashes.pop(rel, None)
            self._aggregate_truncated_paths.discard(rel)
        if self._aggregate_truncated_paths:
            self.scan_truncated = True
        self.last_merkle_root = _merkle_root(self._hashes)
        return {"changed": changed, "removed": removed, "unchanged": unchanged}

    def repo_map(self) -> RepoMap:
        files = [
            _clone_file_map(self._maps[rel], rel)
            for rel in sorted(
                self._maps,
                key=lambda value: (value.casefold(), value),
            )
        ]
        rmap = RepoMap(
            root=str(self.root),
            backend=_repo_backend(files),
            scan_truncated=self.scan_truncated,
            bytes_hashed=self.bytes_hashed,
            hash_budget_bytes=self.max_total_hash_bytes,
            index_items=self.index_items,
            search_terms_indexed=self.search_terms_indexed,
        )
        rmap.files = files
        return rmap

    def get_repo_map(self, max_tokens: int = 2000, *, query: str = "") -> str:
        if query and self.scan_truncated:
            # Incremental maps are intentionally query-independent. Any
            # per-file, aggregate, traversal, or hash truncation may have
            # discarded the requested lexical term, so rebuild a scan-local
            # query-aware view instead of ranking incomplete retained terms.
            return build_repo_map(
                str(self.root),
                ignore=self.ignore,
                max_files=self.max_files,
                use_parse_cache=True,
                max_total_hash_bytes=self.max_total_hash_bytes,
                query=query,
            ).to_context(max_tokens=max_tokens, query=query)
        return self.repo_map().to_context(max_tokens=max_tokens, query=query)

    @property
    def merkle_root(self) -> str:
        return _merkle_root(self._hashes)


__all__ = [
    "Symbol",
    "FileMap",
    "RepoMap",
    "RepoContextPack",
    "RepoMapIndex",
    "build_repo_map",
    "build_repo_context_pack",
    "clear_repo_context_caches",
    "get_repo_map",
    "hash_file",
    "hash_text",
    "detect_language",
]
