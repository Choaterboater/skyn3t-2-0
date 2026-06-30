"""Sprite-model cascade + background-keying (resilience when the preferred pixel
model, retro-diffusion/rd-fast, is down on Replicate).

- The cascade tries the preferred model, then a working official fallback
  (flux-schnell) before degrading to a primitive — a single model outage stops
  being a blackout.
- flux paints a SOLID background; ``_key_bg_to_alpha`` flood-fills the
  border-connected background to transparent (so a plane drops cleanly onto a
  scrolling scene) WITHOUT eating interior light pixels (white stars/cockpit).
"""

from __future__ import annotations

import io

from skyn3t.agents.art_director import direct_art
from skyn3t.config.settings import Settings
from skyn3t.studio.assets import (
    _SPRITE_ROLE_MODELS,
    _key_bg_to_alpha,
    generate_role_sprites,
)


def _png(bg, shapes):
    """A PNG: solid ``bg`` with ``shapes`` = list of (box, color) filled rects."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (32, 32), bg)
    d = ImageDraw.Draw(im)
    for box, color in shapes:
        d.rectangle(box, fill=color)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _alpha_at(png_bytes, x, y):
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    return im.getpixel((x, y))[3]


# ── background keying ────────────────────────────────────────────────────────

def test_key_bg_makes_border_background_transparent():
    # white bg, a dark plane body in the center
    data = _key_bg_to_alpha(_png((255, 255, 255), [((10, 8, 22, 24), (40, 60, 90))]))
    assert _alpha_at(data, 0, 0) == 0      # corner background -> transparent
    assert _alpha_at(data, 16, 16) == 255  # plane body -> opaque


def test_key_bg_preserves_interior_light_pixels():
    # a dark body with a WHITE star inside it — the interior white must survive
    # (flood-fill from the border, not a global white key).
    data = _key_bg_to_alpha(_png((255, 255, 255), [
        ((8, 8, 24, 24), (40, 60, 90)),     # body
        ((14, 14, 18, 18), (255, 255, 255)),  # interior white "star"
    ]))
    assert _alpha_at(data, 0, 0) == 0        # border bg -> transparent
    assert _alpha_at(data, 16, 16) == 255    # interior white star -> still opaque


def test_key_bg_never_raises_on_garbage():
    assert _key_bg_to_alpha(b"not a png") == b"not a png"


# ── cascade ──────────────────────────────────────────────────────────────────

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _ModelStub:
    """Fails (returns []) for the given models, succeeds for the rest; records the
    model order it was asked for."""
    available = True

    def __init__(self, down):
        self.down = set(down)
        self.models_tried: list[str] = []

    async def generate_images(self, prompt, n=1, *, model=None, timeout=None):
        self.models_tried.append(model)
        return [] if model in self.down else [_PNG]


def _settings(**kw):
    base = dict(llm_backend="stub", replicate_api_token="tok", game_art_source="replicate")
    base.update(kw)
    return Settings(**base)


def test_cascade_lists_a_fallback_after_the_preferred_model():
    assert _SPRITE_ROLE_MODELS[0] == "retro-diffusion/rd-fast"
    assert len(_SPRITE_ROLE_MODELS) >= 2
    assert any("flux" in m for m in _SPRITE_ROLE_MODELS[1:])


async def test_falls_back_to_working_model_when_preferred_is_down(tmp_path):
    brief = "a space shooter with aliens"
    client = _ModelStub(down={"retro-diffusion/rd-fast"})  # rd-fast down
    res = await generate_role_sprites(
        str(tmp_path), brief, settings=_settings(), client=client, seed=1
    )
    sprite_roles = set(direct_art(brief).sprite_roles())
    assert sprite_roles
    assert res["generated"] == len(sprite_roles)         # sprites STILL produced
    assert client.models_tried[0] == "retro-diffusion/rd-fast"   # preferred tried first
    assert any("flux" in m for m in client.models_tried)         # cascade engaged


async def test_preferred_model_used_alone_when_it_works(tmp_path):
    client = _ModelStub(down=set())  # everything works
    await generate_role_sprites(
        str(tmp_path), "a space shooter with aliens",
        settings=_settings(), client=client, seed=1
    )
    assert all(m == "retro-diffusion/rd-fast" for m in client.models_tried)  # no wasted fallback


async def test_all_models_down_degrades_to_primitives(tmp_path):
    client = _ModelStub(down={m for m in _SPRITE_ROLE_MODELS})
    res = await generate_role_sprites(
        str(tmp_path), "a space shooter with aliens",
        settings=_settings(), client=client, seed=1
    )
    assert res["generated"] == 0  # no crash; scaffold renders primitives
