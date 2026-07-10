# tests/test_atomic_io.py
"""atomic_write_text: durable writes via temp+fsync+rename, no truncation on
crash, no leftover temp files. BuildManifest.save must use it."""
from __future__ import annotations

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.manifest import BuildManifest


def test_atomic_write_text_writes_and_leaves_no_temp(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_text(p, '{"a": 1}')
    assert p.read_text() == '{"a": 1}'
    atomic_write_text(p, '{"a": 2}')  # overwrite
    assert p.read_text() == '{"a": 2}'
    assert list(tmp_path.glob("*.tmp")) == []  # no leftover temp file


def test_manifest_save_roundtrips_utf8_atomically(tmp_path, monkeypatch):
    utf8_brief = "Adult beginner golf \U0001f3cc\ufe0f"
    m = BuildManifest(slug="demo", brief=utf8_brief, stack="python")
    m.save(tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []

    original_read_text = type(tmp_path).read_text

    def require_utf8(path, *args, **kwargs):
        assert kwargs.get("encoding") == "utf-8"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "read_text", require_utf8)
    back = BuildManifest.load(tmp_path)
    assert back is not None
    assert back.slug == "demo"
    assert back.brief == utf8_brief
