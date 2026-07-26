from __future__ import annotations

import pytest

from skyn3t.studio.visual_editor import EMPTY_SHA256, PathRejectedError, VisualEditor
from skyn3t.studio.visual_editor_integration import sync_visual_editor_assets


def test_static_visual_editor_assets_are_loaded_and_idempotent(tmp_path):
    index = tmp_path / "index.html"
    index.write_text("<html><head><title>App</title></head><body><h1>Hi</h1></body></html>")
    VisualEditor(tmp_path).set_design_token(
        base_sha=EMPTY_SHA256,
        token="--accent",
        value="#ff5500",
    )

    first = sync_visual_editor_assets(tmp_path)
    second = sync_visual_editor_assets(tmp_path)

    html = index.read_text(encoding="utf-8")
    assert first.integrated is True
    assert second.integrated is True
    assert html.count('data-skyn3t-visual-editor="style"') == 1
    assert html.count('data-skyn3t-visual-editor="bridge"') == 1
    assert (tmp_path / "skyn3t-visual-editor.css").read_text() == (
        tmp_path / ".skyn3t" / "visual-editor.css"
    ).read_text()
    bridge = (tmp_path / "skyn3t-visual-editor.js").read_text()
    assert "skyn3t-element-selected" in bridge
    assert "skyn3t_editor" in bridge


def test_vite_visual_editor_assets_are_mirrored_to_public(tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "index.html").write_text("<html><head></head><body></body></html>")
    VisualEditor(tmp_path).set_layout(
        base_sha=EMPTY_SHA256,
        selector=".hero",
        property_name="gap",
        value="1rem",
    )

    result = sync_visual_editor_assets(tmp_path)

    assert result.integrated is True
    assert (tmp_path / "public" / "skyn3t-visual-editor.css").is_file()
    assert (tmp_path / "public" / "skyn3t-visual-editor.js").is_file()


def test_next_layout_imports_managed_css_and_embeds_bridge(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    layout = app / "layout.tsx"
    layout.write_text(
        "export default function Layout({ children }) {\n"
        "  return <html><body>{children}</body></html>;\n"
        "}\n"
    )
    VisualEditor(tmp_path).set_design_token(
        base_sha=EMPTY_SHA256,
        token="--surface",
        value="#101010",
    )

    result = sync_visual_editor_assets(tmp_path)

    source = layout.read_text(encoding="utf-8")
    assert result.integrated is True
    assert source.startswith('import "./skyn3t-visual-editor.css";')
    assert 'data-skyn3t-visual-editor="bridge"' in source
    assert (app / "skyn3t-visual-editor.css").is_file()
    assert (tmp_path / "public" / "skyn3t-visual-editor.js").is_file()


def test_integration_reports_unsupported_shell_without_claiming_success(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "component.jsx").write_text(
        "export const Card = () => <div className=\"card\">Card</div>;"
    )
    VisualEditor(tmp_path).set_design_token(
        base_sha=EMPTY_SHA256,
        token="--surface",
        value="#101010",
    )

    result = sync_visual_editor_assets(tmp_path)

    assert result.integrated is False
    assert "supported HTML or framework shell" in result.reason


def test_integration_rejects_symlinked_managed_stylesheet_ancestor(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-metadata"
    outside.mkdir()
    (outside / "visual-editor.css").write_text("outside", encoding="utf-8")
    (tmp_path / ".skyn3t").symlink_to(outside, target_is_directory=True)
    (tmp_path / "index.html").write_text(
        "<html><head></head><body></body></html>",
        encoding="utf-8",
    )

    with pytest.raises(PathRejectedError, match="symlink"):
        sync_visual_editor_assets(tmp_path)

    assert (outside / "visual-editor.css").read_text(encoding="utf-8") == "outside"


def test_integration_rejects_symlinked_shell_file_without_reading_or_writing_it(
    tmp_path,
):
    outside = tmp_path.parent / f"{tmp_path.name}-index.html"
    original = "<html><head></head><body>outside</body></html>"
    outside.write_text(original, encoding="utf-8")
    (tmp_path / "index.html").symlink_to(outside)
    VisualEditor(tmp_path).set_design_token(
        base_sha=EMPTY_SHA256,
        token="--accent",
        value="#ff5500",
    )

    with pytest.raises(PathRejectedError, match="symlink"):
        sync_visual_editor_assets(tmp_path)

    assert outside.read_text(encoding="utf-8") == original


def test_integration_rejects_symlinked_public_destination_ancestor(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-public"
    outside.mkdir()
    (tmp_path / "public").symlink_to(outside, target_is_directory=True)
    (tmp_path / "index.html").write_text(
        "<html><head></head><body></body></html>",
        encoding="utf-8",
    )
    VisualEditor(tmp_path).set_design_token(
        base_sha=EMPTY_SHA256,
        token="--accent",
        value="#ff5500",
    )

    with pytest.raises(PathRejectedError, match="symlink"):
        sync_visual_editor_assets(tmp_path)

    assert list(outside.iterdir()) == []
