"""Decompose an architect file manifest into parallel build slices.

The monolithic code stage runs ONE agentic session over the whole app — the slow
path for a non-trivial build. ``slice_plan`` groups the architect's planned files
into a small set of independent workstreams (frontend / backend / tests / config)
that scoped sub-agents can generate concurrently — the Hermes orchestrator-worker
pattern — then merge + wire. Pure & offline; import has zero side effects.

The grouping is deliberately conservative: when an app is too small to benefit,
or everything lands in one slice, ``slice_plan`` returns ``{}`` so the caller
keeps the monolithic path (slicing a tiny app only pays cold-start + merge
overhead).
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import PurePosixPath
from typing import Any

# model_router tier name a slice's sub-agent should prefer (the "mixed by slice"
# routing): cheap/fast models for UI/styles/config/tests, the strong model for
# backend logic. The runner resolves these names to concrete models.
SLICE_TIERS: dict[str, str] = {
    "frontend": "ui",
    "backend": "strong",
    "tests": "cheap",
    "config": "cheap",
}

# Merge order: a slice listed later overwrites earlier ones on a path conflict.
# "config" is authoritative (package.json / lockfiles / build config) so it
# merges LAST. The returned mapping is ordered to encode this for the caller.
_MERGE_ORDER = ("frontend", "backend", "tests", "config")

_BACKEND_EXTS = (".py", ".go", ".rs", ".rb", ".php", ".java", ".ex", ".exs")
_FRONTEND_EXTS = (
    ".jsx", ".tsx", ".vue", ".svelte", ".astro",
    ".css", ".scss", ".sass", ".less", ".html",
)
_BACKEND_DIRS = frozenset({"api", "routes", "server", "backend", "services", "models", "db"})
_FRONTEND_DIRS = frozenset({"components", "pages", "src", "styles", "ui", "views", "app"})
_CONFIG_NAMES = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "tsconfig.json", "jsconfig.json", "index.html",
    "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pipfile",
    "readme.md", ".gitignore", "dockerfile", "makefile", ".env.example",
    "manifest.json", "vercel.json", "netlify.toml", "tailwind.config.js",
    "postcss.config.js",
})
_BACKEND_STACKS = frozenset({
    "python", "cli", "fastapi", "flask", "django", "node", "node_express",
    "express", "script",
    # Swift native macOS: no frontend/backend DOM split — bucket ambiguous files
    # with the non-web (backend) group rather than the web/frontend group.
    "swift",
})


def _canonical_path(raw: Any) -> str | None:
    """Return one safe, portable spelling for an architect-owned path.

    Architect output is untrusted model data.  Normalize separators and benign
    ``.`` segments before assigning ownership, while rejecting absolute paths,
    traversal, Windows drive/stream syntax, and NULs.  The returned spelling is
    always relative and uses forward slashes.
    """
    path = str(raw or "").strip().replace("\\", "/")
    if not path or "\x00" in path or ":" in path or path.startswith("/"):
        return None

    raw_parts = path.split("/")
    if any(part == ".." for part in raw_parts):
        return None

    canonical = PurePosixPath(path).as_posix()
    if canonical in {"", ".", ".."}:
        return None
    return canonical


def _norm(entry: Any) -> dict[str, Any] | None:
    """Normalise an architect entry (dict or str) to a dict with a safe path."""
    if isinstance(entry, str):
        path = _canonical_path(entry)
        return {"path": path} if path else None
    if isinstance(entry, dict):
        path = _canonical_path(entry.get("path") or entry.get("file") or "")
        if not path:
            return None
        out = dict(entry)
        out["path"] = path
        return out
    return None


def _classify(path: str, stack: str) -> str:
    """Assign a file to a slice. Priority: tests → config → backend → frontend →
    stack-based default. Deterministic and offline."""
    p = path.replace("\\", "/").lower()
    parts = p.split("/")
    name = parts[-1]

    # tests first — a test file's extension would otherwise miscategorise it.
    if (name.startswith("test_") or name.endswith("_test.py")
            or ".test." in name or ".spec." in name
            or "tests" in parts or "__tests__" in parts or "test" in parts):
        return "tests"

    # config / manifests / lockfiles / build config.
    if (name in _CONFIG_NAMES or ".config." in name
            or name.endswith((".config.js", ".config.ts", ".config.mjs", ".config.cjs"))
            or name.endswith((".yml", ".yaml", ".toml", ".ini", ".lock"))):
        return "config"

    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""

    # backend by extension or directory signal.
    if suffix in _BACKEND_EXTS or _BACKEND_DIRS.intersection(parts) or name in (
            "server.js", "server.ts", "server.mjs"):
        return "backend"

    # frontend by extension or directory signal.
    if suffix in _FRONTEND_EXTS or _FRONTEND_DIRS.intersection(parts):
        return "frontend"

    # Ambiguous .js/.ts/.mjs (no dir signal) and everything else → stack default.
    return "backend" if (stack or "").lower() in _BACKEND_STACKS else "frontend"


def slice_plan(
    files: list[Any],
    stack: str = "",
    *,
    min_files: int = 8,
) -> OrderedDict[str, list[dict[str, Any]]]:
    """Group ``files`` into parallel build slices.

    ``files`` is the architect manifest — a list of ``{"path","purpose"}`` dicts
    (or plain path strings). Returns an ``OrderedDict`` ``{slice: [file, ...]}``
    containing only non-empty slices, ordered so authoritative slices merge LAST.

    Returns an EMPTY dict when slicing isn't worthwhile — fewer than ``min_files``
    real files, or everything collapses into a single slice — so the caller keeps
    the monolithic code path (slicing a tiny app only adds overhead).
    """
    normed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in files or []:
        f = _norm(entry)
        if f is None:
            continue
        # Generated projects are developed and merged on Windows as well as
        # POSIX.  Windows paths are case-insensitive, so ownership must be too:
        # two case aliases cannot be handed to different slice agents.
        identity = f["path"].casefold()
        if identity in seen:
            continue
        seen.add(identity)
        normed.append(f)

    if len(normed) < max(2, int(min_files)):
        return OrderedDict()

    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in _MERGE_ORDER}
    for f in normed:
        buckets[_classify(f["path"], stack)].append(f)

    non_empty = OrderedDict(
        (name, buckets[name]) for name in _MERGE_ORDER if buckets[name]
    )
    # Nothing to parallelise if it all lands in one slice.
    if len(non_empty) < 2:
        return OrderedDict()
    return non_empty


def slice_tier(slice_name: str) -> str:
    """model_router tier name a slice should prefer (defaults to 'backend')."""
    return SLICE_TIERS.get(slice_name, "backend")
