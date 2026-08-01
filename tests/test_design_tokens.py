import re

from skyn3t.studio.design_tokens import (
    _ACCENT_ROTATION,
    _ARCHETYPES,
    _FONT_PAIRS,
    _FONT_ROTATION,
    _STYLES,
    _THEMES,
    contrast_ratio,
    derive_accent,
    derive_archetype,
    derive_font_pair,
    derive_style,
    derive_theme,
    derive_tokens,
    design_md_block,
    lint_contrast,
)


def test_accent_from_brief_keyword_match():
    assert derive_accent("a green text editor") == "#16a34a"
    assert derive_accent("an ocean weather app") == "#2563eb"
    assert derive_accent("COIN REAPER molten forge game") == "#f59e0b"


def test_accent_matching_is_whole_word():
    # "plumber" must not hit "plum", "whisky" must not hit "sky",
    # "inspired" must not hit "red" — these fall through to the rotation.
    assert derive_accent("a plumber's booking site") in _ACCENT_ROTATION
    assert derive_accent("a whisky tasting notes app") in _ACCENT_ROTATION
    assert derive_accent("an inspired-by-Bauhaus gallery") in _ACCENT_ROTATION


def test_accent_default_rotates_not_one_fixed_indigo():
    # nothing implies a color -> a stable pick from the curated rotation,
    # never the single AI-default indigo for every app
    pick = derive_accent("a contractor invoice tool")
    assert pick in _ACCENT_ROTATION
    assert pick != "#6366f1"
    assert derive_accent("a contractor invoice tool") == pick  # stable
    others = {derive_accent(f"an app about {w}") for w in ("knitting", "taxes", "boats")}
    assert others <= set(_ACCENT_ROTATION)


def test_theme_selection_defaults_light_and_varies():
    assert derive_theme("a contractor invoice tool") == "slate"  # invoice -> cool
    assert derive_theme("a recipe box for home cooks") == "paper"
    assert derive_theme("a cozy bakery site") == "sand"
    assert derive_theme("a dark neon crypto terminal") == "ink"


def test_theme_matching_is_whole_word():
    # "aircraft" must not hit "craft", "swarm" must not hit "warm"
    assert derive_theme("an aircraft tracking dashboard") != "sand"
    assert derive_theme("a swarm robotics fleet monitor") != "sand"


_THEME_BRIEFS = {
    "paper": "a recipe box for home cooks",
    "slate": "a contractor invoice tool",
    "sand": "a cozy bakery site",
    "ink": "a dark neon crypto terminal",
}


def test_derived_tokens_pass_AA_contrast():
    for theme, brief in _THEME_BRIEFS.items():
        t = derive_tokens(brief)
        # text on bg and on surface must clear WCAG AA (4.5)
        assert contrast_ratio(t["--text"], t["--bg"]) >= 4.5, theme
        assert contrast_ratio(t["--text"], t["--surface"]) >= 4.5, theme


def test_accent_text_clears_AA_on_every_theme():
    # The "links in brand color" guidance must stay honest: --accent-text is
    # fitted against the theme's worst-case surface.
    for theme, brief in _THEME_BRIEFS.items():
        t = derive_tokens(brief)
        assert contrast_ratio(t["--accent-text"], t["--bg"]) >= 4.5, theme
        assert contrast_ratio(t["--accent-text"], t["--surface"]) >= 4.5, theme


def test_accent_text_fits_hard_cases():
    # honey-on-sand was 1.69:1 before the fit; amber on paper 2.01:1.
    t = derive_tokens("a honey farm shop")
    assert contrast_ratio(t["--accent-text"], t["--surface"]) >= 4.5
    t = derive_tokens("an amber sunset gallery")
    assert contrast_ratio(t["--accent-text"], t["--surface"]) >= 4.5


def test_derived_tokens_lint_clean_on_themselves(tmp_path):
    # Our own token block must pass our own lint (every text/bg pair — including
    # --text-on-accent, which the lint checks against --accent, not --bg).
    for brief in _THEME_BRIEFS.values():
        t = derive_tokens(brief)
        css = ":root {\n" + "\n".join(f"  {k}: {v};" for k, v in t.items()) + "\n}\n"
        assert "--text-on-accent:" in css and "--chart-5:" in css  # really linted
        (tmp_path / "styles.css").write_text(css)
        assert lint_contrast(tmp_path) == [], brief


def test_font_pair_keyword_match():
    assert derive_font_pair("a cozy bakery site") == ("Fraunces", "Work Sans")
    assert derive_font_pair("a luxury boutique hotel") == ("Cormorant Garamond", "Spectral")
    assert derive_font_pair("api documentation portal") == ("Space Grotesk", "IBM Plex Sans")


def test_font_pair_rotation_is_stable_and_never_inter():
    pick = derive_font_pair("a contractor invoice tool")
    assert pick in {pair for _, pair in [((k, h, b), (h, b)) for k, h, b in _FONT_PAIRS]} or pick in _FONT_ROTATION
    assert derive_font_pair("a contractor invoice tool") == pick
    all_fonts = {f for _, h, b in _FONT_PAIRS for f in (h, b)} | {f for h, b in _FONT_ROTATION for f in (h, b)}
    assert "Inter" not in all_fonts


