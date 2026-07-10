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
