# tests/test_atomic_io.py
"""atomic_write_text: durable writes via temp+fsync+rename, no truncation on
crash, no leftover temp files. BuildManifest.save must use it."""
from __future__ import annotations

import json

from skyn3t.atomic_io import atomic_write_text
from skyn3t.studio.manifest import BuildManifest


def test_atomic_write_text_writes_and_leaves_no_temp(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_text(p, '{"a": 1}')
    assert p.read_text() == '{"a": 1}'
    atomic_write_text(p, '{"a": 2}')  # overwrite
    assert p.read_text() == '{"a": 2}'
    assert list(tmp_path.glob("*.tmp")) == []  # no leftover temp file


def test_manifest_save_roundtrips_atomically(tmp_path):
    m = BuildManifest(slug="demo", brief="b", stack="python")
    m.save(tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []
    back = BuildManifest.load(tmp_path)
    assert back is not None and back.slug == "demo"
