# tests/test_vision_fn.py
"""The vision_fn factory honors the explicitly selected backend before sending a
screenshot as an image block. The HTTP call itself is integration; the pure
request-building parts are tested here."""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from skyn3t.studio.visual_check import (
    _image_data_url,
    _vision_messages,
    make_vision_fn,
)


@pytest.mark.real_cli_vision
def test_make_vision_fn_is_none_without_a_key(monkeypatch):
    # None only when there's NO vision backend at all: no OpenRouter key AND no
    # vision-capable CLI on PATH. (With a CLI present, make_vision_fn returns a
    # CLI judge — see test_vision_backend.py.)
    import skyn3t.studio.visual_check as _vc
    monkeypatch.setattr(_vc.shutil, "which", lambda p: None)
    s = SimpleNamespace(openrouter_api_key="", vision_model="", cli_llm_provider="claude")
    assert make_vision_fn(s) is None


def test_make_vision_fn_is_callable_with_a_key():
    s = SimpleNamespace(
        llm_backend="openrouter", openrouter_api_key="sk-or-test", vision_model=""
    )
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
