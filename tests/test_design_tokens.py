from skyn3t.studio.design_tokens import (
    contrast_ratio,
    derive_accent,
    derive_tokens,
    design_md_block,
    lint_contrast,
)


def test_accent_from_brief_not_green_by_default():
    assert derive_accent("GreenText editor") == "#16a34a"
    assert derive_accent("an ocean weather app") == "#2563eb"
    assert derive_accent("COIN REAPER molten forge game") == "#f59e0b"
    # nothing implies a color -> neutral indigo, NOT green
    assert derive_accent("a contractor invoice tool") == "#6366f1"


def test_derived_tokens_pass_AA_contrast():
    t = derive_tokens("a plain app")
    # text on bg and on surface must clear WCAG AA (4.5)
    assert contrast_ratio(t["--text"], t["--bg"]) >= 4.5
    assert contrast_ratio(t["--text"], t["--surface"]) >= 4.5


def test_contrast_ratio_known_values():
    assert contrast_ratio("#000000", "#ffffff") == 21.0
    assert contrast_ratio("#ffffff", "#ffffff") == 1.0


def test_design_md_block_has_tokens_and_prepaint_rule():
    block = design_md_block("GreenText")
    assert "--accent: #16a34a" in block
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
