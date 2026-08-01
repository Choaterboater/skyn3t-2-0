"""Advisory SEO check — a deterministic static scan of a delivered web build.

Unlike the game quality checks (empty-board / tiny-sprite / feel), SEO signals are
CHEAP and UNAMBIGUOUS: a page either has a ``<title>`` or it doesn't; a ``<meta
name="description">`` is present or it isn't. So this needs no LLM and no vision — it is
a pure static scan of the delivered source + any built output on disk. It never runs a
server.

Like ``game_visual_check`` / ``qa_playtest`` it is ADVISORY and never-raises: findings are
recorded to the manifest and fed to the improve loop via :meth:`SeoVerdict.gaps`, but it
must NEVER flip a build's verdict — a static SEO nit must not be able to no_go a working
app (same do-no-harm philosophy). On any error, or for a non-web / no-HTML project, it
SOFT-SKIPS (degrade open, never false-flag).

Stacks differ, so the scan handles BOTH literal HTML tags (static / Astro / Remix / a
Vite ``index.html`` / react-helmet / ``next/head``) AND the framework metadata idioms
(Next.js App Router's ``export const metadata`` with ``title`` / ``description`` /
``openGraph``). A signal found in ANY relevant file satisfies the check — a Next.js site
is never false-flagged for lacking a literal ``<title>`` when it has ``metadata.title``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Web stacks that produce public, crawlable HTML pages — the only ones an SEO scan
# applies to. Deliberately NARROWER than runner's ``_WEB_STACKS``: it excludes phaser
# (a game canvas, not an SEO page), tauri/desktop (a packaged desktop app), and the
# API-only fastapi/express server stacks. check_seo itself soft-skips anything not here,
# so it double-guards even if a caller passes a broader stack.
_SEO_WEB_STACKS = frozenset({
    "react", "react_vite", "vite", "nextjs", "next",
    "astro", "remix", "vue", "vuejs", "sveltekit", "svelte",
    "react_ts", "typescript", "static", "static_html",
})

# Source files that can carry HTML or the framework metadata idioms.
_HTML_EXTS = frozenset({".html", ".htm"})
_META_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                        ".astro", ".vue", ".svelte"})
# Never descend into these (heavy / vendored / VCS) — they'd swamp the scan and add noise.
_EXCLUDE_DIRS = frozenset({"node_modules", ".git", ".cache", ".turbo", ".vercel",
                           ".svelte-kit", "coverage", ".pytest_cache"})
# Build-output dirs: we still read their HTML (delivered pages) but skip their minified
# JS chunks (would be noise, and huge).
_BUILD_DIRS = frozenset({".next", "dist", "out", "build", ".output"})

_MAX_FILES = 600
_MAX_BYTES = 300_000

# ── literal-tag detectors (run over the combined corpus of html + framework source) ──
_TITLE_LITERAL_RE = re.compile(r"<title\b[^>]*>\s*(\S[^<]*?)</title>", re.I | re.S)
_META_TAG_RE = re.compile(r"<meta\b[^>]*?>", re.I | re.S)
_H1_RE = re.compile(r"<h1\b", re.I)
_LANG_RE = re.compile(r"<html\b[^>]*\blang\s*=\s*[\"']\s*[A-Za-z]", re.I | re.S)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
_ALT_RE = re.compile(r"\balt\s*=", re.I)

_ROBOTS_PATTERNS = (
    "robots.txt", "public/robots.txt", "static/robots.txt",
    "dist/robots.txt", "out/robots.txt", "build/robots.txt",
    "app/robots.ts", "app/robots.js", "app/robots.tsx", "app/robots.mjs",
    "src/app/robots.ts", "src/app/robots.js", "src/app/robots.tsx",
)
_SITEMAP_PATTERNS = (
    "sitemap.xml", "sitemap-index.xml", "public/sitemap.xml", "static/sitemap.xml",
    "dist/sitemap.xml", "out/sitemap.xml", "build/sitemap.xml",
    "app/sitemap.ts", "app/sitemap.js", "app/sitemap.tsx",
    "src/app/sitemap.ts", "src/app/sitemap.js",
)


@dataclass(slots=True)
class SeoVerdict:
    """The outcome of an advisory SEO scan.

    ``issues`` are HARD flags (missing title / description / h1 / html lang); ``warnings``
    are SOFT signals (weak Open Graph, images without alt, absent robots/sitemap).
    ``ok`` is a property (not a gate) — True when the scan ran and found no hard issues.
    ``skipped`` marks a degrade-open (non-web stack, no HTML files, or any error): a
    skipped verdict is ``ok=False`` yet produces NO gaps, so it can never false-flag.
    """

    skipped: bool = False
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def ok(self) -> bool:
        """A clean page: the scan ran (not a soft-skip) and found no HARD issues.
        Advisory only — the runner never reads this to flip the verdict."""
        return (not self.skipped) and (not self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "checked": dict(self.checked),
            "reason": self.reason,
            "gaps": self.gaps(),
        }

    def gaps(self) -> list[str]:
        """Actionable repair strings for the improve loop; ``[]`` when the verdict is ok
        or a soft-skip (a could-not-scan run must never false-flag a working page).
        Derived from ``checked`` so it stays consistent with ``issues``/``ok``."""
        if self.skipped or self.ok:
            return []
        c = self.checked
        out: list[str] = []
        if not c.get("title"):
            out.append(
                "Add a non-empty <title> for the page (literal <title>, a Next.js "
                "metadata.title, or a next/head/react-helmet <title>) describing the page."
            )
        if not c.get("description"):
            out.append(
                'Add a meta description (<meta name="description" content="...">, a '
                "Next.js metadata.description, or an og:description) summarizing the "
                "page in ~150 characters."
            )
        if not c.get("h1_count"):
            out.append(
                "Add exactly one descriptive <h1> heading to the main page content "
                "(there is currently no <h1> in the delivered page source)."
            )
        if not c.get("lang"):
            out.append(
                'Add a lang attribute to the root <html> element (e.g. <html lang="en">) '
                "for accessibility and search engines."
            )
        return out


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes()[:_MAX_BYTES].decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 - an unreadable file must never break the scan
        return None


def _collect(root: Path) -> tuple[list[str], list[str], int]:
    """Gather HTML texts and framework-source texts from the DELIVERED SOURCE of the
    project, bounded and pruning vendored/VCS AND build-output dirs. Delivered-source
    scanning is the contract: a stale built ``.next``/``dist`` page must not satisfy a
    source-level miss (nor swamp the scan). Returns (html_texts, meta_texts, scanned).

    Uses ``os.walk`` with in-place dir pruning so heavy trees (``node_modules``) are
    never materialized — ``rglob('*')`` would enumerate + sort the whole tree first."""
    html_texts: list[str] = []
    meta_texts: list[str] = []
    scanned = 0
    skip_dirs = _EXCLUDE_DIRS | _BUILD_DIRS
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place (and sort) so we never descend into vendored/build dirs and
        # the traversal order is deterministic.
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for fname in sorted(filenames):
            if scanned >= _MAX_FILES:
                return html_texts, meta_texts, scanned
            ext = os.path.splitext(fname)[1].lower()
            if ext in _HTML_EXTS:
                txt = _read(Path(dirpath) / fname)
                if txt is not None:
                    html_texts.append(txt)
                    scanned += 1
            elif ext in _META_EXTS:
                txt = _read(Path(dirpath) / fname)
                if txt is not None:
                    meta_texts.append(txt)
                    scanned += 1
    return html_texts, meta_texts, scanned


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{re.escape(name)}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
    return m.group(1).strip() if m else None


def _meta_has_named(meta_tags: list[str], name: str) -> bool:
    """A ``<meta name="{name}" content="...non-empty...">`` (attribute order agnostic).

    Also accepts the Astro/JSX expression form ``content={identifier}``: a bound
    prop is a real description when the layout defaults it (our own Astro scaffold
    ships ``content={description}`` with ``description = title`` as the default).
    """
    for tag in meta_tags:
        n = _attr(tag, "name")
        if n and n.lower() == name and (_attr(tag, "content") or ""):
            return True
        if n and n.lower() == name and re.search(r"\bcontent\s*=\s*\{[^{}]+\}", tag):
            return True
    return False


def _meta_has_og(meta_tags: list[str], prop: str) -> bool:
    """A literal Open Graph ``<meta property="og:...">`` (some emitters use name=)."""
    for tag in meta_tags:
        p = _attr(tag, "property") or _attr(tag, "name")
        if p and p.lower() == prop and (_attr(tag, "content") or ""):
            return True
    return False


# A file is a real metadata SOURCE only if it carries one of these structural markers.
# Deliberately stricter than a bare "metadata"/"helmet" substring: a stray word in a
# comment or an unrelated config key must NOT qualify a file (else a random ``title:``
# is read as a page title). Covers Next.js App Router (``export const metadata`` /
# ``generateMetadata``), next/head + react-helmet, and the Remix meta-export idiom.
_METADATA_SOURCE_RE = re.compile(
    r"export\s+(?:const|let|var)\s+metadata\b"      # Next.js App Router metadata export
    r"|generateMetadata\b"                          # Next.js dynamic metadata
    r"|['\"]next/head['\"]"                          # import/require 'next/head'
    r"|<Head\b"                                      # <Head> (next/head)
    r"|react-helmet"                                 # react-helmet import
    r"|<Helmet\b"                                    # <Helmet> usage
    r"|export\s+(?:const|let|var)\s+meta\b"         # Remix: export const meta = () => [...]
    r"|\bMetaFunction\b",                            # Remix: typed MetaFunction
    re.I,
)

# A metadata title as a plain string (``title: "..."``) OR the Next.js object form
# (``title: { default: "...", template: "%s | X" }`` / ``absolute``).
_META_TITLE_STR_RE = re.compile(r"\btitle\s*:\s*[\"'`]\s*\S", re.I)
_META_TITLE_OBJ_RE = re.compile(
    r"\btitle\s*:\s*\{[^}]*\b(?:default|absolute)\b\s*:\s*[\"'`]\s*\S", re.I | re.S)

# A metadata description: the Next.js ``description: "..."`` key, OR the Remix
# meta-array object form ``{ name: "description", content: "..." }`` (attribute-order
# agnostic, non-empty content, tolerant of whitespace/newlines within ~200 chars).
_META_DESC_KEY_RE = re.compile(r"\bdescription\s*:\s*[\"'`]\s*\S", re.I)
_META_DESC_OBJ_RE = re.compile(
    r"name\s*:\s*[\"']description[\"'][\s\S]{0,200}?content\s*:\s*[\"'`]\s*\S"
    r"|content\s*:\s*[\"'`]\s*\S[\s\S]{0,200}?name\s*:\s*[\"']description[\"']",
    re.I,
)

_OG_OPEN_RE = re.compile(r"opengraph\s*:\s*\{", re.I)


def _is_metadata_source(text: str) -> bool:
    """True when ``text`` carries a real metadata-source marker (see
    :data:`_METADATA_SOURCE_RE`) — the context gate for the framework metadata idioms."""
    return bool(_METADATA_SOURCE_RE.search(text))


def _has_metadata_title(meta_texts: list[str]) -> bool:
    """A Next.js/Remix metadata ``title`` (string or object form) in a real metadata
    source. A stray ``title:`` in unrelated config never counts (context-gated)."""
    for t in meta_texts:
        if not _is_metadata_source(t):
            continue
        if _META_TITLE_STR_RE.search(t) or _META_TITLE_OBJ_RE.search(t):
            return True
    return False


def _has_metadata_description(meta_texts: list[str]) -> bool:
    """A Next.js ``description`` key OR a Remix ``{ name: "description", content }``
    object, in a real metadata source (context-gated)."""
    for t in meta_texts:
        if not _is_metadata_source(t):
            continue
        if _META_DESC_KEY_RE.search(t) or _META_DESC_OBJ_RE.search(t):
            return True
    return False


def _extract_braced_block(text: str, brace_index: int) -> str:
    """Return the balanced ``{...}`` substring starting at ``brace_index`` (which must
    point at a ``{``). Best-effort: an unbalanced block returns the remainder. Used to
    scope an ``openGraph:`` block so a key search can't leak into later, unrelated text."""
    depth = 0
    n = len(text)
    i = brace_index
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace_index:i + 1]
        i += 1
    return text[brace_index:]


