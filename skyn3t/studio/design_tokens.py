"""design.md-style design tokens + a WCAG contrast lint.

Two jobs, both aimed at making generated UI look *designed* and *accessible*:

1. ``derive_tokens`` / ``design_md_block`` — turn a brief into a concrete, branded,
   AA-contrast token set + font pairing + shape preset + layout archetype and
   render it as a DESIGN.md-style block to inject into codegen, so the model
   themes from one source of truth instead of ad-hoc hex and the same two system
   fonts.
2. ``lint_contrast`` — scan a built project's CSS and flag text/background pairs that
   fail WCAG AA, so the "white-by-default / unreadable" class of bug gets caught.

The brief picks a surface THEME (light by default; dark only when the brief implies
it), a brand ACCENT (whole-word keyword match, else a stable rotation over curated
hues), a FONT PAIR, a STYLE (radius/density/depth preset) and a LAYOUT ARCHETYPE —
the same keyword-or-stable-rotation mechanism each time, with a different hash salt
per axis so the five picks decorrelate. ``--accent-text`` is the accent shifted
until it clears AA on the theme's ``--bg``, so the "links in brand color" guidance
stays honest on every theme; ``--text-on-accent`` is the better of white/near-black
on the accent itself. All keyword matching is whole-word: "aircraft" must
not match "craft", "plumber" must not match "plum".
"""

from __future__ import annotations

import colorsys
import hashlib
import re
from pathlib import Path

# Accent hue picked from the brief/name. First whole-word match wins; otherwise a
# stable rotation over _ACCENT_ROTATION — never one fixed default for every app.
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

# Rotation for briefs that imply no color: a spread of hues so two unrelated
# apps never converge on the same brand color by default. The indigo/violet
# family is deliberately absent — it reads as the AI-default look.
_ACCENT_ROTATION: tuple[str, ...] = (
    "#0f766e",  # deep teal
    "#c2410c",  # burnt orange
    "#1d4ed8",  # royal blue
    "#be123c",  # crimson
    "#15803d",  # forest green
    "#a16207",  # ochre
)

# Neutral ramps. The brief picks a theme; text/surface pairs inside a theme are
# fixed so text-on-bg always passes WCAG AA.
_THEMES: dict[str, dict[str, str]] = {
    "paper": {  # light neutral — the default
        "--bg": "#f8f7f5", "--surface": "#ffffff", "--surface-2": "#f0eeea",
        "--border": "#e0dcd4", "--text": "#1c1917", "--text-muted": "#57534e",
        "--danger": "#dc2626", "--good": "#15803d",
    },
    "slate": {  # light cool — corporate / data / professional briefs
        "--bg": "#f6f8fa", "--surface": "#ffffff", "--surface-2": "#eaeef2",
        "--border": "#d0d7de", "--text": "#1f2937", "--text-muted": "#4b5563",
        "--danger": "#dc2626", "--good": "#15803d",
    },
    "sand": {  # light warm — hospitality / craft / editorial briefs
        "--bg": "#f5f0e8", "--surface": "#fffdf8", "--surface-2": "#ece4d6",
        "--border": "#ddd2c0", "--text": "#292524", "--text-muted": "#57534e",
        "--danger": "#dc2626", "--good": "#15803d",
    },
    "ink": {  # dark — only when the brief implies it
        "--bg": "#0d1117", "--surface": "#161b22", "--surface-2": "#21262d",
        "--border": "#30363d", "--text": "#e6edf3", "--text-muted": "#9aa4b2",
        "--danger": "#f85149", "--good": "#3fb950",
    },
}

# Dark is a deliberate choice, not a default: only these brief hints get "ink".
_DARK_KEYWORDS = (
    "dark", "night", "terminal", "crypto", "neon", "gaming", "code editor",
    "devtool", "dev tool",
)
_WARM_KEYWORDS = (
    "bakery", "cafe", "coffee", "restaurant", "warm", "craft", "artisan",
    "editorial", "magazine", "wedding", "florist", "farm",
)
_COOL_KEYWORDS = (
    "corporate", "finance", "fintech", "medical", "health", "legal", "saas",
    "analytics", "invoice", "insurance", "bank", "enterprise",
)

