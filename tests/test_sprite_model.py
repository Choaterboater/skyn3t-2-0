"""Pixel game-sprite model + its input params (roadmap #6 art quality).

Researched + live-verified: retro-diffusion/rd-fast is an OFFICIAL Replicate model
(works on the bare-name endpoint the adapter uses) that produces pixel-native game
sprites and, with remove_bg=true, a transparent-alpha PNG in one call. The adapter
must send those rd-specific params — but ONLY to retro-diffusion/* models (flux /
recraft reject style/remove_bg).
"""

from __future__ import annotations

from skyn3t.adapters.replicate import ReplicateClient
from skyn3t.config.settings import Settings
from skyn3t.studio.assets import _SPRITE_ROLE_MODEL


def _cli():
    return ReplicateClient(Settings(replicate_api_token="t"))


def test_sprite_model_is_retro_diffusion_rd_fast():
    assert _SPRITE_ROLE_MODEL == "retro-diffusion/rd-fast"


def test_input_for_sends_pixel_game_params_for_retro_diffusion():
    inp = _cli()._input_for("a knight sprite", "retro-diffusion/rd-fast")
    assert inp["prompt"] == "a knight sprite"
    assert inp["style"] == "game_asset"
    assert inp["remove_bg"] is True
    assert inp["width"] == 256 and inp["height"] == 256


def test_input_for_minimal_for_non_retro_models():
    # flux / recraft reject style/remove_bg — those keys must NOT reach them.
    for model in ("black-forest-labs/flux-schnell", "recraft-ai/recraft-v3", ""):
        assert _cli()._input_for("a logo", model) == {"prompt": "a logo"}