def _has_opengraph_key(meta_texts: list[str], key: str) -> bool:
    """A ``key`` (e.g. ``title`` / ``description``) INSIDE an ``openGraph: { ... }``
    block. Brace-counted so the search is confined to the actual balanced block (og
    blocks nest, e.g. ``images: [{...}]``) and never matches a later unrelated key."""
    key_re = re.compile(rf"\b{re.escape(key)}\b", re.I)
    for t in meta_texts:
        for m in _OG_OPEN_RE.finditer(t):
            block = _extract_braced_block(t, m.end() - 1)  # m.end()-1 points at the '{'
            if key_re.search(block):
                return True
    return False


def _find_any(root: Path, patterns: tuple[str, ...]) -> bool:
    for pat in patterns:
        try:
            for p in root.glob(pat):
                if p.exists():
                    return True
        except Exception:  # noqa: BLE001 - a bad glob must never break the scan
            pass
    return False


def check_seo(project_dir: str | Path, stack: str = "") -> SeoVerdict:
    """Deterministically scan a delivered web build for the cheap, unambiguous SEO
    signals and return an ADVISORY :class:`SeoVerdict`.

    Checks (hard ``issues``): a non-empty <title> (literal OR Next.js metadata.title OR a
    next/head/helmet title); a meta description (<meta name="description"> OR
    metadata.description OR og:description); at least one <h1>; an <html lang> attribute.
    Softer ``warnings``: Open Graph basics (og:title / og:description); <img> tags missing
    an ``alt`` (counted); a robots.txt / sitemap.xml in public/ or the build output.

    ``ok`` is True when the scan ran with no hard issues — it is NOT a gate. Soft-skips
    (``skipped=True``, ``ok=False``, no gaps) for a non-web stack, a project with no
    HTML-producing files, or any error (degrade open, never false-flag). Never raises.
    """
    try:
        s = (stack or "").strip().lower()
        if s and s not in _SEO_WEB_STACKS:
            return SeoVerdict(skipped=True, reason=f"stack '{stack}' is not an HTML/web stack")
        root = Path(project_dir)
        if not root.is_dir():
            return SeoVerdict(skipped=True, reason="project dir is not a directory")

        html_texts, meta_texts, files_scanned = _collect(root)
        if not html_texts and not meta_texts:
            return SeoVerdict(skipped=True, reason="no HTML-producing files found")

        corpus = "\n".join(html_texts + meta_texts)
        meta_tags = _META_TAG_RE.findall(corpus)

        has_title = bool(_TITLE_LITERAL_RE.search(corpus)) or _has_metadata_title(meta_texts)
        has_og_title = _meta_has_og(meta_tags, "og:title") or _has_opengraph_key(meta_texts, "title")
        has_og_description = (_meta_has_og(meta_tags, "og:description")
                              or _has_opengraph_key(meta_texts, "description"))
        has_meta_description = (_meta_has_named(meta_tags, "description")
                                or _has_metadata_description(meta_texts))
        has_description = has_meta_description or has_og_description
        has_lang = bool(_LANG_RE.search(corpus))
        h1_count = len(_H1_RE.findall(corpus))

        imgs = _IMG_RE.findall(corpus)
        images_total = len(imgs)
        images_missing_alt = sum(1 for t in imgs if not _ALT_RE.search(t))

        has_robots = _find_any(root, _ROBOTS_PATTERNS)
        has_sitemap = _find_any(root, _SITEMAP_PATTERNS)

        issues: list[str] = []
        if not has_title:
            issues.append("missing a non-empty <title>")
        if not has_description:
            issues.append("missing a meta description")
        if not h1_count:
            issues.append("no <h1> heading found in the page source")
        if not has_lang:
            issues.append("missing a lang attribute on <html>")

        warnings: list[str] = []
        if not (has_og_title and has_og_description):
            warnings.append("weak Open Graph metadata (og:title / og:description) for social sharing")
        if images_missing_alt:
            warnings.append(
                f"{images_missing_alt} <img> tag(s) missing an alt attribute "
                "(accessibility + image SEO)"
            )
        if not has_robots:
            warnings.append("no robots.txt found in public/ or build output")
        if not has_sitemap:
            warnings.append("no sitemap.xml found in public/ or build output")

        checked = {
            "title": has_title,
            "description": has_description,
            "h1_count": h1_count,
            "lang": has_lang,
            "og_title": has_og_title,
            "og_description": has_og_description,
            "images_total": images_total,
            "images_missing_alt": images_missing_alt,
            "robots": has_robots,
            "sitemap": has_sitemap,
            "files_scanned": files_scanned,
        }
        return SeoVerdict(issues=issues, warnings=warnings, checked=checked)
    except Exception as exc:  # noqa: BLE001 - a checker must never break a build
        return SeoVerdict(skipped=True, reason=f"seo check error: {exc}")
