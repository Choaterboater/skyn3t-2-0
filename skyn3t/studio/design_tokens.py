"""design.md-style design tokens + a WCAG contrast lint.

Two jobs, both aimed at making generated UI look *designed* and *accessible*:

1. ``derive_tokens`` / ``design_md_block`` — turn a brief into a concrete, branded,
   AA-contrast token set and render it as a DESIGN.md-style block to inject into
   codegen, so the model themes from one source of truth instead of ad-hoc hex.
2. ``lint_contrast`` — scan a built project's CSS and flag text/background pairs that
   fail WCAG AA, so the "white-by-default / unreadable" class of bug gets caught.

Neutral surfaces are fixed and high-contrast by construction; only the ACCENT varies
by brief — so the derived set always passes the lint, and green is never forced onto
an app that didn't ask for it.
"""

from __future__ import annotations

import re
from pathlib import Path

# Accent hue picked from the brief/name. First match wins; neutral indigo default.
# Intentionally NOT green-by-default — green only when the brief implies it.
_ACCENT_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("green", "forest", "emerald", "mint", "lime", "eco"), "#16a34a"),
    (("blue", "ocean", "sky", "azure", "marine", "aqua"), "#2563eb"),
    (("red", "crimson", "scarlet", "ruby"), "#dc2626"),
    (("purple", "violet", "grape", "plum"), "#7c3aed"),
    (("indigo",), "#4f46e5"),
    (("gold", "amber", "orange", "sunset", "ember", "forge", "molten"), "#f59e0b"),
    (("teal", "cyan"), "#0d9488"),
    (("pink", "rose", "magenta", "blush"), "#db2777"),
    (("yellow", "honey"), "#eab308"),
    (("slate", "steel", "graphite", "mono"), "#64748b"),
]
_DEFAULT_ACCENT = "#6366f1"  # tasteful neutral indigo


def _darken(hex_color: str, factor: float = 0.82) -> str:
    r, g, b = _to_rgb(hex_color)
    return "#%02x%02x%02x" % (int(r * factor), int(g * factor), int(b * factor))


def derive_accent(brief: str) -> str:
    """Pick one brand accent from the brief text. Neutral indigo if nothing implied."""
    text = (brief or "").lower()
    for keywords, hexv in _ACCENT_KEYWORDS:
        if any(k in text for k in keywords):
            return hexv
    return _DEFAULT_ACCENT


def derive_tokens(brief: str) -> dict[str, str]:
    """A complete, AA-contrast dark token set. Only the accent varies by brief; the
    neutral surfaces/text are fixed so text-on-bg always passes WCAG AA."""
    accent = derive_accent(brief)
    return {
        "--accent": accent,
        "--accent-hover": _darken(accent),
        "--accent-muted": accent + "22",  # 13% alpha for subtle fills
        "--bg": "#0d1117",
        "--surface": "#161b22",
        "--surface-2": "#21262d",
        "--border": "#30363d",
        "--text": "#e6edf3",
        "--text-muted": "#9aa4b2",
        "--danger": "#f85149",
        "--good": "#3fb950",
    }


def design_md_block(brief: str) -> str:
    """A DESIGN.md-style instruction block to inject into the codegen prompt."""
    t = derive_tokens(brief)
    lines = "\n".join(f"  {k}: {v};" for k, v in t.items())
    return (
        "## DESIGN TOKENS (use these EXACTLY — single source of truth)\n"
        "Define these CSS variables once in `:root` and reference them everywhere; do "
        "NOT hard-code hex per component. They are WCAG-AA contrast-checked.\n"
        f"```css\n:root {{\n{lines}\n}}\n```\n"
        "- `--accent` = brand color: primary buttons, active state, links, focus rings, logo.\n"
        "- `--bg`/`--surface`/`--surface-2` = page/panel/raised surfaces; `--border` = dividers.\n"
        "- `--text` on `--bg`/`--surface`; `--text-muted` for secondary. `--danger`/`--good` for state.\n"
        "- Apply the theme class/attribute to <html> BEFORE first paint (not only in a useEffect), "
        "and sync any code-editor theme to it, so the app never flashes or sticks to a white default.\n"
    )


# ---- WCAG contrast lint ---------------------------------------------------------

def _to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rel_luminance(hex_color: str) -> float:
    def chan(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = _to_rgb(hex_color)
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(hex1: str, hex2: str) -> float:
    """WCAG contrast ratio (1..21). AA normal text needs >= 4.5."""
    l1, l2 = _rel_luminance(hex1), _rel_luminance(hex2)
    hi, lo = max(l1, l2), min(l1, l2)
    return round((hi + 0.05) / (lo + 0.05), 2)


_VAR_RE = re.compile(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\b")
_TEXTISH = ("text", "fg", "foreground", "ink", "copy", "body", "heading", "title")
_BGISH = ("bg", "background", "surface", "panel", "card", "base", "paper")


def lint_contrast(root: str | Path, *, threshold: float = 4.5) -> list[dict]:
    """Conservative WCAG-AA lint over a project's CSS. Returns issues for clear
    text-token vs bg-token pairs whose contrast is below ``threshold``. Only checks
    explicit hex CSS variables (no var() resolution / cascade), so it under-reports
    rather than crying wolf. Never raises."""
    issues: list[dict] = []
    try:
        files = [p for p in Path(root).rglob("*.css")
                 if "node_modules" not in p.parts and "dist" not in p.parts]
    except OSError:
        return issues
    for css in files[:40]:
        try:
            text = css.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        vars_ = {m.group(1).lower(): m.group(2) for m in _VAR_RE.finditer(text)}
        if not vars_:
            continue
        texts = {k: v for k, v in vars_.items() if any(t in k for t in _TEXTISH)}
        bgs = {k: v for k, v in vars_.items() if any(b in k for b in _BGISH)}
        for tk, tv in texts.items():
            for bk, bv in bgs.items():
                try:
                    ratio = contrast_ratio(tv, bv)
                except (ValueError, IndexError):
                    continue
                if ratio < threshold:
                    issues.append({
                        "file": str(css), "text": f"{tk}={tv}", "bg": f"{bk}={bv}",
                        "ratio": ratio, "needs": threshold,
                    })
    return issues
