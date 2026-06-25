"""Different app types route to different Replicate models, not line-art for all."""

from __future__ import annotations

import asyncio

from skyn3t.adapters.replicate import (
    asset_model,
    asset_prompt,
    select_asset_style,
)
from skyn3t.studio import assets as assets_mod

# --- style classification ----------------------------------------------------

def test_coloring_app_routes_to_flux_schnell():
    style = select_asset_style("A kids coloring app: gallery of animals to color in")
    assert style == "coloring"
    assert asset_model(style) == "black-forest-labs/flux-schnell"
    assert "coloring-book" in asset_prompt(style, "elephant")


def test_logo_app_routes_to_recraft_svg():
    style = select_asset_style("A startup landing page that needs a logo and brand icons")
    assert style == "logo"
    assert asset_model(style) == "recraft-ai/recraft-v4-svg"


def test_photo_app_routes_to_flux_pro():
    style = select_asset_style("A recipe app with realistic food photos for each dish")
    assert style == "photo"
    assert asset_model(style) == "black-forest-labs/flux-1.1-pro"
    assert "photograph" in asset_prompt(style, "pizza")


def test_sticker_app_routes_to_sticker_maker():
    assert select_asset_style("a sticker pack maker app") == "sticker"
    assert asset_model("sticker") == "fofr/sticker-maker"


def test_generic_image_app_defaults_to_illustration_flux_dev():
    style = select_asset_style("a game with cartoon character art")
    assert style == "illustration"
    assert asset_model(style) == "black-forest-labs/flux-dev"


# --- end-to-end: generate_assets uses the routed model + prompt --------------

class _CaptureClient:
    available = True

    def __init__(self):
        self.calls = []

    async def generate_images(self, prompt, n=1, *, model=None, timeout=None):
        self.calls.append({"prompt": prompt, "model": model})
        return [b"\x89PNG\r\n\x1a\n" + b"x" * 64]  # minimal png-ish bytes


def test_generate_assets_uses_routed_model(tmp_path):
    from skyn3t.config.settings import Settings
    s = Settings(replicate_api_token="r8_x", asset_gen=True)
    cli = _CaptureClient()
    brief = "A recipe app with realistic food photos"
    out = asyncio.run(assets_mod.generate_assets(
        str(tmp_path), brief, settings=s, client=cli, max_assets=2))
    assert out["style"] == "photo"
    assert out["model"] == "black-forest-labs/flux-1.1-pro"
    # every generation call used the routed photo model + a photo prompt
    assert cli.calls and all(c["model"] == "black-forest-labs/flux-1.1-pro" for c in cli.calls)
    assert all("photograph" in c["prompt"] for c in cli.calls)


def test_svg_extension_detected():
    assert assets_mod._ext_for(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") == "svg"
    assert assets_mod._ext_for(b"<?xml version='1.0'?><svg></svg>") == "svg"
    assert assets_mod._ext_for(b"\x89PNG\r\n\x1a\n") == "png"
