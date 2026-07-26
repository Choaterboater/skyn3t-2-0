"""Install the narrow visual-editor bridge into generated app shells."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.visual_editor import (
    MANAGED_STYLESHEET_RELATIVE_PATH,
    PathRejectedError,
)

_STYLE_NAME = "skyn3t-visual-editor.css"
_BRIDGE_NAME = "skyn3t-visual-editor.js"
_STYLE_LINK = (
    '<link rel="stylesheet" href="/skyn3t-visual-editor.css" '
    'data-skyn3t-visual-editor="style">'
)
_SCRIPT_TAG = (
    '<script defer src="/skyn3t-visual-editor.js" '
    'data-skyn3t-visual-editor="bridge"></script>'
)
_JSX_SCRIPT_TAG = (
    '<script defer src="/skyn3t-visual-editor.js" '
    'data-skyn3t-visual-editor="bridge" />'
)
_BRIDGE_JS = r"""(() => {
  const params = new URLSearchParams(window.location.search);
  if (params.get("skyn3t_editor") !== "1") return;
  const marker = "data-skyn3t-editor-selected";
  let selected = null;
  const clear = () => {
    if (selected) selected.removeAttribute(marker);
    selected = null;
  };
  const signature = (element) => ({
    tag: String(element.tagName || "").toLowerCase(),
    element_id: String(element.id || ""),
    classes: Array.from(element.classList || []).slice(0, 12),
    text: String(element.innerText || element.textContent || "").trim().slice(0, 2000),
    image_src: String(element.getAttribute?.("src") || ""),
  });
  document.addEventListener("click", (event) => {
    const element = event.target?.closest?.("*");
    if (!element || element === document.documentElement || element === document.body) return;
    event.preventDefault();
    event.stopPropagation();
    clear();
    selected = element;
    selected.setAttribute(marker, "true");
    window.parent.postMessage({
      type: "skyn3t-element-selected",
      signature: signature(element),
    }, "*");
  }, true);
  window.addEventListener("beforeunload", clear);
  document.documentElement.setAttribute("data-skyn3t-editor-active", "true");
})();
"""


@dataclass(frozen=True, slots=True)
class VisualEditorIntegration:
    integrated: bool
    shell: str = ""
    files: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["files"] = list(self.files)
        return value


def _safe_project_path(root: Path, relative_path: str | Path) -> Path:
    """Resolve one integration path without following project-owned symlinks."""

    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise PathRejectedError(
            "visual-editor integration path must stay inside the project",
            path=relative.as_posix(),
        )
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PathRejectedError(
                "symlink paths are not allowed during visual-editor integration",
                path=relative.as_posix(),
            )
    try:
        resolved = current.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathRejectedError(
            "visual-editor integration path escapes the project",
            path=relative.as_posix(),
        ) from exc
    return resolved


def _read_project_text(root: Path, relative_path: str | Path) -> str:
    path = _safe_project_path(root, relative_path)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PathRejectedError(
            "visual-editor integration source could not be read safely",
            path=Path(relative_path).as_posix(),
        ) from exc


def _write_if_changed(root: Path, relative_path: str | Path, text: str) -> bool:
    path = _safe_project_path(root, relative_path)
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeError):
        pass
    path = _safe_project_path(root, relative_path)
    atomic_write_text(path, text)
    return True


def _insert_html_assets(source: str) -> str:
    updated = source
    if 'data-skyn3t-visual-editor="style"' not in updated:
        if "</head>" in updated:
            updated = updated.replace("</head>", f"  {_STYLE_LINK}\n</head>", 1)
        else:
            updated = f"{_STYLE_LINK}\n{updated}"
    if 'data-skyn3t-visual-editor="bridge"' not in updated:
        if "</body>" in updated:
            updated = updated.replace("</body>", f"  {_SCRIPT_TAG}\n</body>", 1)
        else:
            updated = f"{updated}\n{_SCRIPT_TAG}\n"
    return updated


def _insert_next_assets(source: str) -> str:
    updated = source
    if "skyn3t-visual-editor.css" not in updated:
        updated = f'import "./{_STYLE_NAME}";\n{updated}'
    if 'data-skyn3t-visual-editor="bridge"' not in updated:
        if "</body>" in updated:
            updated = updated.replace("</body>", f"  {_JSX_SCRIPT_TAG}\n</body>", 1)
    return updated


def sync_visual_editor_assets(project_dir: str | Path) -> VisualEditorIntegration:
    """Mirror managed CSS and install a query-gated selection bridge.

    The bridge sends only a bounded element signature to its parent window. It
    cannot write files or call the control-plane API.
    """

    raw_root = Path(project_dir)
    if raw_root.is_symlink():
        raise PathRejectedError(
            "project root must not be a symlink",
            path=str(raw_root),
        )
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise PathRejectedError(
            "project root could not be resolved",
            path=str(raw_root),
        ) from exc
    if not root.is_dir():
        raise PathRejectedError("project root must be a directory", path=str(root))

    managed = _safe_project_path(root, MANAGED_STYLESHEET_RELATIVE_PATH)
    if not managed.is_file():
        return VisualEditorIntegration(
            False,
            reason="managed visual-editor stylesheet does not exist",
        )
    css = _read_project_text(root, MANAGED_STYLESHEET_RELATIVE_PATH)
    written: list[str] = []

    index_relative = Path("index.html")
    index = _safe_project_path(root, index_relative)
    if index.is_file():
        destinations = (
            Path(_STYLE_NAME),
            Path(_BRIDGE_NAME),
            Path("public") / _STYLE_NAME,
            Path("public") / _BRIDGE_NAME,
        )
        public = _safe_project_path(root, "public")
        for destination in destinations:
            _safe_project_path(root, destination)
        updated = _insert_html_assets(_read_project_text(root, index_relative))
        if _write_if_changed(root, index_relative, updated):
            written.append("index.html")
        for destination in destinations:
            if destination.parent == Path("public") and not public.is_dir():
                continue
            payload = css if destination.name == _STYLE_NAME else _BRIDGE_JS
            if _write_if_changed(root, destination, payload):
                written.append(destination.as_posix())
        return VisualEditorIntegration(True, "html", tuple(written))

    next_layouts = (
        Path("app/layout.tsx"),
        Path("app/layout.jsx"),
        Path("app/layout.ts"),
        Path("app/layout.js"),
        Path("src/app/layout.tsx"),
        Path("src/app/layout.jsx"),
        Path("src/app/layout.ts"),
        Path("src/app/layout.js"),
    )
    layout_relative = next(
        (
            relative
            for relative in next_layouts
            if _safe_project_path(root, relative).is_file()
        ),
        None,
    )
    if layout_relative is not None:
        style_target = layout_relative.parent / _STYLE_NAME
        public_relative = Path("public")
        bridge_relative = public_relative / _BRIDGE_NAME
        public = _safe_project_path(root, public_relative)
        _safe_project_path(root, style_target)
        _safe_project_path(root, bridge_relative)
        updated = _insert_next_assets(_read_project_text(root, layout_relative))
        if _write_if_changed(root, layout_relative, updated):
            written.append(layout_relative.as_posix())
        if _write_if_changed(root, style_target, css):
            written.append(style_target.as_posix())
        public.mkdir(parents=True, exist_ok=True)
        _safe_project_path(root, public_relative)
        if _write_if_changed(root, bridge_relative, _BRIDGE_JS):
            written.append(f"public/{_BRIDGE_NAME}")
        return VisualEditorIntegration(True, "next-layout", tuple(written))

    return VisualEditorIntegration(
        False,
        reason="no supported HTML or framework shell was found",
    )
