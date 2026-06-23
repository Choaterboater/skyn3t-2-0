"""End-of-build liveness: enumerate a delivered web app's routes/pages, hit each,
repair failures, report. Reuses AppRunner / ImproveEngine / visual_check. Import
has zero side effects; nothing is served until a method runs."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_PY_ROUTE = re.compile(r"""@\w+\.(get|post|put|patch|delete|route)\(\s*['"]([^'"]+)['"]""", re.I)
_JS_ROUTE = re.compile(r"""\b(?:app|router)\.(get|post|put|patch|delete)\(\s*['"]([^'"]+)['"]""", re.I)
_REACT_ROUTE = re.compile(r"""(?:<Route\s+[^>]*\bpath=|["']path["']\s*:\s*)['"]([^'"]+)['"]""")
_SRC_SUFFIXES = (".py", ".js", ".ts", ".jsx", ".tsx", ".mjs")
_IGNORE_PARTS = frozenset({".git", "node_modules", ".venv", "__pycache__", "dist", "build"})


@dataclass(slots=True)
class Route:
    path: str
    method: str = "GET"
    kind: str = "page"  # page | api


def _kind(path: str, method: str) -> str:
    return "api" if method != "GET" or path.startswith("/api") else "page"


def _iter_source(root: Path):
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in _SRC_SUFFIXES:
            continue
        if _IGNORE_PARTS.intersection(p.parts):
            continue
        try:
            yield p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue


def enumerate_routes(project_dir: str | Path, stack: str = "") -> list[Route]:
    """Best-effort discovery of the routes/pages a delivered app should serve.

    Static-parses the common web frameworks (FastAPI/Flask, Express, React Router)
    and turns each static ``*.html`` file into its served path. Always includes the
    root ``/``. Dynamic (``/users/{id}``) and auth-gated routes are out of scope."""
    root = Path(project_dir)
    seen: dict[tuple[str, str], Route] = {}

    def add(path: str, method: str = "GET") -> None:
        if not path.startswith("/") or "*" in path:
            return
        method = method.upper()
        if method == "ROUTE":
            method = "GET"
        key = (path, method)
        seen.setdefault(key, Route(path=path, method=method, kind=_kind(path, method)))

    add("/")  # root always
    for text in _iter_source(root):
        for m in _PY_ROUTE.finditer(text):
            add(m.group(2), m.group(1))
        for m in _JS_ROUTE.finditer(text):
            add(m.group(2), m.group(1))
        for m in _REACT_ROUTE.finditer(text):
            add(m.group(1), "GET")
    for html in root.rglob("*.html"):
        if _IGNORE_PARTS.intersection(html.parts):
            continue
        rel = html.relative_to(root).as_posix()
        add("/" if rel == "index.html" else f"/{rel}")
    return list(seen.values())