def test_contrast_ratio_known_values():
    assert contrast_ratio("#000000", "#ffffff") == 21.0
    assert contrast_ratio("#ffffff", "#ffffff") == 1.0


def test_design_md_block_has_tokens_fonts_and_prepaint_rule():
    block = design_md_block("a green text editor")
    assert "--accent: #16a34a" in block
    assert "--accent-text:" in block
    assert "fonts.googleapis.com" in block
    assert "--font-heading:" in block
    assert "--font-body:" in block
    assert "BEFORE first paint" in block  # the theme-flash lesson is baked in


def test_lint_flags_low_contrast(tmp_path):
    (tmp_path / "styles.css").write_text(
        ":root { --text-primary: #cccccc; --bg-primary: #ffffff; }")
    issues = lint_contrast(tmp_path)
    assert issues and issues[0]["ratio"] < 4.5


def test_lint_passes_good_contrast(tmp_path):
    (tmp_path / "styles.css").write_text(
        ":root { --text: #e6edf3; --bg: #0d1117; }")
    assert lint_contrast(tmp_path) == []


def test_style_keyword_match():
    assert derive_style("a brutalist poster zine") == "sharp brutalist"
    assert derive_style("an operations dashboard") == "compact workspace"
    assert derive_style("an editorial journal") == "soft editorial"
    assert derive_style("a friendly community app") == "rounded friendly"
    assert derive_style("a playful arcade game") == "pill playful"
    assert derive_style("a minimal landing page") == "minimal flat"


def test_style_matching_is_whole_word():
    # "minimalism" must not hit "minimal", "gamingly" must not hit "gaming"
    assert derive_style("a platform for minimalism") not in ("minimal flat",)
    assert derive_style("a platform for minimalism") in _STYLES


def test_style_rotation_is_stable_and_spreads():
    pick = derive_style("a recipe box for home cooks")
    assert pick in _STYLES
    assert derive_style("a recipe box for home cooks") == pick  # stable
    others = {derive_style(f"an app about {w}") for w in ("knitting", "taxes", "boats")}
    assert others <= set(_STYLES)


def test_tokens_include_style_radii():
    t = derive_tokens("a brutalist poster zine")
    assert (t["--radius-sm"], t["--radius"], t["--radius-lg"]) == ("0px", "2px", "2px")
    t = derive_tokens("an operations dashboard")
    assert (t["--radius-sm"], t["--radius"], t["--radius-lg"]) == ("4px", "6px", "8px")


def test_text_on_accent_clears_AA_on_accent():
    for theme, brief in _THEME_BRIEFS.items():
        t = derive_tokens(brief)
        assert t["--text-on-accent"] in ("#ffffff", "#1c1917")
        assert contrast_ratio(t["--text-on-accent"], t["--accent"]) >= 4.5, theme


def test_chart_tokens_are_five_distinct_valid_hex():
    for brief in _THEME_BRIEFS.values():
        t = derive_tokens(brief)
        charts = [t[f"--chart-{i}"] for i in range(1, 6)]
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in charts)
        assert len(set(charts)) == 5  # five distinct hues


def test_archetype_keyword_match():
    assert derive_archetype("a photo gallery for a studio") == "masonry gallery"
    assert derive_archetype("an operations dashboard") == "sidebar workspace"
    assert derive_archetype("an invoice tool") == "sidebar workspace"
    assert derive_archetype("a neon arcade game") == "full-bleed immersive"
    assert derive_archetype("a personal blog") == "longform single column"
    assert derive_archetype("a product landing page") == "asymmetric hero"
    assert derive_archetype("a coffee shop store") == "bento grid"
    assert derive_archetype("a tech news magazine") == "magazine"
    assert derive_archetype("a photography portfolio") == "split-screen"


def test_archetype_rotation_is_stable_and_spreads():
    pick = derive_archetype("a recipe box for home cooks")
    assert pick in _ARCHETYPES
    assert derive_archetype("a recipe box for home cooks") == pick  # stable
    others = {derive_archetype(f"an app about {w}") for w in ("knitting", "taxes", "boats")}
    assert others <= set(_ARCHETYPES)


def test_design_md_block_has_style_archetype_and_semantic_tokens():
    block = design_md_block("an operations dashboard")
    assert "--text-on-accent:" in block
    assert "--chart-5:" in block
    assert "--radius-lg:" in block
    assert "Shape language: compact workspace" in block
    assert "LAYOUT ARCHETYPE: sidebar workspace" in block
    assert "not a centered hero + 3-card grid" in block


def test_lint_checks_text_on_accent_against_accent_not_bg(tmp_path):
    # White --text-on-accent is correct on a dark accent fill: it must be checked
    # against --accent, not flagged against the light --bg it never sits on.
    (tmp_path / "styles.css").write_text(
        ":root { --text-on-accent: #ffffff; --accent: #0f766e; --bg: #f8f7f5; }")
    assert lint_contrast(tmp_path) == []
    # ...and a genuinely bad on-accent pick IS still caught, against --accent.
    (tmp_path / "styles.css").write_text(
        ":root { --text-on-accent: #ffffff; --accent: #f59e0b; --bg: #f8f7f5; }")
    issues = lint_contrast(tmp_path)
    assert issues and issues[0]["bg"].startswith("--accent")
