# tests/test_worktree_security.py
"""A worktree slug must never escape the worktrees root (path traversal)."""
from __future__ import annotations

import pytest

import skyn3t.worktree as worktree_mod
from skyn3t.worktree import create_worktree, merge_back, source_tree_snapshot


def test_create_worktree_rejects_path_traversal_slug(tmp_path):
    base = tmp_path / "projects"
    base.mkdir()
    # A slug like "../../evil" would otherwise mkdir outside the worktrees root.
    with pytest.raises(ValueError):
        create_worktree(base, "../../../evil")
    # And nothing escaped: no stray dir created up the tree.
    assert not list(tmp_path.parent.glob("evil-*"))


def test_create_worktree_allows_normal_slug(tmp_path):
    base = tmp_path / "projects"
    base.mkdir()
    wt = create_worktree(base, "my-app")
    root = (base.parent / ".skyn3t_worktrees").resolve()
    assert wt.path.resolve().is_relative_to(root)


def test_merge_back_rejects_symlink_to_outside_file(tmp_path):
    src = tmp_path / "wt"
    dst = tmp_path / "project"
    src.mkdir()
    outside = tmp_path / "secret.env"
    outside.write_text("OPENROUTER_API_KEY=sk-or-leak\n", encoding="utf-8")
    (src / "public").mkdir()
    (src / "public" / "leak.txt").symlink_to(outside)

    snapshot = source_tree_snapshot(src)
    copied = merge_back(src, dst)

    assert snapshot["valid"] is False
    assert snapshot["unsafe_aliases"] == ["public/leak.txt"]
    assert "public/leak.txt" not in copied
    assert not (dst / "public" / "leak.txt").exists()


def test_merge_back_clean_unlinks_preseeded_symlink_instead_of_writing_through(
    tmp_path,
):
    # Regression: a poisoned symlink left in the delivered project dir survived
    # the clean pass (rmtree refuses symlinks) and the copy loop then wrote
    # THROUGH it — outside the project boundary.
    src = tmp_path / "wt"
    dst = tmp_path / "project"
    outside = tmp_path / "outside"
    src.mkdir()
    dst.mkdir()
    outside.mkdir()
    (src / "sub").mkdir()
    (src / "sub" / "payload.txt").write_text("build output\n", encoding="utf-8")
    (dst / "sub").symlink_to(outside)

    copied = merge_back(src, dst, clean=True)

    # The alias is gone, nothing escaped, and the payload landed INSIDE dst.
    assert not (dst / "sub").is_symlink()
    assert not (outside / "payload.txt").exists()
    assert (dst / "sub" / "payload.txt").read_text(encoding="utf-8") == "build output\n"
    assert "sub/payload.txt" in copied


def test_merge_back_never_writes_through_symlinked_ancestor(tmp_path):
    # Even without clean=True, a symlinked ancestor already present in the
    # destination must not be traversed by the copy loop.
    src = tmp_path / "wt"
    dst = tmp_path / "project"
    outside = tmp_path / "outside"
    src.mkdir()
    dst.mkdir()
    outside.mkdir()
    (outside / "keep.txt").write_text("untouched\n", encoding="utf-8")
    (src / "linked").mkdir()
    (src / "linked" / "evil.txt").write_text("escape attempt\n", encoding="utf-8")
    (src / "safe.txt").write_text("fine\n", encoding="utf-8")
    (dst / "linked").symlink_to(outside)

    copied = merge_back(src, dst)

    assert "linked/evil.txt" not in copied
    assert "safe.txt" in copied
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "untouched\n"
    assert not (outside / "evil.txt").exists()


