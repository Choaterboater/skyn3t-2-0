"""The generated hero image must actually reach the page.

From a real delivered Astro site, the foundry produced three assets and codegen
wired exactly two:

    /assets/favicon.png   ->  <link rel="icon">            in BaseLayout
    /assets/og.png        ->  <meta property="og:image">   in BaseLayout
    /assets/hero.png      ->  nowhere

Not a path mismatch — the foundry's paths and the source's agree exactly. The
two that landed are mechanical one-liners in <head>; the hero is the one asset
needing a layout decision in the page body, and that is the one the model
skipped. `test_generated_asset_is_referenced[assets/hero.png]` caught it.

So the directive has to name the element, not just the intent: "render it
visibly" was already there and was not enough.
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent

_directive = CodeAgent._web_asset_directive


def _foundry(**paths) -> dict:
    return {
        "type": "web",
        "selected": {k: {"path": v} for k, v in paths.items()},
    }


def test_the_hero_requirement_names_a_concrete_element():
    """A body-level asset needs an unmissable instruction, not an adjective."""
    out = _directive(_foundry(**{"web/hero": "/assets/hero.png"}))

    assert "/assets/hero.png" in out
    low = out.lower()
    assert "<img" in low or "image component" in low, (
        "the hero instruction must name the element to emit"
    )
    assert "alt" in low, "an undescribed hero image is an accessibility defect"


def test_all_three_assets_are_still_announced():
    out = _directive(_foundry(**{
        "web/hero": "/assets/hero.png",
        "web/og": "/assets/og.png",
        "web/favicon": "/assets/favicon.png",
    }))

    for path in ("/assets/hero.png", "/assets/og.png", "/assets/favicon.png"):
        assert path in out


def test_invented_remote_assets_are_still_forbidden():
    out = _directive(_foundry(**{"web/hero": "/assets/hero.png"}))

    low = out.lower()
    assert "do not invent" in low
    assert "cdn" in low or "stock" in low


def test_a_partial_foundry_only_mentions_what_exists():
    out = _directive(_foundry(**{"web/favicon": "/assets/favicon.png"}))

    assert "/assets/favicon.png" in out
    assert "hero" not in out.lower()


def test_a_non_web_foundry_is_silent():
    assert _directive({"type": "game", "selected": {"x": {"path": "/a.png"}}}) == ""


def test_missing_or_empty_input_is_silent():
    assert _directive(None) == ""
    assert _directive({}) == ""
    assert _directive({"type": "web"}) == ""
    assert _directive({"type": "web", "selected": {}}) == ""


def test_entries_without_a_path_are_ignored():
    out = _directive({"type": "web", "selected": {"web/hero": {"path": ""}}})

    assert out == ""
