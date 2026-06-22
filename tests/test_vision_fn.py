# tests/test_vision_fn.py
"""The vision_fn factory that activates the visual loop's judgement step when an
OpenRouter key is present (sends the screenshot as an image block). The HTTP call
itself is integration; the pure request-building parts are tested here."""
from __future__ import annotations

import base64
from types import SimpleNamespace

from skyn3t.studio.visual_check import (
    _image_data_url,
    _vision_messages,
    make_vision_fn,
)


def test_make_vision_fn_is_none_without_a_key():
    s = SimpleNamespace(openrouter_api_key="", vision_model="")
    assert make_vision_fn(s) is None


def test_make_vision_fn_is_callable_with_a_key():
    s = SimpleNamespace(openrouter_api_key="sk-or-test", vision_model="")
    assert callable(make_vision_fn(s))


def test_vision_messages_carry_text_and_image():
    msgs = _vision_messages("data:image/png;base64,AAAA", "judge this layout")
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    by_type = {c["type"]: c for c in content}
    assert "text" in by_type and "image_url" in by_type
    assert by_type["text"]["text"] == "judge this layout"
    assert by_type["image_url"]["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_data_url_round_trips(tmp_path):
    raw = b"\x89PNG\r\n\x1a\nfake-bytes"
    p = tmp_path / "shot.png"
    p.write_bytes(raw)
    url = _image_data_url(str(p))
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == raw