def test_merge_back_excludes_case_variants_of_ignored_dirs(tmp_path):
    # On case-insensitive filesystems "Node_Modules" IS "node_modules"; the
    # ignore list must match case-folded like SOURCE_TREE_EXCLUDED_DIR_NAMES.
    src = tmp_path / "wt"
    dst = tmp_path / "project"
    node_modules = src / "Node_Modules" / "pkg"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    pycache = src / "pkg" / "__PYCACHE__"
    pycache.mkdir(parents=True)
    (pycache / "mod.cpython-312.pyc").write_bytes(b"\x00pyc")
    (src / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")

    copied = merge_back(src, dst)

    assert "pkg/mod.py" in copied
    assert "Node_Modules/pkg/index.js" not in copied
    assert "pkg/__PYCACHE__/mod.cpython-312.pyc" not in copied
    assert not (dst / "Node_Modules").exists()
    assert not (dst / "pkg" / "__PYCACHE__").exists()


def test_merge_back_excludes_swift_module_cache(tmp_path):
    src = tmp_path / "wt"
    dst = tmp_path / "project"
    cache = src / ".skyn3t-swift-module-cache" / "JFWOF8FH7S6X"
    cache.mkdir(parents=True)
    (cache / "SwiftShims.pcm").write_text("absolute-path-bound cache\n", encoding="utf-8")
    (src / "Package.swift").write_text("// swift package\n", encoding="utf-8")

    copied = merge_back(src, dst)

    assert "Package.swift" in copied
    assert ".skyn3t-swift-module-cache/JFWOF8FH7S6X/SwiftShims.pcm" not in copied
    assert not (dst / ".skyn3t-swift-module-cache").exists()


def test_merge_back_excludes_swiftpm_build_artifacts(tmp_path):
    src = tmp_path / "wt"
    dst = tmp_path / "project"
    build_cache = src / ".build" / "arm64-apple-macosx" / "debug" / "ModuleCache"
    build_cache.mkdir(parents=True)
    (build_cache / "SwiftShims.pcm").write_text("absolute-path-bound cache\n", encoding="utf-8")
    swiftpm = src / ".swiftpm" / "configuration"
    swiftpm.mkdir(parents=True)
    (swiftpm / "registries.json").write_text("{}", encoding="utf-8")
    (src / "Package.swift").write_text("// swift package\n", encoding="utf-8")

    copied = merge_back(src, dst)

    assert "Package.swift" in copied
    assert ".build/arm64-apple-macosx/debug/ModuleCache/SwiftShims.pcm" not in copied
    assert ".swiftpm/configuration/registries.json" not in copied
    assert not (dst / ".build").exists()
    assert not (dst / ".swiftpm").exists()


def test_source_tree_snapshot_excludes_generated_dirs_at_any_depth(tmp_path):
    source = tmp_path / "src" / "app.js"
    source.parent.mkdir()
    source.write_text("export const app = true;\n", encoding="utf-8")
    manifest = tmp_path / "skyn3t_manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    before = source_tree_snapshot(tmp_path)
    for generated in (
        tmp_path / "dist" / "index.html",
        tmp_path / "packages" / "web" / ".next" / "cache" / "entry",
        tmp_path / "packages" / "web" / "coverage" / "report.json",
        tmp_path / "packages" / "web" / ".astro" / "content.d.ts",
    ):
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("generated", encoding="utf-8")
    after = source_tree_snapshot(tmp_path)

    assert before["valid"] is True
    assert before["sha256"] == after["sha256"]
    assert after["files"] == ["src/app.js"]
    assert after["file_count"] == 1


def test_source_tree_snapshot_excludes_only_runner_proof_output_under_dot_skyn3t(
    tmp_path,
):
    product = tmp_path / ".skyn3t" / "product.json"
    product.parent.mkdir()
    product.write_text('{"schema_version": 1}\n', encoding="utf-8")
    authored = tmp_path / "docs" / "proof-ladder" / "design.md"
    authored.parent.mkdir(parents=True)
    authored.write_text("Authored proof design\n", encoding="utf-8")
    nested_manifest = tmp_path / "public" / "skyn3t_manifest.json"
    nested_manifest.parent.mkdir()
    nested_manifest.write_text('{"authored": true}\n', encoding="utf-8")
    nested_observability = tmp_path / "fixtures" / "skyn3t-observability.json"
    nested_observability.parent.mkdir()
    nested_observability.write_text('{"authored": true}\n', encoding="utf-8")
    (tmp_path / "skyn3t_manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "skyn3t-observability.json").write_text("{}", encoding="utf-8")

    before = source_tree_snapshot(tmp_path)
    for generated in (
        tmp_path / ".skyn3t" / "proof-ladder" / "proof-ladder.json",
        tmp_path / ".skyn3t" / "visual-proof" / "visual-proof.json",
    ):
        generated.parent.mkdir(parents=True)
        generated.write_text('{"passed": true}\n', encoding="utf-8")
    after = source_tree_snapshot(tmp_path)

    assert before["valid"] is True
    assert after["files"] == [
        ".skyn3t/product.json",
        "docs/proof-ladder/design.md",
        "fixtures/skyn3t-observability.json",
        "public/skyn3t_manifest.json",
    ]
    assert before["sha256"] == after["sha256"]


def test_source_tree_snapshot_fails_closed_on_unreadable_directory(
    tmp_path, monkeypatch
):
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    real_walk = worktree_mod.os.walk

    def walk_with_error(root, *, topdown, onerror, followlinks):
        onerror(PermissionError(13, "denied", str(tmp_path / "private")))
        yield from real_walk(
            root,
            topdown=topdown,
            onerror=onerror,
            followlinks=followlinks,
        )

    monkeypatch.setattr(worktree_mod.os, "walk", walk_with_error)

    snapshot = source_tree_snapshot(tmp_path)

    assert snapshot["valid"] is False
    assert snapshot["sha256"] == ""
    assert snapshot["unreadable_files"]
