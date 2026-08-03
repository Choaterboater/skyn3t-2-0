"""Divergence-seeded design directions for best-of-N trajectories.

Plain best-of-N runs N code trajectories off ONE brief-derived token set, so
the candidates only sample *implementation* luck — the aesthetic is identical
and proof can rarely tell them apart on design. This module gives trajectory
``i`` a DISTINCT design seed (accent/font/style/archetype rotated off the
brief's derived picks), so candidates diverge aesthetically and the proof-tie
break (vision judge, see best_of_n) actually compares different designs.

Variant 0 is the CONTROL: its ``tokens_md`` is byte-identical to
``design_md_block(brief)`` and its axes are exactly the brief-derived picks.
That keeps the default single-build path untouched and gives every best-of-N
run one candidate generated under today's behavior, so divergence can never
do worse than the status quo — the winner is chosen by proof either way.

Every variant keeps the AA guarantees of ``derive_tokens``: ``--accent-text``
is fitted against the variant theme's ``--surface-2`` (the worst-case surface
for the text direction) with the same 12-step fit the base theme uses.

Import has zero side effects.
"""

from __future__ import annotations

import hashlib

from skyn3t.studio import design_tokens as dt
from skyn3t.studio.design_tokens import (
    derive_accent,
    derive_archetype,
    derive_font_pair,
    derive_style,
    derive_theme,
    design_md_block,
)


def _stable_index(text: str, modulo: int) -> int:
    """Local copy of design_tokens' stable-hash pick (md5 of the normalized
    text mod ``modulo``) — same algorithm, so salted picks here decorrelate
    from the base derives exactly the way the per-axis salts there do."""
    digest = hashlib.md5(
        text.strip().lower().encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return int(digest, 16) % modulo


def _rotated(items, current, step: int, *, salt: str):
    """The item ``step`` positions after ``current`` in ``items``. When
    ``current`` is not in the list (a keyword-matched pick from outside the
    rotation), the base position is the same stable hash the derive would
    have used, so the rotation still starts from the brief's own slot."""
    seq = list(items)
    if not seq:
        return current
    try:
        base = seq.index(current)
    except ValueError:
        base = _stable_index(salt, len(seq))
    return seq[(base + step) % len(seq)]


def _control_seed(brief: str) -> dict:
    """Variant 0 — the control. ``tokens_md`` is design_md_block(brief)
    VERBATIM so the default path stays byte-identical to pre-seed behavior."""
    heading, body = derive_font_pair(brief)
    return {
        "theme": derive_theme(brief),
        "accent": derive_accent(brief),
        "font_heading": heading,
        "font_body": body,
        "style": derive_style(brief),
        "archetype": derive_archetype(brief),
        "tokens_md": design_md_block(brief),
    }


def _variant_tokens(accent: str, theme_name: str, style_name: str) -> dict[str, str]:
    """derive_tokens-style assembly for an explicit (accent, theme, style)
    instead of the brief's picks — same AA fitting, same chart series."""
    tokens = dict(dt._THEMES[theme_name])
    dark_bg = dt._rel_luminance(tokens["--bg"]) < 0.18
    # --surface-2 is the worst-case surface for the text direction (darkest on
    # light themes, lightest on dark) — fit --accent-text against THAT.
    fit_bg = tokens["--surface-2"]
    tokens.update({
        "--accent": accent,
        "--accent-hover": dt._lighten(accent) if dark_bg else dt._darken(accent),
        "--accent-muted": accent + "22",
        "--accent-text": dt._fit_on_bg(accent, fit_bg),
        "--text-on-accent": dt._on_accent_text(accent),
    })
    radius_sm, radius, radius_lg = dt._STYLES[style_name]["radius"]
    tokens.update({
        "--radius-sm": f"{radius_sm}px",
        "--radius": f"{radius}px",
        "--radius-lg": f"{radius_lg}px",
    })
    for i in range(5):
        tokens[f"--chart-{i + 1}"] = dt._rotate_hue(accent, 60 * i)
    return tokens


def _render_block(
    theme: str,
    tokens: dict[str, str],
    heading: str,
    body: str,
    style_name: str,
    archetype: str,
) -> str:
    """A design_md_block-equivalent render for an explicit axis set. Mirrors
    design_tokens.design_md_block's format so a seeded prompt reads exactly
    like the control's, just with different values."""
    style = dt._STYLES[style_name]
    lines = "\n".join(f"  {k}: {v};" for k, v in tokens.items())
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
        f"  --font-heading: '{heading}', {dt._heading_fallback(heading)};\n"
        f"  --font-body: '{body}', {dt._SANS_FALLBACK};\n"
        "Pair a characterful display with a quiet body; extreme heading weight "
        "contrast (700 vs 400, not 400 vs 500).\n"
        f"Shape language: {style_name} — radius scale via --radius tokens; "
        f"{style['density']} spacing; {style['depth']}.\n"
        f"LAYOUT ARCHETYPE: {archetype} — {dt._ARCHETYPES[archetype]['guidance']}. "
        "Compose the page from THIS shape, not a centered hero + 3-card grid.\n"
        "- BASE theme, not a straitjacket: tune type scale and imagery to fit THIS "
        "product — keep text/bg pairs at WCAG AA.\n"
        "- Apply the theme class to <html> BEFORE first paint (not in a "
        "useEffect); sync code-editor themes — no flash, no stuck white default.\n"
    )


def _variant_seed(brief: str, index: int) -> dict:
    text = (brief or "").lower()
    theme = derive_theme(brief)
    if index >= 3:
        # Later variants also leave the derived surface ramp — cycle to a
        # different theme so the field spans light/dark/warm/cool too.
        themes = list(dt._THEMES)
        theme = themes[(themes.index(theme) + (index - 2)) % len(themes)]
    accent = _rotated(dt._ACCENT_ROTATION, derive_accent(brief), index, salt=text)
    heading, body = _rotated(
        dt._FONT_ROTATION, derive_font_pair(brief), index, salt=text + ":fonts")
    style_name = _rotated(list(dt._STYLES), derive_style(brief), index, salt=text + ":style")
    archetype = _rotated(
        list(dt._ARCHETYPES), derive_archetype(brief), index, salt=text + ":archetype")
    tokens = _variant_tokens(accent, theme, style_name)
    return {
        "theme": theme,
        "accent": accent,
        "font_heading": heading,
        "font_body": body,
        "style": style_name,
        "archetype": archetype,
        "tokens_md": _render_block(theme, tokens, heading, body, style_name, archetype),
    }


def design_seed_for(brief: str, index: int) -> dict:
    """The design seed for best-of-N trajectory ``index``: a payload with the
    variant's theme/accent/font pair/style/archetype plus ``tokens_md``, a
    ready-to-inject design_md_block-equivalent string for THAT variant.

    Index 0 is the control (byte-identical to today's behavior); indices 1+
    rotate each aesthetic axis deterministically off the brief's derived
    picks. A seed error never breaks a build: any failure falls back to the
    control seed."""
    brief = brief or ""
    try:
        index = max(0, int(index))
    except (TypeError, ValueError):
        index = 0
    if index == 0:
        return _control_seed(brief)
    try:
        return _variant_seed(brief, index)
    except Exception:  # noqa: BLE001 - seeding is best-effort, never a gate
        return _control_seed(brief)
