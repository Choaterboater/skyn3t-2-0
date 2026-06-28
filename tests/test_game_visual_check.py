"""Game visual check — advisory vision judge of a running game's screenshot.

Validated separately that gpt-4o-mini flags an empty board and a sparse field; these
tests pin the parse + gap logic deterministically with an injected vision_fn (no
network), and the never-raise / degrade-open contract.
"""

from __future__ import annotations

from skyn3t.studio.game_visual_check import GAME_PROMPT, judge_game_frame


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
