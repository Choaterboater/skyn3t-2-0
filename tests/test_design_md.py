"""DESIGN.md persistence — the design direction a build was generated with is
delivered as an artifact (tokens + the designer stage's direction) so
`skyn3t studio improve` re-reads it instead of drifting palette/fonts/layout.
Write side lives in the runner's delivery step (same stack gate as the design
bar); read side is exercised in tests/test_improve_engine.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.design_tokens import (
    DESIGN_MD_HEADER,
    design_md_block,
    read_design_md,
    render_design_md,
    write_design_md,
)
from skyn3t.studio.runner import StudioRunner

_DESIGN = {
    "theme": "dark minimal",
    "palette": {"bg": "#0e1116", "fg": "#e6edf3", "accent": "#6750f2"},
    "typography": "Inter",
    "layout": "card grid",
    "components": ["filter bar", "metric cards"],
    "states": ["empty", "loading", "error"],
}


def _runner() -> StudioRunner:
    return StudioRunner(EventBus(), Orchestrator(EventBus()), settings=Settings())


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(extra={}, files=[])


def test_design_md_written_for_web_stack_with_token_block(tmp_path):
    manifest = _manifest()
    # The shape the runner stores in prior["design"]: the designer stage's
    # verbatim {"design": {...}, "model"/"backend": ...} payload.
    prior = {"design": {"design": _DESIGN, "model": "m", "backend": "stub"}}

    _runner()._deliver_design_md(str(tmp_path), "a cozy bakery site", "react", prior, manifest)

    text = (tmp_path / "DESIGN.md").read_text(encoding="utf-8")
    assert text.startswith(DESIGN_MD_HEADER)
    # The deterministic token block travels verbatim.
    assert design_md_block("a cozy bakery site") in text
    # Plus the designer stage's direction, unwrapped from the nested payload.
    assert "## Design direction" in text
    assert "accent:#6750f2" in text
    assert manifest.extra["design_md"] == "DESIGN.md"
    assert "DESIGN.md" in manifest.files


def test_design_md_omits_direction_when_designer_produced_none(tmp_path):
    manifest = _manifest()

    _runner()._deliver_design_md(str(tmp_path), "a cozy bakery site", "react", {}, manifest)

    text = (tmp_path / "DESIGN.md").read_text(encoding="utf-8")
    assert "DESIGN TOKENS" in text
    assert "## Design direction" not in text


def test_design_md_not_written_for_phaser_or_cli_stacks(tmp_path):
    runner = _runner()
    for stack in ("phaser", "python", "cli"):
        proj = tmp_path / stack
        proj.mkdir()
        manifest = _manifest()

        runner._deliver_design_md(str(proj), "a thing", stack, {}, manifest)

        assert not (proj / "DESIGN.md").exists()
        assert "design_md" not in manifest.extra


def test_design_md_never_clobbers_codegen_written_file(tmp_path):
    codegen_file = "# Design\nNotes the codegen agent wrote itself.\n"
    (tmp_path / "DESIGN.md").write_text(codegen_file, encoding="utf-8")
    manifest = _manifest()

    _runner()._deliver_design_md(str(tmp_path), "a cozy bakery site", "react", {}, manifest)

    assert (tmp_path / "DESIGN.md").read_text(encoding="utf-8") == codegen_file
    assert "design_md" not in manifest.extra


def test_design_md_refreshes_file_we_wrote_before(tmp_path):
    assert write_design_md(tmp_path, "a cozy bakery site") is True
    stale = (tmp_path / "DESIGN.md").read_text(encoding="utf-8")

    assert write_design_md(tmp_path, "a modern fintech dashboard") is True

    refreshed = (tmp_path / "DESIGN.md").read_text(encoding="utf-8")
    assert refreshed != stale
    assert refreshed == render_design_md("a modern fintech dashboard")


def test_read_design_md_bounded_and_missing(tmp_path):
    assert read_design_md(tmp_path) == ""
    (tmp_path / "DESIGN.md").write_text("x" * 5000, encoding="utf-8")
    assert len(read_design_md(tmp_path)) == 4000
