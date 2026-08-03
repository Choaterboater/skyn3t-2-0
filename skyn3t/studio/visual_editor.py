"""Safe, source-mapped visual edits for generated projects.

The visual editor deliberately exposes a narrow surface:

* locate an HTML/JSX-like element from a runtime signature;
* replace direct text or a quoted image ``src``;
* manage design tokens and layout declarations in one generated stylesheet.

It never evaluates JavaScript, rewrites component structure, or accepts raw CSS.
Every write is guarded by a SHA-256 version and performed as an atomic replace.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import html
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from skyn3t.studio.visual_design_contract import read_visual_design_contract

try:  # pragma: no cover - Windows import fallback.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


MANAGED_STYLESHEET_RELATIVE_PATH = Path(".skyn3t") / "visual-editor.css"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_LOCK_RELATIVE_PATH = Path(".skyn3t") / "visual-editor.lock"
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_SOURCE_FILES = 5_000
_SOURCE_SUFFIXES = {
    ".astro",
    ".htm",
    ".html",
    ".js",
    ".jsx",
    ".mdx",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
}
_PLAIN_HTML_SUFFIXES = {".htm", ".html"}
_IGNORED_PARTS = {
    ".git",
    ".next",
    ".nuxt",
    ".output",
    ".parcel-cache",
    ".svelte-kit",
    ".turbo",
    ".vercel",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "target",
    "vendor",
}
_NON_EDITABLE_TAGS = {"script", "style"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9:._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^--[a-z][a-z0-9-]{0,79}$")
_SIMPLE_SELECTOR_RE = re.compile(
    r"^(?:#[A-Za-z_][\w-]*|\.[A-Za-z_][\w-]*)"
    r"(?:\.[A-Za-z_][\w-]*){0,4}$"
)
_DATA_SELECTOR_RE = re.compile(r'^\[data-skyn3t-id="([A-Za-z0-9_.:-]{1,128})"\]$')
_OPEN_TAG_RE = re.compile(
    r"<(?P<tag>[A-Za-z][A-Za-z0-9:._-]*)"
    r"(?P<attrs>(?:[^<>\"']|\"[^\"]*\"|'[^']*')*?)"
    r"\s*(?P<selfclose>/?)>",
    re.DOTALL,
)
_ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_:][A-Za-z0-9:._-]*)"
    r"\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_SAFE_CSS_CHARS_RE = re.compile(r"^[A-Za-z0-9#.%(),+*/_\-\s]+$")
_NUMBER_RE = re.compile(r"^-?(?:\d+(?:\.\d+)?|\.\d+)$")
_NONNEG_NUMBER_RE = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)$")
_INTEGER_RE = re.compile(r"^-?\d+$")
_DIMENSION_RE = re.compile(
    r"^-?(?:\d+(?:\.\d+)?|\.\d+)"
    r"(?:px|rem|em|%|vh|vw|vmin|vmax|ch|ex|fr|pt|pc|cm|mm|in)?$",
    re.IGNORECASE,
)
_VAR_RE = re.compile(r"^var\(--[a-z][a-z0-9-]{0,79}\)$", re.IGNORECASE)
_MANAGED_HEADER_RE = re.compile(r"^/\* skyn3t-visual-editor:v1:(?P<payload>[A-Za-z0-9_-]+) \*/\n")
_BREAKPOINTS = {
    "base": None,
    "sm": "640px",
    "md": "768px",
    "lg": "1024px",
    "xl": "1280px",
}

_SPACING_PROPERTIES = {
    "gap",
    "row-gap",
    "column-gap",
    "margin",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "padding",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
}
_SIZING_PROPERTIES = {
    "width",
    "height",
    "min-width",
    "max-width",
    "min-height",
    "max-height",
    "aspect-ratio",
    "box-sizing",
    "overflow",
    "overflow-x",
    "overflow-y",
}
_ALIGNMENT_PROPERTIES = {
    "align-content",
    "align-items",
    "align-self",
    "justify-content",
    "justify-items",
    "justify-self",
    "place-content",
    "place-items",
    "place-self",
    "text-align",
}
_FLEX_PROPERTIES = {
    "display",
    "flex",
    "flex-basis",
    "flex-direction",
    "flex-flow",
    "flex-grow",
    "flex-shrink",
    "flex-wrap",
    "order",
}
_GRID_PROPERTIES = {
    "grid",
    "grid-area",
    "grid-auto-columns",
    "grid-auto-flow",
    "grid-auto-rows",
    "grid-column",
    "grid-column-end",
    "grid-column-start",
    "grid-row",
    "grid-row-end",
    "grid-row-start",
    "grid-template",
    "grid-template-areas",
    "grid-template-columns",
    "grid-template-rows",
}
_LAYOUT_PROPERTIES = (
    _SPACING_PROPERTIES
    | _SIZING_PROPERTIES
    | _ALIGNMENT_PROPERTIES
    | _FLEX_PROPERTIES
    | _GRID_PROPERTIES
)

_ALIGNMENT_VALUES = {
    "auto",
    "baseline",
    "center",
    "end",
    "first baseline",
    "flex-end",
    "flex-start",
    "last baseline",
    "left",
    "normal",
    "right",
    "safe center",
    "space-around",
    "space-between",
    "space-evenly",
    "start",
    "stretch",
    "unsafe center",
}
_DISPLAY_VALUES = {
    "block",
    "contents",
    "flex",
    "flow-root",
    "grid",
    "inline",
    "inline-block",
    "inline-flex",
    "inline-grid",
    "none",
}
_OVERFLOW_VALUES = {"auto", "clip", "hidden", "scroll", "visible"}

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class VisualEditorError(Exception):
    """Base error carrying an API-safe code and structured details."""

    code = "visual_editor_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": dict(self.details)}


class PathRejectedError(VisualEditorError, ValueError):
    code = "path_rejected"


class SourceNotFoundError(VisualEditorError, FileNotFoundError):
    code = "source_not_found"


class AmbiguousSourceError(VisualEditorError):
    code = "ambiguous_source"


class StaleSourceError(VisualEditorError):
    code = "stale_source"

    def __init__(self, expected_sha: str, current_sha: str, *, path: str) -> None:
        self.expected_sha = expected_sha
        self.current_sha = current_sha
        super().__init__(
            f"base SHA does not match current source for {path}",
            path=path,
            expected_sha=expected_sha,
            current_sha=current_sha,
        )


class UnsafeEditError(VisualEditorError, ValueError):
    code = "unsafe_edit"


class EditKind(StrEnum):
    TEXT = "text"
    IMAGE_SRC = "image_src"
    DESIGN_TOKEN = "design_token"
    LAYOUT = "layout"


@dataclass(frozen=True, slots=True)
class ElementSignature:
    """Browser-observed properties used to rank source occurrences."""

    tag: str = ""
    element_id: str = ""
    classes: tuple[str, ...] = ()
    text: str = ""
    image_src: str = ""

    def __post_init__(self) -> None:
        tag = self.tag.strip()
        element_id = self.element_id.strip()
        raw_classes = self.classes.split() if isinstance(self.classes, str) else self.classes
        classes = tuple(
            dict.fromkeys(
                item.strip() for item in raw_classes if isinstance(item, str) and item.strip()
            )
        )
        text = _normalize_space(self.text)
        image_src = self.image_src.strip()
        if tag and not _TAG_RE.fullmatch(tag):
            raise UnsafeEditError("element tag is invalid", field="tag")
        if element_id and not _IDENTIFIER_RE.fullmatch(element_id):
            raise UnsafeEditError("element id is invalid", field="element_id")
        if any(not _IDENTIFIER_RE.fullmatch(item) for item in classes):
            raise UnsafeEditError("element class is invalid", field="classes")
        if len(text) > 2_000 or len(image_src) > 8_192:
            raise UnsafeEditError("element signature is too large")
        if not any((tag, element_id, classes, text, image_src)):
            raise UnsafeEditError("element signature must contain at least one signal")
        object.__setattr__(self, "tag", tag)
        object.__setattr__(self, "element_id", element_id)
        object.__setattr__(self, "classes", classes)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "image_src", image_src)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ElementSignature:
        classes = value.get("classes", ())
        if isinstance(classes, str):
            classes = tuple(classes.split())
        if not isinstance(classes, (list, tuple)):
            raise UnsafeEditError("element classes must be an array or string")
        return cls(
            tag=str(value.get("tag", "")),
            element_id=str(value.get("element_id", value.get("id", ""))),
            classes=tuple(str(item) for item in classes),
            text=str(value.get("text", "")),
            image_src=str(value.get("image_src", value.get("src", ""))),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["classes"] = list(self.classes)
        return value


@dataclass(frozen=True, slots=True)
class SourceOccurrence:
    relative_path: str
    line: int
    column: int
    excerpt: str
    current_sha: str
    occurrence_id: str
    score: float
    signals: tuple[str, ...]
    editable: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["signals"] = list(self.signals)
        value["editable"] = list(self.editable)
        return value


@dataclass(frozen=True, slots=True)
class DiffMetadata:
    before_excerpt: str
    after_excerpt: str
    unified_diff: str
    added_lines: int
    removed_lines: int
    start_line: int
    end_line: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EditResult:
    operation: str
    relative_path: str
    before_sha: str
    after_sha: str
    changed: bool
    diff: DiffMetadata

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["diff"] = self.diff.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class ManagedStylesheetState:
    relative_path: str
    current_sha: str
    exists: bool
    tokens: Mapping[str, str] = field(default_factory=dict)
    rules: Mapping[str, Mapping[str, Mapping[str, str]]] = field(default_factory=dict)
    design_contract: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "current_sha": self.current_sha,
            "exists": self.exists,
            "tokens": dict(self.tokens),
            "rules": {
                breakpoint: {
                    selector: dict(declarations) for selector, declarations in selectors.items()
                }
                for breakpoint, selectors in self.rules.items()
            },
            "design_contract": dict(self.design_contract) if self.design_contract else None,
        }


@dataclass(frozen=True, slots=True)
class EditRequest:
    kind: EditKind | str
    base_sha: str
    value: str | None
    relative_path: str = ""
    signature: ElementSignature | None = None
    occurrence_id: str = ""
    line: int | None = None
    selector: str = ""
    css_property: str = ""
    breakpoint: str = "base"


@dataclass(slots=True)
class _Attribute:
    value: str
    value_start: int
    value_end: int
    quote: str


@dataclass(slots=True)
class _MarkupCandidate:
    tag: str
    start: int
    end: int
    line: int
    column: int
    element_id: str
    classes: tuple[str, ...]
    image_src: str
    src_attr: _Attribute | None
    text: str
    text_span: tuple[int, int] | None


@dataclass(slots=True)
class _ManagedState:
    tokens: dict[str, str] = field(default_factory=dict)
    rules: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "rules": self.rules}


def _normalize_space(value: str) -> str:
    return " ".join(str(value).split())


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _validate_sha(value: str) -> str:
    normalized = str(value).strip().lower()
    if not _SHA_RE.fullmatch(normalized):
        raise UnsafeEditError("base_sha must be a lowercase SHA-256 digest")
    return normalized


def _thread_lock_for(root: Path) -> threading.RLock:
    key = str(root)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _split_css_components(value: str) -> list[str]:
    components: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return []
        elif char.isspace() and depth == 0:
            if value[start:index].strip():
                components.append(value[start:index].strip())
            start = index + 1
    if depth != 0:
        return []
    if value[start:].strip():
        components.append(value[start:].strip())
    return components


def _validate_css_scalar(value: str) -> str:
    clean = str(value).strip()
    lowered = clean.casefold()
    if not clean or len(clean) > 240:
        raise UnsafeEditError("CSS value must contain 1 to 240 characters")
    forbidden = (
        ";",
        "{",
        "}",
        "<",
        ">",
        "\\",
        '"',
        "'",
        "\n",
        "\r",
        "\x00",
        "/*",
        "*/",
        "@",
        "!important",
        "url(",
        "expression",
        "javascript:",
        "vbscript:",
        "data:",
        "-moz-binding",
        "behavior:",
    )
    if any(item in lowered for item in forbidden):
        raise UnsafeEditError("CSS value contains a forbidden construct", value=clean)
    if not _SAFE_CSS_CHARS_RE.fullmatch(clean):
        raise UnsafeEditError("CSS value contains unsupported characters", value=clean)
    if not _split_css_components(clean):
        raise UnsafeEditError("CSS value has unbalanced parentheses", value=clean)
    return clean


def _is_dimension(value: str, *, allow_auto: bool = False, allow_negative: bool = True) -> bool:
    lowered = value.casefold()
    keywords = {"fit-content", "max-content", "min-content", "none"}
    if allow_auto:
        keywords.add("auto")
    if lowered in keywords or _VAR_RE.fullmatch(lowered):
        return True
    if _DIMENSION_RE.fullmatch(lowered):
        return allow_negative or not lowered.startswith("-")
    if lowered.startswith(("calc(", "clamp(", "min(", "max(")) and lowered.endswith(")"):
        return True
    return False


def _validate_spacing(property_name: str, value: str) -> None:
    parts = _split_css_components(value)
    if not 1 <= len(parts) <= 4:
        raise UnsafeEditError(f"{property_name} requires one to four spacing values")
    allow_auto = property_name.startswith("margin")
    allow_negative = property_name.startswith("margin")
    if not all(
        _is_dimension(part, allow_auto=allow_auto, allow_negative=allow_negative) for part in parts
    ):
        raise UnsafeEditError(f"{property_name} contains an invalid spacing value")


def _validate_sizing(property_name: str, value: str) -> None:
    lowered = value.casefold()
    if property_name == "box-sizing":
        if lowered not in {"border-box", "content-box"}:
            raise UnsafeEditError("box-sizing must be border-box or content-box")
        return
    if property_name.startswith("overflow"):
        if lowered not in _OVERFLOW_VALUES:
            raise UnsafeEditError(f"{property_name} contains an invalid overflow mode")
        return
    if property_name == "aspect-ratio":
        if lowered == "auto":
            return
        parts = [part.strip() for part in lowered.split("/")]
        if len(parts) not in {1, 2} or not all(_NONNEG_NUMBER_RE.fullmatch(p) for p in parts):
            raise UnsafeEditError("aspect-ratio must be auto, a number, or a numeric ratio")
        return
    if not _is_dimension(value, allow_auto=True):
        raise UnsafeEditError(f"{property_name} contains an invalid size")


def _validate_flex(property_name: str, value: str) -> None:
    lowered = value.casefold()
    if property_name == "display":
        if lowered not in _DISPLAY_VALUES:
            raise UnsafeEditError("display contains an unsupported layout mode")
    elif property_name == "flex-direction":
        if lowered not in {"column", "column-reverse", "row", "row-reverse"}:
            raise UnsafeEditError("flex-direction contains an invalid value")
    elif property_name == "flex-wrap":
        if lowered not in {"nowrap", "wrap", "wrap-reverse"}:
            raise UnsafeEditError("flex-wrap contains an invalid value")
    elif property_name == "flex-flow":
        parts = set(lowered.split())
        valid = {
            "column",
            "column-reverse",
            "nowrap",
            "row",
            "row-reverse",
            "wrap",
            "wrap-reverse",
        }
        if not 1 <= len(parts) <= 2 or not parts <= valid:
            raise UnsafeEditError("flex-flow contains an invalid value")
    elif property_name in {"flex-grow", "flex-shrink"}:
        if not _NONNEG_NUMBER_RE.fullmatch(lowered):
            raise UnsafeEditError(f"{property_name} must be a non-negative number")
    elif property_name == "order":
        if not _INTEGER_RE.fullmatch(lowered):
            raise UnsafeEditError("order must be an integer")
    elif property_name == "flex-basis":
        if not _is_dimension(lowered, allow_auto=True):
            raise UnsafeEditError("flex-basis contains an invalid size")
    elif property_name == "flex":
        if lowered in {"auto", "initial", "none"}:
            return
        flex_parts = _split_css_components(lowered)
        if not 1 <= len(flex_parts) <= 3:
            raise UnsafeEditError("flex contains too many components")
        for part in flex_parts:
            if not (_NONNEG_NUMBER_RE.fullmatch(part) or _is_dimension(part, allow_auto=True)):
                raise UnsafeEditError("flex contains an invalid component")


def _validate_grid(property_name: str, value: str) -> None:
    lowered = value.casefold()
    if property_name == "grid-auto-flow":
        if lowered not in {"column", "column dense", "dense", "row", "row dense"}:
            raise UnsafeEditError("grid-auto-flow contains an invalid value")
        return
    if property_name == "grid-template-areas":
        # Quoted arbitrary templates would broaden the CSS parser and permit injection.
        if lowered != "none":
            raise UnsafeEditError("grid-template-areas only supports 'none' in visual edits")
        return
    if lowered in {"auto", "none", "subgrid"}:
        return
    parts = _split_css_components(lowered)
    if not parts:
        raise UnsafeEditError(f"{property_name} contains an invalid grid value")
    allowed_words = {"auto", "max-content", "min-content", "span"}
    for part in parts:
        if (
            part in allowed_words
            or _INTEGER_RE.fullmatch(part)
            or _is_dimension(part, allow_auto=True)
            or (part.startswith(("repeat(", "minmax(", "fit-content(")) and part.endswith(")"))
            or "/" in part
        ):
            continue
        raise UnsafeEditError(f"{property_name} contains an invalid grid component")


def _validate_layout_value(property_name: str, value: str) -> str:
    property_name = property_name.strip().casefold()
    if property_name not in _LAYOUT_PROPERTIES:
        raise UnsafeEditError(
            "CSS property is not an allowed visual layout control",
            property=property_name,
        )
    clean = _validate_css_scalar(value)
    if property_name in _SPACING_PROPERTIES:
        _validate_spacing(property_name, clean)
    elif property_name in _SIZING_PROPERTIES:
        _validate_sizing(property_name, clean)
    elif property_name in _ALIGNMENT_PROPERTIES:
        if clean.casefold() not in _ALIGNMENT_VALUES:
            raise UnsafeEditError(f"{property_name} contains an invalid alignment")
    elif property_name in _FLEX_PROPERTIES:
        _validate_flex(property_name, clean)
    else:
        _validate_grid(property_name, clean)
    return clean


def _validate_selector(selector: str) -> str:
    clean = str(selector).strip()
    if not (_SIMPLE_SELECTOR_RE.fullmatch(clean) or _DATA_SELECTOR_RE.fullmatch(clean)):
        raise UnsafeEditError(
            "selector must be a simple id, class chain, or data-skyn3t-id selector",
            selector=clean,
        )
    return clean


def selector_for_signature(signature: ElementSignature) -> str:
    """Return the narrowest managed-CSS selector available for a signature."""
    id_selector = f"#{signature.element_id}"
    if signature.element_id and _SIMPLE_SELECTOR_RE.fullmatch(id_selector):
        return id_selector
    safe_classes = [name for name in signature.classes if _SIMPLE_SELECTOR_RE.fullmatch(f".{name}")]
    if safe_classes:
        return "".join(f".{name}" for name in safe_classes[:5])
    raise UnsafeEditError("layout edits require an element id or static class")


def _validate_image_src(value: str) -> str:
    clean = str(value).strip()
    lowered = clean.casefold()
    if not clean or len(clean) > 8_192:
        raise UnsafeEditError("image source must contain 1 to 8192 characters")
    if any(ord(char) < 32 for char in clean) or any(char in clean for char in "\"'<>`{}"):
        raise UnsafeEditError("image source contains unsupported characters")
    if lowered.startswith(("javascript:", "vbscript:", "file:", "//")):
        raise UnsafeEditError("image source uses a forbidden URL scheme")
    if lowered.startswith("data:"):
        if not re.fullmatch(
            r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+",
            clean,
            re.IGNORECASE,
        ):
            raise UnsafeEditError("only base64 raster data image sources are allowed")
    else:
        parsed = urlsplit(clean)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            raise UnsafeEditError("image source must be relative, http, or https")
        if parsed.username or parsed.password:
            raise UnsafeEditError("image source must not contain URL credentials")
    return clean


def _image_sources_match(observed: str, source: str) -> bool:
    if observed == source:
        return True
    try:
        observed_path = urlsplit(observed).path
        source_path = urlsplit(source).path
    except ValueError:
        return False
    return bool(
        observed_path
        and source_path
        and (
            observed_path == source_path
            or observed_path.endswith(source_path)
            or source_path.endswith(observed_path)
        )
    )


class VisualEditor:
    """Inspect and safely edit one generated project."""

    def __init__(self, project_dir: str | Path) -> None:
        raw_root = Path(project_dir)
        if raw_root.is_symlink():
            raise PathRejectedError("project root must not be a symlink", path=str(raw_root))
        try:
            root = raw_root.resolve(strict=True)
        except OSError as exc:
            raise PathRejectedError("project root does not exist", path=str(raw_root)) from exc
        if not root.is_dir():
            raise PathRejectedError("project root must be a directory", path=str(root))
        self.project_dir = root
        self._thread_lock = _thread_lock_for(root)

    def inspect(
        self,
        signature: ElementSignature | Mapping[str, Any],
        *,
        limit: int = 20,
    ) -> list[SourceOccurrence]:
        """Rank project source occurrences matching a runtime element signature."""
        normalized_signature = (
            signature
            if isinstance(signature, ElementSignature)
            else ElementSignature.from_mapping(signature)
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise UnsafeEditError("limit must be between 1 and 100")
        occurrences: list[SourceOccurrence] = []
        for path in self._iter_source_files():
            try:
                source_bytes = path.read_bytes()
                source = source_bytes.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            current_sha = _sha_bytes(source_bytes)
            relative_path = path.relative_to(self.project_dir).as_posix()
            for candidate in self._markup_candidates(source):
                ranked = self._rank_candidate(
                    normalized_signature,
                    candidate,
                    source=source,
                    relative_path=relative_path,
                    current_sha=current_sha,
                )
                if ranked is not None:
                    occurrences.append(ranked)
        occurrences.sort(
            key=lambda item: (
                -item.score,
                item.relative_path,
                item.line,
                item.column,
            )
        )
        return occurrences[:limit]

    inspect_element = inspect

    def edit_text(
        self,
        *,
        relative_path: str,
        base_sha: str,
        signature: ElementSignature | Mapping[str, Any],
        value: str,
        occurrence_id: str = "",
        line: int | None = None,
    ) -> EditResult:
        if not isinstance(value, str):
            raise UnsafeEditError("text replacement must be a string")
        if len(value) > 20_000 or "\x00" in value:
            raise UnsafeEditError("text replacement is too large or contains NUL")
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise UnsafeEditError("text replacement contains unsupported control characters")
        replacement = html.escape(value, quote=False)
        return self._edit_markup(
            operation=EditKind.TEXT.value,
            relative_path=relative_path,
            base_sha=base_sha,
            signature=signature,
            occurrence_id=occurrence_id,
            line=line,
            replacement=replacement,
        )

    apply_text_edit = edit_text

    def edit_image_src(
        self,
        *,
        relative_path: str,
        base_sha: str,
        signature: ElementSignature | Mapping[str, Any],
        value: str,
        occurrence_id: str = "",
        line: int | None = None,
    ) -> EditResult:
        replacement = _validate_image_src(value)
        return self._edit_markup(
            operation=EditKind.IMAGE_SRC.value,
            relative_path=relative_path,
            base_sha=base_sha,
            signature=signature,
            occurrence_id=occurrence_id,
            line=line,
            replacement=replacement,
        )

    apply_image_edit = edit_image_src

    def _visual_design_contract(self) -> dict[str, Any] | None:
        return read_visual_design_contract(self.project_dir)

    def _contract_tokens_for_editor(self) -> dict[str, str]:
        contract = self._visual_design_contract()
        raw_tokens = contract.get("tokens") if contract else None
        if not isinstance(raw_tokens, Mapping):
            return {}
        tokens: dict[str, str] = {}
        for token, value in raw_tokens.items():
            if not isinstance(token, str) or not isinstance(value, str):
                continue
            try:
                if _TOKEN_RE.fullmatch(token):
                    tokens[token] = _validate_css_scalar(value)
            except UnsafeEditError:
                # Contract metadata may safely describe values the narrow visual
                # editor cannot author (for example quoted font-family lists).
                continue
        return tokens

    def stylesheet_state(self) -> ManagedStylesheetState:
        """Return the current optimistic version and decoded managed overrides."""
        path = self._managed_stylesheet_path(must_exist=False)
        contract = self._visual_design_contract()
        if not path.exists():
            return ManagedStylesheetState(
                relative_path=MANAGED_STYLESHEET_RELATIVE_PATH.as_posix(),
                current_sha=EMPTY_SHA256,
                exists=False,
                design_contract=contract,
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise VisualEditorError(f"could not read managed stylesheet: {exc}") from exc
        state = self._decode_managed_state(text)
        return ManagedStylesheetState(
            relative_path=MANAGED_STYLESHEET_RELATIVE_PATH.as_posix(),
            current_sha=_sha_text(text),
            exists=True,
            tokens=dict(state.tokens),
            rules={
                breakpoint: {
                    selector: dict(declarations) for selector, declarations in selectors.items()
                }
                for breakpoint, selectors in state.rules.items()
            },
            design_contract=contract,
        )

    managed_stylesheet_state = stylesheet_state

    def set_design_token(
        self,
        *,
        base_sha: str,
        token: str,
        value: str | None,
    ) -> EditResult:
        token_name = str(token).strip().casefold()
        if not _TOKEN_RE.fullmatch(token_name):
            raise UnsafeEditError("design token must look like --token-name", token=token)
        clean_value = None if value is None else _validate_css_scalar(value)
        return self._edit_managed_stylesheet(
            base_sha=base_sha,
            operation=f"{EditKind.DESIGN_TOKEN.value}:{token_name}",
            mutate=lambda state: self._mutate_token(state, token_name, clean_value),
        )

    apply_design_token = set_design_token

    def set_layout(
        self,
        *,
        base_sha: str,
        selector: str,
        property_name: str,
        value: str | None,
        breakpoint: str = "base",
    ) -> EditResult:
        clean_selector = _validate_selector(selector)
        clean_property = str(property_name).strip().casefold()
        if clean_property not in _LAYOUT_PROPERTIES:
            raise UnsafeEditError(
                "CSS property is not an allowed visual layout control",
                property=clean_property,
            )
        clean_breakpoint = str(breakpoint).strip().casefold()
        if clean_breakpoint not in _BREAKPOINTS:
            raise UnsafeEditError(
                "responsive breakpoint must be base, sm, md, lg, or xl",
                breakpoint=clean_breakpoint,
            )
        clean_value = None if value is None else _validate_layout_value(clean_property, value)
        return self._edit_managed_stylesheet(
            base_sha=base_sha,
            operation=(
                f"{EditKind.LAYOUT.value}:{clean_breakpoint}:{clean_selector}:{clean_property}"
            ),
            mutate=lambda state: self._mutate_layout(
                state,
                breakpoint=clean_breakpoint,
                selector=clean_selector,
                property_name=clean_property,
                value=clean_value,
            ),
        )

    set_layout_control = set_layout

    def apply_edit(self, request: EditRequest) -> EditResult:
        """Dispatch a typed edit request without accepting raw source or raw CSS."""
        try:
            kind = request.kind if isinstance(request.kind, EditKind) else EditKind(request.kind)
        except ValueError as exc:
            raise UnsafeEditError("unsupported visual edit kind", kind=str(request.kind)) from exc
        if kind is EditKind.TEXT:
            if request.signature is None or request.value is None:
                raise UnsafeEditError("text edit requires a signature and value")
            return self.edit_text(
                relative_path=request.relative_path,
                base_sha=request.base_sha,
                signature=request.signature,
                value=request.value,
                occurrence_id=request.occurrence_id,
                line=request.line,
            )
        if kind is EditKind.IMAGE_SRC:
            if request.signature is None or request.value is None:
                raise UnsafeEditError("image edit requires a signature and value")
            return self.edit_image_src(
                relative_path=request.relative_path,
                base_sha=request.base_sha,
                signature=request.signature,
                value=request.value,
                occurrence_id=request.occurrence_id,
                line=request.line,
            )
        if kind is EditKind.DESIGN_TOKEN:
            return self.set_design_token(
                base_sha=request.base_sha,
                token=request.css_property,
                value=request.value,
            )
        return self.set_layout(
            base_sha=request.base_sha,
            selector=request.selector,
            property_name=request.css_property,
            value=request.value,
            breakpoint=request.breakpoint,
        )

    def _edit_markup(
        self,
        *,
        operation: str,
        relative_path: str,
        base_sha: str,
        signature: ElementSignature | Mapping[str, Any],
        occurrence_id: str,
        line: int | None,
        replacement: str,
    ) -> EditResult:
        normalized_sha = _validate_sha(base_sha)
        normalized_signature = (
            signature
            if isinstance(signature, ElementSignature)
            else ElementSignature.from_mapping(signature)
        )
        path = self._safe_source_path(relative_path, must_exist=True)
        if (
            operation == EditKind.TEXT.value
            and path.suffix.casefold() not in _PLAIN_HTML_SUFFIXES
            and any(delimiter in replacement for delimiter in ("{", "}", "`"))
        ):
            raise UnsafeEditError(
                "text replacement contains a framework template delimiter",
                path=path.relative_to(self.project_dir).as_posix(),
            )
        with self._locked():
            path = self._safe_source_path(relative_path, must_exist=True)
            before = self._read_source(path)
            current_sha = _sha_text(before)
            if current_sha != normalized_sha:
                raise StaleSourceError(
                    normalized_sha,
                    current_sha,
                    path=PurePosixPath(relative_path).as_posix(),
                )
            selected = self._select_candidate(
                before,
                relative_path=path.relative_to(self.project_dir).as_posix(),
                current_sha=current_sha,
                signature=normalized_signature,
                occurrence_id=occurrence_id,
                line=line,
            )
            if operation == EditKind.TEXT.value:
                if selected.text_span is None:
                    raise UnsafeEditError(
                        "selected element does not contain directly editable static text"
                    )
                start, end = selected.text_span
            else:
                if selected.src_attr is None:
                    raise UnsafeEditError(
                        "selected element does not have a quoted static src attribute"
                    )
                start, end = selected.src_attr.value_start, selected.src_attr.value_end
                if selected.src_attr.quote in replacement:
                    raise UnsafeEditError("image source conflicts with source quote delimiter")
            after = before[:start] + replacement + before[end:]
            return self._write_result(
                path,
                operation=operation,
                before=before,
                after=after,
                expected_sha=current_sha,
                focus_start=start,
                focus_end=start + len(replacement),
            )

    def _select_candidate(
        self,
        source: str,
        *,
        relative_path: str,
        current_sha: str,
        signature: ElementSignature,
        occurrence_id: str,
        line: int | None,
    ) -> _MarkupCandidate:
        candidates: list[_MarkupCandidate] = []
        for candidate in self._markup_candidates(source):
            if self._candidate_score(signature, candidate) is None:
                continue
            candidate_id = self._occurrence_id(relative_path, candidate.start, current_sha)
            if occurrence_id and candidate_id != occurrence_id:
                continue
            if line is not None and candidate.line != line:
                continue
            candidates.append(candidate)
        if not candidates:
            raise SourceNotFoundError(
                "no source occurrence matches the requested element",
                path=relative_path,
            )
        if len(candidates) != 1:
            raise AmbiguousSourceError(
                "element signature resolves to multiple source occurrences",
                path=relative_path,
                count=len(candidates),
                lines=sorted({candidate.line for candidate in candidates})[:20],
            )
        return candidates[0]

    def _iter_source_files(self) -> Iterator[Path]:
        count = 0
        for directory, dirnames, filenames in os.walk(
            self.project_dir,
            topdown=True,
            followlinks=False,
        ):
            base = Path(directory)
            kept_dirs: list[str] = []
            for name in sorted(dirnames):
                child = base / name
                if name.casefold() in _IGNORED_PARTS or name == ".skyn3t":
                    continue
                if child.is_symlink():
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                path = base / name
                if path.suffix.casefold() not in _SOURCE_SUFFIXES or path.is_symlink():
                    continue
                try:
                    if path.stat().st_size > _MAX_SOURCE_BYTES:
                        continue
                except OSError:
                    continue
                yield path
                count += 1
                if count >= _MAX_SOURCE_FILES:
                    return

    @staticmethod
    def _markup_candidates(source: str) -> Iterator[_MarkupCandidate]:
        for match in _OPEN_TAG_RE.finditer(source):
            tag = match.group("tag")
            if tag.casefold() in _NON_EDITABLE_TAGS:
                continue
            attrs_start = match.start("attrs")
            attributes: dict[str, _Attribute] = {}
            for attr_match in _ATTR_RE.finditer(match.group("attrs")):
                name = attr_match.group("name").casefold()
                attributes[name] = _Attribute(
                    value=html.unescape(attr_match.group("value")),
                    value_start=attrs_start + attr_match.start("value"),
                    value_end=attrs_start + attr_match.end("value"),
                    quote=attr_match.group("quote"),
                )
            class_attr = attributes.get("class") or attributes.get("classname")
            classes = (
                tuple(dict.fromkeys(class_attr.value.split())) if class_attr is not None else ()
            )
            id_attr = attributes.get("id")
            src_attr = attributes.get("src")
            text = ""
            text_span: tuple[int, int] | None = None
            if not match.group("selfclose"):
                close_re = re.compile(
                    rf"(?P<inner>[^<]{{0,20000}})</\s*{re.escape(tag)}\s*>",
                    re.DOTALL | re.IGNORECASE,
                )
                close_match = close_re.match(source, match.end())
                if close_match is not None:
                    inner = close_match.group("inner")
                    text = _normalize_space(html.unescape(inner))
                    if "{" not in inner and "}" not in inner:
                        text_span = (close_match.start("inner"), close_match.end("inner"))
            line = source.count("\n", 0, match.start()) + 1
            previous_newline = source.rfind("\n", 0, match.start())
            column = match.start() - previous_newline
            yield _MarkupCandidate(
                tag=tag,
                start=match.start(),
                end=match.end(),
                line=line,
                column=column,
                element_id=id_attr.value if id_attr else "",
                classes=classes,
                image_src=src_attr.value if src_attr else "",
                src_attr=src_attr,
                text=text,
                text_span=text_span,
            )

    def _rank_candidate(
        self,
        signature: ElementSignature,
        candidate: _MarkupCandidate,
        *,
        source: str,
        relative_path: str,
        current_sha: str,
    ) -> SourceOccurrence | None:
        result = self._candidate_score(signature, candidate)
        if result is None:
            return None
        score, signals = result
        editable: list[str] = []
        if candidate.text_span is not None:
            editable.append(EditKind.TEXT.value)
        if candidate.src_attr is not None:
            editable.append(EditKind.IMAGE_SRC.value)
        excerpt = self._excerpt(source, candidate.start, candidate.end)
        return SourceOccurrence(
            relative_path=relative_path,
            line=candidate.line,
            column=candidate.column,
            excerpt=excerpt,
            current_sha=current_sha,
            occurrence_id=self._occurrence_id(
                relative_path,
                candidate.start,
                current_sha,
            ),
            score=round(score, 2),
            signals=tuple(signals),
            editable=tuple(editable),
        )

    @staticmethod
    def _candidate_score(
        signature: ElementSignature,
        candidate: _MarkupCandidate,
    ) -> tuple[float, list[str]] | None:
        score = 0.0
        signals: list[str] = []
        strong_match = False
        if signature.tag:
            if signature.tag.casefold() != candidate.tag.casefold():
                return None
            score += 10
            signals.append("tag")
        if signature.element_id:
            if signature.element_id != candidate.element_id:
                return None
            score += 120
            signals.append("id")
            strong_match = True
        if signature.image_src and candidate.image_src:
            if _image_sources_match(signature.image_src, candidate.image_src):
                score += 90
                signals.append("image_src")
                strong_match = True
        if signature.text and candidate.text:
            if signature.text == candidate.text:
                score += 75
                signals.append("text")
                strong_match = True
            elif signature.text in candidate.text or candidate.text in signature.text:
                score += 45
                signals.append("partial_text")
                strong_match = True
        if signature.classes:
            matched_classes = [item for item in signature.classes if item in candidate.classes]
            if matched_classes:
                score += len(matched_classes) * 12
                signals.extend(f"class:{item}" for item in matched_classes)
                if len(matched_classes) == len(signature.classes):
                    score += 8
                strong_match = True
        only_tag = bool(signature.tag) and not any(
            (signature.element_id, signature.classes, signature.text, signature.image_src)
        )
        if not strong_match and not only_tag:
            return None
        return score, signals

    @staticmethod
    def _occurrence_id(relative_path: str, offset: int, current_sha: str) -> str:
        value = f"{relative_path}\0{offset}\0{current_sha}".encode()
        return hashlib.sha256(value).hexdigest()[:24]

    @staticmethod
    def _excerpt(source: str, start: int, end: int, *, width: int = 240) -> str:
        line_start = source.rfind("\n", 0, start) + 1
        line_end = source.find("\n", end)
        if line_end < 0:
            line_end = len(source)
        excerpt = source[line_start:line_end].strip()
        if len(excerpt) > width:
            return excerpt[: width - 1] + "…"
        return excerpt

    def _safe_source_path(self, relative_path: str, *, must_exist: bool) -> Path:
        path = self._safe_relative_path(
            relative_path,
            must_exist=must_exist,
            allow_managed=False,
        )
        if path.suffix.casefold() not in _SOURCE_SUFFIXES:
            raise PathRejectedError(
                "visual source edits only support HTML-like source files",
                path=str(relative_path),
            )
        return path

    def _managed_stylesheet_path(self, *, must_exist: bool) -> Path:
        return self._safe_relative_path(
            MANAGED_STYLESHEET_RELATIVE_PATH.as_posix(),
            must_exist=must_exist,
            allow_managed=True,
        )

    def _safe_relative_path(
        self,
        relative_path: str,
        *,
        must_exist: bool,
        allow_managed: bool,
    ) -> Path:
        raw = str(relative_path)
        if not raw or "\\" in raw or "\x00" in raw:
            raise PathRejectedError("path must be a non-empty POSIX relative path", path=raw)
        pure = PurePosixPath(raw)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise PathRejectedError("path traversal is not allowed", path=raw)
        lowered_parts = {part.casefold() for part in pure.parts}
        if lowered_parts & _IGNORED_PARTS:
            raise PathRejectedError("vendor and build output paths are not editable", path=raw)
        if ".skyn3t" in lowered_parts and not (
            allow_managed and pure == PurePosixPath(MANAGED_STYLESHEET_RELATIVE_PATH.as_posix())
        ):
            raise PathRejectedError("SkyN3t metadata is not editable as project source", path=raw)
        candidate = self.project_dir.joinpath(*pure.parts)
        current = self.project_dir
        for part in pure.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise PathRejectedError("symlink paths are not editable", path=raw)
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise SourceNotFoundError("source file does not exist", path=raw) from exc
        except OSError as exc:
            raise PathRejectedError("source path could not be resolved", path=raw) from exc
        try:
            resolved.relative_to(self.project_dir)
        except ValueError as exc:
            raise PathRejectedError("path escapes the project root", path=raw) from exc
        if must_exist and not resolved.is_file():
            raise SourceNotFoundError("source path is not a file", path=raw)
        return resolved

    def _read_source(self, path: Path) -> str:
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise VisualEditorError(f"could not read source file {path}: {exc}") from exc
        if len(source_bytes) > _MAX_SOURCE_BYTES:
            raise PathRejectedError("source file is too large for visual editing", path=str(path))
        try:
            return source_bytes.decode("utf-8")
        except UnicodeError as exc:
            raise PathRejectedError("source file must be UTF-8 text", path=str(path)) from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            # Validating the managed stylesheet also validates that the metadata
            # directory is inside the root and does not traverse a symlink.
            self._managed_stylesheet_path(must_exist=False)
            lock_path = self.project_dir / _LOCK_RELATIVE_PATH
            metadata_dir = lock_path.parent
            if metadata_dir.exists() and metadata_dir.is_symlink():
                raise PathRejectedError("SkyN3t metadata directory must not be a symlink")
            metadata_dir.mkdir(parents=True, exist_ok=True)
            try:
                handle = lock_path.open("a+", encoding="utf-8")
            except OSError as exc:
                raise VisualEditorError(f"could not open visual-editor lock: {exc}") from exc
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
                handle.close()

    def _edit_managed_stylesheet(
        self,
        *,
        base_sha: str,
        operation: str,
        mutate: Any,
    ) -> EditResult:
        normalized_sha = _validate_sha(base_sha)
        with self._locked():
            path = self._managed_stylesheet_path(must_exist=False)
            if path.exists():
                try:
                    before = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise VisualEditorError(f"could not read managed stylesheet: {exc}") from exc
                state = self._decode_managed_state(before)
            else:
                before = ""
                state = _ManagedState(tokens=self._contract_tokens_for_editor())
            current_sha = _sha_text(before)
            if current_sha != normalized_sha:
                raise StaleSourceError(
                    normalized_sha,
                    current_sha,
                    path=MANAGED_STYLESHEET_RELATIVE_PATH.as_posix(),
                )
            mutate(state)
            after = self._encode_managed_state(state)
            return self._write_result(
                path,
                operation=operation,
                before=before,
                after=after,
                expected_sha=current_sha,
                focus_start=0,
                focus_end=len(after),
            )

    @staticmethod
    def _mutate_token(
        state: _ManagedState,
        token: str,
        value: str | None,
    ) -> None:
        if value is None:
            state.tokens.pop(token, None)
        else:
            state.tokens[token] = value

    @staticmethod
    def _mutate_layout(
        state: _ManagedState,
        *,
        breakpoint: str,
        selector: str,
        property_name: str,
        value: str | None,
    ) -> None:
        breakpoint_rules = state.rules.setdefault(breakpoint, {})
        declarations = breakpoint_rules.setdefault(selector, {})
        if value is None:
            declarations.pop(property_name, None)
            if not declarations:
                breakpoint_rules.pop(selector, None)
            if not breakpoint_rules:
                state.rules.pop(breakpoint, None)
        else:
            declarations[property_name] = value

    @staticmethod
    def _encode_managed_state(state: _ManagedState) -> str:
        sorted_tokens = dict(sorted(state.tokens.items()))
        sorted_rules = {
            breakpoint: {
                selector: dict(sorted(declarations.items()))
                for selector, declarations in sorted(selectors.items())
            }
            for breakpoint, selectors in sorted(
                state.rules.items(),
                key=lambda item: list(_BREAKPOINTS).index(item[0]),
            )
        }
        normalized_payload: dict[str, Any] = {
            "tokens": sorted_tokens,
            "rules": sorted_rules,
        }
        payload_bytes = json.dumps(
            normalized_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        encoded = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
        lines = [f"/* skyn3t-visual-editor:v1:{encoded} */"]
        lines.append("/* Managed by SkyN3t. Use the visual editor instead of raw edits. */")
        if sorted_tokens:
            lines.append(":root {")
            for token, value in sorted_tokens.items():
                lines.append(f"  {token}: {value};")
            lines.append("}")
        for breakpoint, selectors in sorted_rules.items():
            media_value = _BREAKPOINTS[breakpoint]
            indent = ""
            if media_value is not None:
                lines.append(f"@media (min-width: {media_value}) {{")
                indent = "  "
            for selector, declarations in selectors.items():
                lines.append(f"{indent}{selector} {{")
                for property_name, value in declarations.items():
                    lines.append(f"{indent}  {property_name}: {value};")
                lines.append(f"{indent}}}")
            if media_value is not None:
                lines.append("}")
        return "\n".join(lines) + "\n"

    @classmethod
    def _decode_managed_state(cls, text: str) -> _ManagedState:
        match = _MANAGED_HEADER_RE.match(text)
        if match is None:
            raise UnsafeEditError("managed stylesheet is missing its SkyN3t state header")
        payload = match.group("payload")
        try:
            padded = payload + "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode())
            value = json.loads(decoded)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise UnsafeEditError("managed stylesheet state header is corrupt") from exc
        if not isinstance(value, dict) or set(value) != {"tokens", "rules"}:
            raise UnsafeEditError("managed stylesheet state has an invalid schema")
        raw_tokens = value["tokens"]
        raw_rules = value["rules"]
        if not isinstance(raw_tokens, dict) or not isinstance(raw_rules, dict):
            raise UnsafeEditError("managed stylesheet state has invalid collections")
        state = _ManagedState()
        for token, token_value in raw_tokens.items():
            if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
                raise UnsafeEditError("managed stylesheet contains an invalid token")
            if not isinstance(token_value, str):
                raise UnsafeEditError("managed stylesheet contains a non-string token value")
            state.tokens[token] = _validate_css_scalar(token_value)
        for breakpoint, selectors in raw_rules.items():
            if breakpoint not in _BREAKPOINTS or not isinstance(selectors, dict):
                raise UnsafeEditError("managed stylesheet contains an invalid breakpoint")
            state.rules[breakpoint] = {}
            for selector, declarations in selectors.items():
                clean_selector = _validate_selector(selector)
                if not isinstance(declarations, dict):
                    raise UnsafeEditError("managed stylesheet rule must be an object")
                state.rules[breakpoint][clean_selector] = {}
                for property_name, declaration_value in declarations.items():
                    if not isinstance(property_name, str) or not isinstance(
                        declaration_value,
                        str,
                    ):
                        raise UnsafeEditError("managed stylesheet declaration is invalid")
                    clean_property = property_name.casefold()
                    state.rules[breakpoint][clean_selector][clean_property] = (
                        _validate_layout_value(clean_property, declaration_value)
                    )
        if cls._encode_managed_state(state) != text:
            raise UnsafeEditError("managed stylesheet was modified outside the visual editor")
        return state

    def _write_result(
        self,
        path: Path,
        *,
        operation: str,
        before: str,
        after: str,
        expected_sha: str,
        focus_start: int,
        focus_end: int,
    ) -> EditResult:
        relative_path = path.relative_to(self.project_dir).as_posix()
        before_sha = _sha_text(before)
        if before_sha != expected_sha:
            raise StaleSourceError(expected_sha, before_sha, path=relative_path)
        if before == after:
            diff = self._diff_metadata(
                before,
                after,
                relative_path=relative_path,
                focus_start=focus_start,
                focus_end=focus_end,
            )
            return EditResult(
                operation=operation,
                relative_path=relative_path,
                before_sha=before_sha,
                after_sha=before_sha,
                changed=False,
                diff=diff,
            )
        self._atomic_compare_and_swap(path, after, expected_sha=expected_sha)
        after_sha = _sha_text(after)
        return EditResult(
            operation=operation,
            relative_path=relative_path,
            before_sha=before_sha,
            after_sha=after_sha,
            changed=True,
            diff=self._diff_metadata(
                before,
                after,
                relative_path=relative_path,
                focus_start=focus_start,
                focus_end=focus_end,
            ),
        )

    @staticmethod
    def _diff_metadata(
        before: str,
        after: str,
        *,
        relative_path: str,
        focus_start: int,
        focus_end: int,
    ) -> DiffMetadata:
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
                n=3,
            )
        )
        added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(
            1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
        )
        start_line = after.count("\n", 0, min(focus_start, len(after))) + 1
        end_line = after.count("\n", 0, min(focus_end, len(after))) + 1
        return DiffMetadata(
            before_excerpt=VisualEditor._excerpt(
                before,
                min(focus_start, len(before)),
                min(focus_end, len(before)),
            ),
            after_excerpt=VisualEditor._excerpt(
                after,
                min(focus_start, len(after)),
                min(focus_end, len(after)),
            ),
            unified_diff="".join(diff_lines),
            added_lines=added,
            removed_lines=removed,
            start_line=start_line,
            end_line=end_line,
        )

    def _atomic_compare_and_swap(
        self,
        path: Path,
        text: str,
        *,
        expected_sha: str,
    ) -> None:
        relative_path = path.relative_to(self.project_dir).as_posix()
        if path.exists():
            current = (
                self._read_source(path)
                if path.suffix != ".css"
                else path.read_text(encoding="utf-8")
            )
            mode = stat.S_IMODE(path.stat().st_mode)
        else:
            current = ""
            mode = 0o644
        current_sha = _sha_text(current)
        if current_sha != expected_sha:
            raise StaleSourceError(expected_sha, current_sha, path=relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            # Recheck after the durable temporary file is ready. This detects
            # non-cooperating editor writes during preparation.
            if path.exists():
                latest_sha = _sha_bytes(path.read_bytes())
            else:
                latest_sha = EMPTY_SHA256
            if latest_sha != expected_sha:
                raise StaleSourceError(expected_sha, latest_sha, path=relative_path)
            os.replace(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


__all__ = [
    "EMPTY_SHA256",
    "MANAGED_STYLESHEET_RELATIVE_PATH",
    "AmbiguousSourceError",
    "DiffMetadata",
    "EditKind",
    "EditRequest",
    "EditResult",
    "ElementSignature",
    "ManagedStylesheetState",
    "PathRejectedError",
    "SourceNotFoundError",
    "SourceOccurrence",
    "StaleSourceError",
    "UnsafeEditError",
    "VisualEditor",
    "VisualEditorError",
    "selector_for_signature",
]