# Curated heading/body font pairs (Google Fonts), grouped by feel. Brief keywords
# win; otherwise a stable rotation over _FONT_ROTATION. Inter is deliberately
# absent everywhere — it reads as the AI-default face.
_FONT_PAIRS: list[tuple[tuple[str, ...], str, str]] = [
    (("blog", "editorial", "magazine", "news", "journal", "writing", "writer"),
     "Playfair Display", "Source Sans 3"),
    (("bakery", "cafe", "coffee", "restaurant", "food", "artisan", "craft"),
     "Fraunces", "Work Sans"),
    (("docs", "documentation", "developer", "api", "devtool", "dev tool",
      "engineering"),
     "Space Grotesk", "IBM Plex Sans"),
    (("saas", "startup", "platform", "dashboard", "tool"),
     "Sora", "DM Sans"),
    (("hotel", "luxury", "fashion", "wedding", "beauty", "spa"),
     "Cormorant Garamond", "Spectral"),
    (("fitness", "gym", "sports", "gaming", "esports", "club"),
     "Archivo", "Manrope"),
    (("portfolio", "studio", "gallery", "photography", "artist"),
     "Outfit", "Newsreader"),
    (("retro", "y2k", "vintage", "arcade"),
     "Archivo Black", "Space Grotesk"),
    (("finance", "fintech", "legal", "insurance", "bank", "medical",
      "enterprise", "corporate"),
     "Libre Franklin", "Source Sans 3"),
    (("kids", "children", "school", "education", "learning"),
     "Baloo 2", "Nunito"),
]
_FONT_ROTATION: tuple[tuple[str, str], ...] = (
    ("Sora", "DM Sans"),
    ("Fraunces", "Work Sans"),
    ("Space Grotesk", "IBM Plex Sans"),
    ("Playfair Display", "Source Sans 3"),
    ("Outfit", "Newsreader"),
    ("Archivo", "Manrope"),
)
_SERIF_HEADINGS = {
    "Playfair Display", "Fraunces", "Cormorant Garamond", "Newsreader", "Spectral",
}
_SANS_FALLBACK = "system-ui, -apple-system, 'Segoe UI', sans-serif"

# Shape-language presets: each style pins a radius scale (--radius-sm/--radius/
# --radius-lg), a spacing density, and a depth treatment, so a brutalist zine and
# a friendly kids app do not ship the same 8px-rounded, one-shadow chrome. First
# whole-word keyword match wins; otherwise a stable rotation with its own salt
# (":style") so the pick decorrelates from accent and font.
_STYLES: dict[str, dict] = {
    "sharp brutalist": {
        "keywords": ("brutalist", "brutal", "sharp"),
        "radius": (0, 2, 2),
        "density": "compact",
        "depth": "flat, hard 2px borders, no shadows",
    },
    "compact workspace": {
        "keywords": ("dashboard", "workspace", "corporate", "admin", "tool"),
        "radius": (4, 6, 8),
        "density": "compact",
        "depth": "one soft shadow layer",
    },
    "soft editorial": {
        "keywords": ("editorial", "magazine", "journal", "blog"),
        "radius": (8, 10, 12),
        "density": "spacious",
        "depth": "soft diffuse shadows",
    },
    "rounded friendly": {
        "keywords": ("friendly", "kids", "children", "social", "community"),
        "radius": (10, 12, 16),
        "density": "regular",
        "depth": "soft shadows, slight hover lift",
    },
    "pill playful": {
        "keywords": ("playful", "game", "gaming", "arcade", "fun"),
        "radius": (12, 16, 999),
        "density": "regular",
        "depth": "bold 2px borders, chunky offset shadows",
    },
    "minimal flat": {
        "keywords": ("minimal", "flat", "clean", "simple"),
        "radius": (2, 4, 4),
        "density": "spacious",
        "depth": "none — whitespace alone separates surfaces",
    },
}

