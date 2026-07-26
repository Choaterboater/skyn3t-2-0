"""Game visual check — advisory vision judge of a running game's screenshot.

Validated separately that gpt-4o-mini flags an empty board and a sparse field; these
tests pin the parse + gap logic deterministically with an injected vision_fn (no
network), and the never-raise / degrade-open contract.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from skyn3t.studio.game_visual_check import (
    GAME_PROMPT,
    _looks_like_a_flash_frame,
    check_game_visual,
    judge_game_frame,
)


def _vf(text: str):
    return lambda image_path, prompt: text


def test_populated_readable_frame_has_no_gap():
    v = judge_game_frame("x.png", vision_fn=_vf(
        '{"populated": true, "entities_readable_size": true, "issues": []}'))
    assert v.ok is True
    assert v.gap() is None


def test_empty_field_produces_an_empty_gap():
    v = judge_game_frame("x.png", vision_fn=_vf(
        '{"populated": false, "entities_readable_size": true, "issues": ["empty play field"]}'))
    assert v.ok is False
    gap = v.gap()
    assert gap and "empty" in gap.lower()


def test_tiny_entities_produce_a_size_gap():
    v = judge_game_frame("x.png", vision_fn=_vf(
        '{"populated": true, "entities_readable_size": false, "issues": ["sprites too small"]}'))
    gap = v.gap()
    assert gap and "tiny" in gap.lower()


def test_fenced_json_is_parsed():
    v = judge_game_frame("x.png", vision_fn=_vf(
        '```json\n{"populated": false, "entities_readable_size": true, "issues": []}\n```'))
    assert v.populated is False
    assert v.gap() is not None  # not populated -> a gap even with empty issues


def test_no_vision_fn_soft_skips_and_degrades_open():
    v = judge_game_frame("x.png", vision_fn=None)
    assert v.skipped is True
    assert v.gap() is None  # a soft-skip NEVER produces a gap (never false-flag)


def test_garbage_vision_output_never_raises_and_skips():
    for junk in ("", "not json at all", "{broken", "null", "[1,2,3]"):
        v = judge_game_frame("x.png", vision_fn=_vf(junk))
        assert v.skipped is True or v.gap() is None  # never a spurious gap from garbage


def test_prompt_asks_observable_questions():
    low = GAME_PROMPT.lower()
    assert "populated" in low and "size" in low and "json" in low


def test_prompt_treats_between_wave_lull_as_populated():
    # Caught live: a screenshot of a real, working tower-defense build judged
    # populated=False/"empty play field" — the game state was "Wave cleared! +30
    # Shine" banner, wave 2/4, one small enemy near the goal, Start Wave button
    # ready for the next wave. That's a normal between-wave LULL in a genuinely
    # working game, not a broken/empty board, but the prompt only described
    # "mid-play" with no notion that a brief pause between waves still counts.
    low = GAME_PROMPT.lower()
    assert "between wave" in low or "wave clear" in low or "tower defense" in low


def test_game_visual_defaults_to_docker_supervisor_and_awaits_stop(
    tmp_path: Path,
    monkeypatch,
):
    import skyn3t.studio.app_runner as app_runner
    import skyn3t.studio.game_visual_check as game_visual_check
    import skyn3t.studio.preview_supervisor as preview_supervisor
    import skyn3t.studio.visual_check as visual_check

    calls: dict[str, object] = {}
    app = type(
        "RunningPreview",
        (),
        {
            "status": "running",
            "url": "http://127.0.0.1:9876",
            "pid": None,
            "log_path": None,
        },
    )()

    class FakeSupervisor:
        async def start(self, project_dir, stack="", **kwargs):
            calls["start"] = (Path(project_dir), stack, kwargs)
            return app

        async def stop(self, stopped_app):
            await asyncio.sleep(0)
            calls["stopped"] = stopped_app

    class HostRunnerMustNotBeConstructed:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("game preview must not execute on the host")

    monkeypatch.setattr(app_runner, "AppRunner", HostRunnerMustNotBeConstructed)
    monkeypatch.setattr(
        preview_supervisor,
        "PreviewSupervisor",
        FakeSupervisor,
    )
    monkeypatch.setattr(visual_check, "playwright_available", lambda: True)
    monkeypatch.setattr(
        visual_check,
        "make_vision_fn",
        lambda _settings: _vf(
            '{"populated": true, "entities_readable_size": true, "issues": []}'
        ),
    )
    monkeypatch.setattr(visual_check, "make_click_vision_fn", lambda _settings: None)
    monkeypatch.setattr(
        game_visual_check,
        "_screenshot_midplay",
        lambda *_args, **_kwargs: True,
    )

    verdict = asyncio.run(check_game_visual(tmp_path, settings=object()))

    assert verdict.ok is True
    assert calls["start"] == (tmp_path, "phaser", {})
    assert calls["stopped"] is app


# ── _looks_like_a_flash_frame (hardening pass, same session) ───────────────────
# Root cause of measured flakiness (the SAME real delivered build scoring
# populated=True then populated=False with zero code change): a judged screenshot
# can land mid an in-game full-screen FX (this game's `cameras.main.flash(200, 255,
# 40, 40)` red damage-tint, fired on the Still taking a hit) — for that ONE frame the
# canvas is a solid color, genuinely showing nothing, even though the underlying game
# state is fine (confirmed live: two screenshots a few seconds apart on the SAME run
# showed identical real progress — wave 2/4, an enemy on the path — except one landed
# on a solid red frame). A vision judge correctly reports "empty" for that exact
# frame; the fix is to not hand it that frame in the first place.

PIL = pytest.importorskip("PIL", reason="Pillow is optional (pyproject [visual] extra)")


def _solid_png(color=(220, 30, 30)) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (64, 64), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _varied_png() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (64, 64), (20, 20, 30))
    px = img.load()
    for x in range(64):
        for y in range(64):
            px[x, y] = ((x * 4) % 256, (y * 4) % 256, (x * y) % 256)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_flash_frame_detects_a_solid_color_screenshot():
    assert _looks_like_a_flash_frame(_solid_png()) is True


def test_flash_frame_does_not_flag_a_normal_varied_screenshot():
    assert _looks_like_a_flash_frame(_varied_png()) is False


def test_flash_frame_never_raises_on_garbage_bytes():
    assert _looks_like_a_flash_frame(b"not a real png at all") is False


def test_flash_frame_never_raises_on_empty_bytes():
    assert _looks_like_a_flash_frame(b"") is False


def test_flash_frame_soft_skips_without_pillow(monkeypatch):
    # If Pillow genuinely isn't installed, this is an optimization, not a
    # correctness requirement -- must degrade to "not flagged", never raise.
    fixture = _solid_png()  # build BEFORE patching -- the helper itself needs PIL
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no module named PIL")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert _looks_like_a_flash_frame(fixture) is False
