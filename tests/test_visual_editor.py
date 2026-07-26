from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skyn3t.studio.visual_editor import (
    EMPTY_SHA256,
    AmbiguousSourceError,
    ElementSignature,
    PathRejectedError,
    StaleSourceError,
    UnsafeEditError,
    VisualEditor,
)


def _write_component(root: Path) -> Path:
    path = root / "src" / "Hero.tsx"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
export function Hero() {
  return (
    <section id="hero" className="hero featured">
      <h1 id="headline" className="title">Build something useful</h1>
      <img id="hero-image" className="hero-image" src="/images/old.png" alt="Preview" />
    </section>
  )
}
""".lstrip(),
        encoding="utf-8",
    )
    return path


def test_inspect_ranks_strong_source_match_and_returns_edit_metadata(tmp_path: Path) -> None:
    _write_component(tmp_path)
    duplicate = tmp_path / "src" / "Other.tsx"
    duplicate.write_text('<h1 className="title">Different title</h1>\n', encoding="utf-8")
    editor = VisualEditor(tmp_path)

    matches = editor.inspect(
        ElementSignature(
            tag="h1",
            element_id="headline",
            classes=("title",),
            text="Build something useful",
        )
    )

    assert len(matches) == 1
    match = matches[0]
    assert match.relative_path == "src/Hero.tsx"
    assert match.line == 4
    assert match.current_sha
    assert match.occurrence_id
    assert match.signals[:3] == ("tag", "id", "text")
    assert "text" in match.editable
    assert "<h1" in match.excerpt


def test_text_edit_is_source_mapped_sha_guarded_atomic_and_escaped(tmp_path: Path) -> None:
    path = _write_component(tmp_path)
    editor = VisualEditor(tmp_path)
    signature = ElementSignature(tag="h1", element_id="headline", text="Build something useful")
    match = editor.inspect(signature)[0]

    result = editor.edit_text(
        relative_path=match.relative_path,
        base_sha=match.current_sha,
        signature=signature,
        occurrence_id=match.occurrence_id,
        value="Ship <faster> & safer",
    )

    source = path.read_text(encoding="utf-8")
    assert "Ship &lt;faster&gt; &amp; safer" in source
    assert result.changed is True
    assert result.before_sha == match.current_sha
    assert result.after_sha != result.before_sha
    assert result.diff.added_lines == 1
    assert result.diff.removed_lines == 1
    assert "Build something useful" in result.diff.unified_diff
    assert "Ship &lt;faster&gt;" in result.diff.unified_diff
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    ("suffix", "value"),
    [
        (".jsx", "{globalThis.alert(1)}"),
        (".tsx", "${process.env.SECRET}"),
        (".svelte", "{@html attackerControlled}"),
        (".vue", "{{ constructor.constructor('alert(1)')() }}"),
        (".astro", "{dangerouslySetInnerHTML}"),
        (".mdx", "{<InjectedComponent />}"),
    ],
)
def test_text_edit_rejects_framework_template_expressions(
    tmp_path: Path,
    suffix: str,
    value: str,
) -> None:
    path = tmp_path / f"Component{suffix}"
    path.write_text('<h1 id="headline">Safe title</h1>\n', encoding="utf-8")
    editor = VisualEditor(tmp_path)
    signature = ElementSignature(tag="h1", element_id="headline", text="Safe title")
    occurrence = editor.inspect(signature)[0]

    with pytest.raises(UnsafeEditError, match="template"):
        editor.edit_text(
            relative_path=occurrence.relative_path,
            base_sha=occurrence.current_sha,
            signature=signature,
            occurrence_id=occurrence.occurrence_id,
            value=value,
        )

    assert path.read_text(encoding="utf-8") == '<h1 id="headline">Safe title</h1>\n'


def test_plain_html_text_edit_keeps_literal_braces_as_text(tmp_path: Path) -> None:
    path = tmp_path / "index.html"
    path.write_text('<h1 id="headline">Safe title</h1>\n', encoding="utf-8")
    editor = VisualEditor(tmp_path)
    signature = ElementSignature(tag="h1", element_id="headline", text="Safe title")
    occurrence = editor.inspect(signature)[0]

    editor.edit_text(
        relative_path=occurrence.relative_path,
        base_sha=occurrence.current_sha,
        signature=signature,
        occurrence_id=occurrence.occurrence_id,
        value="Literal {braces}",
    )

    assert "Literal {braces}" in path.read_text(encoding="utf-8")


def test_image_edit_only_changes_a_quoted_static_src(tmp_path: Path) -> None:
    path = _write_component(tmp_path)
    editor = VisualEditor(tmp_path)
    signature = ElementSignature(
        tag="img",
        element_id="hero-image",
        image_src="http://localhost:3000/images/old.png",
    )
    match = editor.inspect(signature)[0]

    result = editor.edit_image_src(
        relative_path=match.relative_path,
        base_sha=match.current_sha,
        signature=signature,
        occurrence_id=match.occurrence_id,
        value="https://cdn.example.test/new.webp",
    )

    source = path.read_text(encoding="utf-8")
    assert 'src="https://cdn.example.test/new.webp"' in source
    assert 'alt="Preview"' in source
    assert result.operation == "image_src"


def test_ambiguous_replacement_requires_an_inspected_occurrence_id(tmp_path: Path) -> None:
    path = tmp_path / "index.html"
    path.write_text(
        '<button class="save">Save</button>\n<button class="save">Save</button>\n',
        encoding="utf-8",
    )
    editor = VisualEditor(tmp_path)
    signature = ElementSignature(tag="button", classes=("save",), text="Save")
    base_sha = editor.inspect(signature)[0].current_sha

    with pytest.raises(AmbiguousSourceError) as error:
        editor.edit_text(
            relative_path="index.html",
            base_sha=base_sha,
            signature=signature,
            value="Store",
        )

    assert error.value.details["count"] == 2
    assert path.read_text(encoding="utf-8").count("Save") == 2


def test_stale_source_version_never_overwrites_newer_work(tmp_path: Path) -> None:
    path = _write_component(tmp_path)
    editor = VisualEditor(tmp_path)
    signature = ElementSignature(tag="h1", element_id="headline")
    match = editor.inspect(signature)[0]
    path.write_text(path.read_text(encoding="utf-8") + "// external edit\n", encoding="utf-8")

    with pytest.raises(StaleSourceError) as error:
        editor.edit_text(
            relative_path=match.relative_path,
            base_sha=match.current_sha,
            signature=signature,
            occurrence_id=match.occurrence_id,
            value="Do not overwrite",
        )

    assert error.value.expected_sha == match.current_sha
    assert error.value.current_sha != match.current_sha
    assert "Do not overwrite" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.html",
        "/tmp/outside.html",
        "node_modules/package/index.html",
        "dist/index.html",
        ".skyn3t/product.json",
    ],
)
def test_source_edits_reject_traversal_metadata_vendor_and_build_outputs(
    tmp_path: Path,
    relative_path: str,
) -> None:
    _write_component(tmp_path)
    editor = VisualEditor(tmp_path)

    with pytest.raises(PathRejectedError):
        editor.edit_text(
            relative_path=relative_path,
            base_sha="0" * 64,
            signature=ElementSignature(tag="h1"),
            value="No",
        )


def test_inspection_ignores_and_direct_edits_reject_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.html"
    outside.write_text('<h1 id="outside">Private</h1>', encoding="utf-8")
    link = tmp_path / "linked.html"
    link.symlink_to(outside)
    editor = VisualEditor(tmp_path)

    assert editor.inspect(ElementSignature(element_id="outside")) == []
    with pytest.raises(PathRejectedError, match="symlink"):
        editor.edit_text(
            relative_path="linked.html",
            base_sha="0" * 64,
            signature=ElementSignature(element_id="outside"),
            value="Changed",
        )
    assert "Private" in outside.read_text(encoding="utf-8")


def test_managed_stylesheet_supports_tokens_layout_and_responsive_rules(
    tmp_path: Path,
) -> None:
    editor = VisualEditor(tmp_path)

    token_result = editor.set_design_token(
        base_sha=EMPTY_SHA256,
        token="--accent",
        value="#7c3aed",
    )
    layout_result = editor.set_layout(
        base_sha=token_result.after_sha,
        selector="#hero",
        property_name="padding",
        value="2rem clamp(1rem, 3vw, 3rem)",
    )
    responsive_result = editor.set_layout(
        base_sha=layout_result.after_sha,
        selector="#hero",
        property_name="grid-template-columns",
        value="repeat(2, minmax(0, 1fr))",
        breakpoint="md",
    )

    css = (tmp_path / ".skyn3t" / "visual-editor.css").read_text(encoding="utf-8")
    assert "--accent: #7c3aed;" in css
    assert "padding: 2rem clamp(1rem, 3vw, 3rem);" in css
    assert "@media (min-width: 768px)" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    state = editor.stylesheet_state()
    assert state.current_sha == responsive_result.after_sha
    assert state.tokens == {"--accent": "#7c3aed"}
    assert state.rules["md"]["#hero"]["grid-template-columns"].startswith("repeat")


@pytest.mark.parametrize(
    ("property_name", "value"),
    [
        ("background", "red"),
        ("padding", "1rem; color: red"),
        ("width", "url(javascript:alert(1))"),
        ("display", "table-caption"),
        ("grid-template-areas", '"a a"'),
    ],
)
def test_layout_rejects_raw_or_unsafe_css(
    tmp_path: Path,
    property_name: str,
    value: str,
) -> None:
    editor = VisualEditor(tmp_path)

    with pytest.raises(UnsafeEditError):
        editor.set_layout(
            base_sha=EMPTY_SHA256,
            selector="#hero",
            property_name=property_name,
            value=value,
        )
    assert not (tmp_path / ".skyn3t" / "visual-editor.css").exists()


@pytest.mark.parametrize(
    "selector",
    ["body #hero", "#hero:hover", "#hero, body", "*", "[onclick]"],
)
def test_layout_rejects_broad_or_active_selectors(tmp_path: Path, selector: str) -> None:
    editor = VisualEditor(tmp_path)

    with pytest.raises(UnsafeEditError):
        editor.set_layout(
            base_sha=EMPTY_SHA256,
            selector=selector,
            property_name="display",
            value="grid",
        )


def test_managed_stylesheet_rejects_manual_tampering(tmp_path: Path) -> None:
    editor = VisualEditor(tmp_path)
    created = editor.set_layout(
        base_sha=EMPTY_SHA256,
        selector=".card",
        property_name="gap",
        value="1rem",
    )
    path = tmp_path / ".skyn3t" / "visual-editor.css"
    path.write_text(path.read_text(encoding="utf-8") + "body { display: none; }\n")

    with pytest.raises(UnsafeEditError, match="outside"):
        editor.set_layout(
            base_sha=created.after_sha,
            selector=".card",
            property_name="gap",
            value="2rem",
        )


def test_concurrent_edits_with_same_base_sha_allow_exactly_one_winner(
    tmp_path: Path,
) -> None:
    editor_a = VisualEditor(tmp_path)
    editor_b = VisualEditor(tmp_path)

    def apply(editor: VisualEditor, value: str) -> str:
        try:
            editor.set_design_token(
                base_sha=EMPTY_SHA256,
                token="--accent",
                value=value,
            )
        except StaleSourceError:
            return "stale"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: apply(*pair),
                [(editor_a, "#111111"), (editor_b, "#222222")],
            )
        )

    assert sorted(outcomes) == ["stale", "written"]
    state = editor_a.stylesheet_state()
    assert state.tokens["--accent"] in {"#111111", "#222222"}


def test_dynamic_text_and_javascript_src_are_not_editable(tmp_path: Path) -> None:
    path = tmp_path / "Component.tsx"
    path.write_text(
        '<h1 id="dynamic">{title}</h1>\n<img id="dynamic-image" src={imageUrl} />\n',
        encoding="utf-8",
    )
    editor = VisualEditor(tmp_path)
    heading = editor.inspect(ElementSignature(tag="h1", element_id="dynamic"))[0]

    assert "text" not in heading.editable
    with pytest.raises(UnsafeEditError, match="static text"):
        editor.edit_text(
            relative_path=heading.relative_path,
            base_sha=heading.current_sha,
            signature=ElementSignature(tag="h1", element_id="dynamic"),
            occurrence_id=heading.occurrence_id,
            value="Unsafe structure rewrite",
        )
    image = editor.inspect(ElementSignature(tag="img", element_id="dynamic-image"))[0]
    assert "image_src" not in image.editable
    with pytest.raises(UnsafeEditError, match="static src"):
        editor.edit_image_src(
            relative_path=image.relative_path,
            base_sha=image.current_sha,
            signature=ElementSignature(tag="img", element_id="dynamic-image"),
            occurrence_id=image.occurrence_id,
            value="/new.png",
        )