# Page-level composition picked from the brief: the archetype fixes the page's
# skeleton BEFORE components, so every app is not a centered hero + 3-card grid.
# Keyword match first; otherwise a stable rotation with its own salt
# (":archetype"). Guidance is one line — prompt real estate is scarce.
_ARCHETYPES: dict[str, dict] = {
    "masonry gallery": {
        "keywords": ("gallery",),
        "guidance": "packed varied-height media columns, images lead",
    },
    "sidebar workspace": {
        "keywords": ("dashboard", "tool"),
        "guidance": "nav rail + dense work area of tables, filters, charts",
    },
    "full-bleed immersive": {
        "keywords": ("game", "gaming"),
        "guidance": "edge-to-edge scene with UI overlaid like a HUD",
    },
    "longform single column": {
        "keywords": ("blog", "docs"),
        "guidance": "one ~70ch reading column, generous rhythm, asides",
    },
    "asymmetric hero": {
        "keywords": ("landing", "launch"),
        "guidance": "oversized off-center headline against a staggered visual",
    },
    "bento grid": {
        "keywords": ("store", "shop"),
        "guidance": "uneven tile mosaic mixing hero, stat and content cells",
    },
    "magazine": {
        "keywords": ("magazine", "news"),
        "guidance": "editorial grid led by one dominant feature story, varied spans",
    },
    "split-screen": {
        "keywords": ("portfolio",),
        "guidance": "two rigid halves — visual/story one side, content the other",
    },
}


def _stable_index(text: str, modulo: int) -> int:
    digest = hashlib.md5(text.strip().lower().encode("utf-8")).hexdigest()
    return int(digest, 16) % modulo


def _word_match(text: str, keyword: str) -> bool:
    """Whole-word/whole-phrase match: 'aircraft' must not hit 'craft',
    'swarm' must not hit 'warm', 'plumber' must not hit 'plum'."""
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def derive_theme(brief: str) -> str:
    """Pick the surface theme name from the brief. Light by default; dark only
    when the brief genuinely implies it (so every app is not the same dark UI)."""
    text = (brief or "").lower()
    if any(_word_match(text, k) for k in _DARK_KEYWORDS):
        return "ink"
    if any(_word_match(text, k) for k in _WARM_KEYWORDS):
        return "sand"
    if any(_word_match(text, k) for k in _COOL_KEYWORDS):
        return "slate"
    return "paper"


def _darken(hex_color: str, factor: float = 0.82) -> str:
    r, g, b = _to_rgb(hex_color)
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def _lighten(hex_color: str, factor: float = 0.82) -> str:
    r, g, b = _to_rgb(hex_color)
    return "#%02x%02x%02x" % tuple(int(c + (255 - c) * (1 - factor)) for c in (r, g, b))


def _on_accent_text(accent: str) -> str:
    """Text color for accent fills: the better-contrasting of white vs near-black
    against the accent. Every curated accent has at least a 4.6:1 option, and the
    lint checks --text-on-accent against --accent (not --bg), so the AA guarantee
    on accent fills stays structural."""
    return max(("#ffffff", "#1c1917"), key=lambda c: contrast_ratio(c, accent))


def _rotate_hue(hex_color: str, degrees: float) -> str:
    """Spin the hue while keeping the accent's saturation, clamping lightness into
    a readable 0.35-0.65 band. Used for chart series (data fills, not text)."""
    r, g, b = _to_rgb(hex_color)
    h, lightness, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    h = (h + degrees / 360.0) % 1.0
    lightness = min(0.65, max(0.35, lightness))
    cr, cg, cb = colorsys.hls_to_rgb(h, lightness, s)
    return f"#{round(cr * 255):02x}{round(cg * 255):02x}{round(cb * 255):02x}"


def _fit_on_bg(hex_color: str, bg: str, *, minimum: float = 4.5) -> str:
    """Shift a color darker (on light bg) or lighter (on dark bg) until the pair
    clears the WCAG ratio. Best-effort within 12 steps; converges for any sane
    accent because each step moves ~20% toward black/white."""
    if contrast_ratio(hex_color, bg) >= minimum:
        return hex_color
    dark_bg = _rel_luminance(bg) < 0.18
    out = hex_color
    for _ in range(12):
        out = _lighten(out) if dark_bg else _darken(out)
        if contrast_ratio(out, bg) >= minimum:
            return out
    return out


def derive_accent(brief: str) -> str:
    """Pick one brand accent from the brief text (whole-word match). No color
    implied -> a stable rotation over curated hues, so unrelated apps do not
    share one default."""
    text = (brief or "").lower()
    for keywords, hexv in _ACCENT_KEYWORDS:
        if any(_word_match(text, k) for k in keywords):
            return hexv
    return _ACCENT_ROTATION[_stable_index(text, len(_ACCENT_ROTATION))]


