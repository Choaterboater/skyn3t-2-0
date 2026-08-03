"""A planned-but-unwritten favicon must not buy an agentic round-trip.

From a real build log:

    code_agent.agentic_retry attempt=1 code_bytes=51181 ok=True
        missing_planned=['public/favicon.svg'] threshold=800

A 51 KB delivery that the provider confirmed complete was declared
under-delivered over one decorative SVG, and both best-of-N trajectories did
the same. Neither resume produced the file — the model plans asset paths far
more reliably than it writes asset bytes — so each round-trip was ~60s of cost
with no chance of success.

Resuming is the right trade for a missing module and the wrong one for
decoration. Only files whose content carries no app behaviour are synthesized;
anything else still counts as missing so the resume still happens.
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent

_synth = CodeAgent._synthesize_trivial_assets


def test_a_planned_favicon_is_synthesized(tmp_path):
    made = _synth(tmp_path, ["public/favicon.svg"])

    assert made == ["public/favicon.svg"]
    body = (tmp_path / "public" / "favicon.svg").read_text(encoding="utf-8")
    assert body.startswith("<svg") and "</svg>" in body


def test_parent_directories_are_created(tmp_path):
    _synth(tmp_path, ["assets/icons/logo.svg"])

    assert (tmp_path / "assets" / "icons" / "logo.svg").is_file()


def test_robots_and_keepfiles_are_synthesized(tmp_path):
    made = _synth(tmp_path, ["robots.txt", ".nojekyll", "src/.gitkeep"])

    assert set(made) == {"robots.txt", ".nojekyll", "src/.gitkeep"}
    assert "User-agent" in (tmp_path / "robots.txt").read_text(encoding="utf-8")
    assert (tmp_path / ".nojekyll").read_text(encoding="utf-8") == ""


def test_source_files_are_never_synthesized(tmp_path):
    """The whole point: a missing MODULE must still trigger the resume."""
    made = _synth(tmp_path, ["src/App.jsx", "src/store.ts", "main.py", "index.html"])

    assert made == []
    assert not (tmp_path / "src" / "App.jsx").exists()


def test_an_existing_file_is_never_overwritten(tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "favicon.svg").write_text("<svg>real</svg>", encoding="utf-8")

    made = _synth(tmp_path, ["public/favicon.svg"])

    assert made == []
    assert (tmp_path / "public" / "favicon.svg").read_text(encoding="utf-8") == "<svg>real</svg>"


def test_path_traversal_is_refused(tmp_path):
    outside = tmp_path.parent / "escaped.svg"
    made = _synth(tmp_path / "wt", ["../escaped.svg"])

    assert made == []
    assert not outside.exists()


def test_a_symlinked_parent_is_refused(tmp_path):
    """Same containment rule _present_planned_files applies."""
    root = tmp_path / "wt"
    (root / "real").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "public").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlink creation not permitted on this host")

    made = _synth(root, ["public/favicon.svg"])

    assert made == []
    assert not (outside / "favicon.svg").exists()


def test_synthesis_is_idempotent(tmp_path):
    first = _synth(tmp_path, ["public/favicon.svg"])
    second = _synth(tmp_path, ["public/favicon.svg"])

    assert first == ["public/favicon.svg"]
    assert second == []


def test_the_placeholder_is_valid_xml(tmp_path):
    """A malformed SVG would trade a resume for a broken asset reference."""
    import xml.etree.ElementTree as ET

    _synth(tmp_path, ["favicon.svg"])

    ET.parse(tmp_path / "favicon.svg")  # raises if malformed


def test_the_placeholder_carries_no_branding(tmp_path):
    _synth(tmp_path, ["favicon.svg"])

    body = (tmp_path / "favicon.svg").read_text(encoding="utf-8").lower()
    for word in ("skyn3t", "placeholder", "todo", "lorem"):
        assert word not in body
