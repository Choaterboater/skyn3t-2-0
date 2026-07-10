"""Deterministic source reachability for generated browser games.

Game quality checks must inspect the code the browser can actually execute. Scanning
every file lets an orphaned scene or a decoy module satisfy a contract even though the
shipped ``src/main.js`` never reaches it. This module follows local JavaScript imports
from the real HTML/default entrypoints without running project code or using a network.
"""

from __future__ import annotations

import re
from pathlib import Path

_JS_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
_IMPORT_RE = re.compile(
    r"""\b(?:from|import|require)\b\s*\(?\s*['\"](\.\.?/[^'\"]+)['\"]"""
)
_SCRIPT_SRC_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"][^>]*>", re.IGNORECASE
)
_COMMENT_RE = re.compile(r"/\*.*?\*/|(?<!:)//[^\n]*", re.DOTALL)
_IMPORT_FROM_RE = re.compile(
    r"\bimport\s+(?P<clause>[^;\n]+?)\s+from\s*['\"](?P<spec>\.\.?/[^'\"]+)['\"]"
)


def strip_js_comments(text: str) -> str:
    """Remove JavaScript comments while preserving quoted import specifiers."""
    return _COMMENT_RE.sub(" ", text)


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def resolve_game_import(importer: Path, spec: str, root: Path) -> Path | None:
    """Resolve a local JS module specifier, refusing paths outside ``root``."""
    clean = spec.split("?", 1)[0].split("#", 1)[0]
    if not clean.startswith("."):
        return None
    target = importer.parent / clean
    candidates = [target]
    candidates.extend(target.with_name(target.name + suffix) for suffix in _JS_SUFFIXES)
    candidates.extend(target / f"index{suffix}" for suffix in _JS_SUFFIXES)
    for candidate in candidates:
        try:
            if _inside_root(candidate, root) and candidate.is_file():
                return candidate.resolve()
        except OSError:
            return None
    return None


def game_entry_files(project_dir: str | Path) -> list[Path]:
    """Return existing local browser-game entry modules in stable priority order."""
    root = Path(project_dir).resolve()
    explicit_candidates: list[Path] = []
    index = root / "index.html"
    try:
        if index.is_file():
            html = index.read_text(encoding="utf-8", errors="replace")
            for raw in _SCRIPT_SRC_RE.findall(html):
                clean = raw.split("?", 1)[0].split("#", 1)[0]
                if not clean or clean.startswith(("http:", "https:", "//", "data:")):
                    continue
                explicit_candidates.append(root / clean.lstrip("/"))
    except OSError:
        pass

    def _existing(candidates: list[Path]) -> list[Path]:
        out: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if resolved not in seen and _inside_root(resolved, root) and resolved.is_file():
                    seen.add(resolved)
                    out.append(resolved)
            except OSError:
                continue
        return out

    explicit = _existing(explicit_candidates)
    if explicit:
        return explicit

    # When HTML has no external script entry, fall back to the generated Phaser
    # convention. Never add these alternatives alongside an explicit script: an
    # orphaned main.ts must not make a decoy sim look browser-reachable.
    return _existing([root / "src" / f"main{suffix}" for suffix in _JS_SUFFIXES])


def reachable_game_sources(project_dir: str | Path) -> set[Path]:
    """Return local JS modules reachable from the game's browser entrypoints."""
    root = Path(project_dir).resolve()
    seen: set[Path] = set()
    pending = list(reversed(game_entry_files(root)))
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            text = strip_js_comments(
                current.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        for spec in _IMPORT_RE.findall(text):
            target = resolve_game_import(current, spec, root)
            if target is not None and target not in seen:
                pending.append(target)
    return seen


def _source_graph(root: Path, sources: set[Path]) -> dict[Path, set[Path]]:
    graph: dict[Path, set[Path]] = {source: set() for source in sources}
    for source in sources:
        try:
            text = strip_js_comments(source.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for spec in _IMPORT_RE.findall(text):
            target = resolve_game_import(source, spec, root)
            if target in sources:
                graph[source].add(target)
    return graph


def _modules_reaching(graph: dict[Path, set[Path]], target: Path) -> set[Path]:
    reaching = {target}
    changed = True
    while changed:
        changed = False
        for source, dependencies in graph.items():
            if source not in reaching and dependencies & reaching:
                reaching.add(source)
                changed = True
    return reaching


def _named_bindings(clause: str, export_name: str) -> set[str]:
    match = re.search(r"\{(?P<named>[^}]*)\}", clause)
    if not match:
        return set()
    out: set[str] = set()
    for item in match.group("named").split(","):
        parts = re.split(r"\s+as\s+", item.strip())
        if parts and parts[0].strip() == export_name:
            out.add((parts[-1] or export_name).strip())
    return {name for name in out if re.fullmatch(r"[A-Za-z_$][\w$]*", name)}


def simulation_integration_violations(
    project_dir: str | Path, sim_file: str | Path
) -> list[str]:
    """Return blocking gaps when the browser entrypoint does not run its pure sim.

    A standalone sim fixture has no browser entrypoint and remains valid for direct
    harness testing. Once a generated game has an entry module, however, the sim must
    be reachable through local imports and its ``createState`` and ``step`` bindings
    must both be invoked by reachable application code.
    """
    root = Path(project_dir).resolve()
    entries = game_entry_files(root)
    if not entries:
        return []
    try:
        sim = Path(sim_file).resolve()
        sim_rel = sim.relative_to(root).as_posix()
    except (OSError, ValueError):
        return ["pure sim core resolves outside the delivered game tree"]

    sources = reachable_game_sources(root)
    entry_names = ", ".join(path.relative_to(root).as_posix() for path in entries)
    if sim not in sources:
        return [
            f"pure sim core {sim_rel} is disconnected from the browser entrypoint "
            f"({entry_names}); import it from reachable game code and render the one "
            "authoritative state it advances"
        ]

    graph = _source_graph(root, sources)
    reaches_sim = _modules_reaching(graph, sim)
    called = {"createState": False, "step": False}
    for source in sources - {sim}:
        try:
            text = strip_js_comments(source.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for match in _IMPORT_FROM_RE.finditer(text):
            target = resolve_game_import(source, match.group("spec"), root)
            if target not in reaches_sim:
                continue
            clause = match.group("clause")
            namespace = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", clause)
            for export_name in called:
                bindings = _named_bindings(clause, export_name)
                if namespace:
                    ns = re.escape(namespace.group(1))
                    if re.search(rf"\b{ns}\s*\.\s*{export_name}\s*\(", text):
                        called[export_name] = True
                for binding in bindings:
                    if re.search(rf"(?<![\w$]){re.escape(binding)}\s*\(", text):
                        called[export_name] = True

    violations: list[str] = []
    if not called["createState"]:
        violations.append(
            "reachable browser game code never calls createState() from the pure sim; "
            "initialize one authoritative simulation state from the configured seed"
        )
    if not called["step"]:
        violations.append(
            "reachable browser game code never calls step() from the pure sim; advance "
            "the authoritative state from the Phaser update loop with delta / 1000"
        )
    return violations