def derive_font_pair(brief: str) -> tuple[str, str]:
    """Pick a (heading, body) Google-Fonts pair: whole-word keyword match by
    feel, else a stable rotation. Never Inter."""
    text = (brief or "").lower()
    for keywords, heading, body in _FONT_PAIRS:
        if any(_word_match(text, k) for k in keywords):
            return heading, body
    return _FONT_ROTATION[_stable_index(text + ":fonts", len(_FONT_ROTATION))]


def derive_style(brief: str) -> str:
    """Pick the shape-language preset name from the brief: whole-word keyword
    match, else a stable rotation salted ':style' so it decorrelates from the
    accent and font picks."""
    text = (brief or "").lower()
    for name, style in _STYLES.items():
        if any(_word_match(text, k) for k in style["keywords"]):
            return name
    names = list(_STYLES)
    return names[_stable_index(text + ":style", len(names))]


def derive_archetype(brief: str) -> str:
    """Pick the page archetype name from the brief: whole-word keyword match,
    else a stable rotation salted ':archetype'."""
    text = (brief or "").lower()
    for name, archetype in _ARCHETYPES.items():
        if any(_word_match(text, k) for k in archetype["keywords"]):
            return name
    names = list(_ARCHETYPES)
    return names[_stable_index(text + ":archetype", len(names))]


def derive_tokens(brief: str) -> dict[str, str]:
    """A complete token set: the brief's theme ramp plus one brand accent, the
    shape preset's radius scale, semantic on-accent/chart colors.
    Text-on-surface pairs pass WCAG AA in every theme by construction, and
    --accent-text is the accent itself shifted until it clears AA on --bg, so
    accent-colored text/links stay readable on light AND dark themes."""
    accent = derive_accent(brief)
    theme = derive_theme(brief)
    tokens = dict(_THEMES[theme])
    dark_bg = _rel_luminance(tokens["--bg"]) < 0.18
    # Fit against the worst-case surface for the text direction: --surface-2 is
    # the DARKEST surface on light themes (worst for darkened text) and the
    # LIGHTEST on dark themes (worst for lightened text). The lint below checks
    # every text/bg token pair, so --accent-text must clear AA everywhere.
    fit_bg = tokens["--surface-2"]
    tokens.update({
        "--accent": accent,
        "--accent-hover": _lighten(accent) if dark_bg else _darken(accent),
        "--accent-muted": accent + "22",  # 13% alpha for subtle fills
        "--accent-text": _fit_on_bg(accent, fit_bg),
        "--text-on-accent": _on_accent_text(accent),
    })
    radius_sm, radius, radius_lg = _STYLES[derive_style(brief)]["radius"]
    tokens.update({
        "--radius-sm": f"{radius_sm}px",
        "--radius": f"{radius}px",
        "--radius-lg": f"{radius_lg}px",
    })
    # Data-viz series spun off the accent hue, 60 degrees apart. These are data
    # FILLS (bars, lines, pie slices), never text: exempt from the AA text
    # guarantee by design.
    for i in range(5):
        tokens[f"--chart-{i + 1}"] = _rotate_hue(accent, 60 * i)
    return tokens


def _heading_fallback(heading: str) -> str:
    if heading in _SERIF_HEADINGS:
        return "Georgia, 'Times New Roman', serif"
    if heading == "Archivo Black":
        return "'Arial Black', sans-serif"
    return _SANS_FALLBACK


def design_md_block(brief: str) -> str:
    """A DESIGN.md-style instruction block to inject into the codegen prompt."""
    theme = derive_theme(brief)
    t = derive_tokens(brief)
    heading, body = derive_font_pair(brief)
    style_name = derive_style(brief)
    style = _STYLES[style_name]
    archetype = derive_archetype(brief)
    lines = "\n".join(f"  {k}: {v};" for k, v in t.items())
    hq = heading.replace(" ", "+")
    bq = body.replace(" ", "+")
    weights = "" if heading == "Archivo Black" else ":wght@400;500;700"
    bweights = "" if body == "Archivo Black" else ":wght@400;500;700"
    return (
        f"## DESIGN TOKENS — base theme '{theme}'\n"
        "Define these CSS variables once in `:root` and reference them everywhere — "
        "never hard-code hex. Text/bg pairs ship WCAG-AA checked.\n"
        f"```css\n:root {{\n{lines}\n}}\n```\n"
        "- `--accent` = brand color for FILLED elements (buttons, chips, nav) "
        "with `--text-on-accent` on top; `--accent-hover` for hover.\n"
        "- `--accent-text` = accent tuned for AA on `--bg`: links, focus rings, "
        "ANY accent-colored text.\n"
        "- `--text-on-accent` = text on `--accent` fills; `--chart-1`..`--chart-5` = "
        "data series (never improvise chart hex).\n"
        "- `--bg`/`--surface`/`--surface-2` = page/panel/raised; `--border` = dividers; "
        "`--text`/`--text-muted` on those, `--danger`/`--good` = state.\n"
        f"Typography: heading '{heading}', body '{body}':\n"
        f"  <link href=\"https://fonts.googleapis.com/css2?family={hq}{weights}"
        f"&family={bq}{bweights}&display=swap\" rel=\"stylesheet\">\n"
        "stacks (offline-safe fallbacks):\n"
        f"  --font-heading: '{heading}', {_heading_fallback(heading)};\n"
        f"  --font-body: '{body}', {_SANS_FALLBACK};\n"
        "Pair a characterful display with a quiet body; extreme heading weight "
        "contrast (700 vs 400, not 400 vs 500).\n"
        f"Shape language: {style_name} — radius scale via --radius tokens; "
        f"{style['density']} spacing; {style['depth']}.\n"
        f"LAYOUT ARCHETYPE: {archetype} — {_ARCHETYPES[archetype]['guidance']}. "
        "Compose the page from THIS shape, not a centered hero + 3-card grid.\n"
        "- BASE theme, not a straitjacket: tune type scale and imagery to fit THIS "
        "product — keep text/bg pairs at WCAG AA.\n"
        "- Apply the theme class to <html> BEFORE first paint (not in a "
        "useEffect); sync code-editor themes — no flash, no stuck white default.\n"
    )


# ---- DESIGN.md persistence (anti-drift) -----------------------------------------
#
# The design direction a build was generated with is delivered as DESIGN.md in
# the project tree, so `skyn3t studio improve` re-reads the SAME direction
# instead of drifting the palette/fonts/layout across runs (the GitHub Spark
# prd.md pattern). The header doubles as the ownership marker: a DESIGN.md the
# build itself did not write (codegen's own file) is never clobbered.

DESIGN_MD_NAME = "DESIGN.md"
DESIGN_MD_HEADER = (
    "Generated by SkyN3t — improve runs re-read this file; keep it in sync "
    "with intentional design changes"
)


def render_design_md(brief: str, design_summary: str = "") -> str:
    """The DESIGN.md content for a delivered web build: the deterministic token
    block verbatim, plus the designer stage's one-line direction when there is one."""
    parts = [DESIGN_MD_HEADER, "", design_md_block(brief)]
    if design_summary:
        parts += ["", "## Design direction", design_summary]
    return "\n".join(parts) + "\n"


def write_design_md(root: str | Path, brief: str, design_summary: str = "") -> bool:
    """Write DESIGN.md into the delivered project tree. Writes only when the
    file is absent or is one we wrote before (the header marker is ours) — a
    DESIGN.md codegen produced itself always wins. Returns True when written."""
    path = Path(root) / DESIGN_MD_NAME
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if not existing.startswith(DESIGN_MD_HEADER):
            return False
    path.write_text(render_design_md(brief, design_summary), encoding="utf-8")
    return True


def read_design_md(root: str | Path, *, max_chars: int = 4000) -> str:
    """The project's DESIGN.md, bounded for prompt context. '' when absent."""
    try:
        text = (Path(root) / DESIGN_MD_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:max_chars].strip()


# ---- WCAG contrast lint ---------------------------------------------------------

def _to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rel_luminance(hex_color: str) -> float:
    def chan(c: float) -> float:
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
            # A "--text-on-accent"-style var names its own background: check THAT
            # pair (it never sits on --bg, so comparing it to every bg would be a
            # false alarm). Skip when the named bg var is absent — under-report
            # rather than guess.
            on = re.search(r"\bon-([\w-]+)$", tk)
            if on:
                target = f"--{on.group(1)}"
                pairs = [(target, vars_[target])] if target in vars_ else []
            else:
                pairs = list(bgs.items())
            for bk, bv in pairs:
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
