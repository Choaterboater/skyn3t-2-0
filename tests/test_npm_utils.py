from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from skyn3t.npm_utils import (
    discard_foreign_node_modules,
    foreign_node_modules_reason,
    mark_npm_build_current,
    mark_npm_install_current,
    npm_build_current,
    npm_build_fingerprint,
    npm_build_stamp_path,
    npm_install_current,
)


def test_npm_install_stamp_invalidates_when_manifest_changes(tmp_path):
    pkg = tmp_path / "package.json"
    (tmp_path / "node_modules").mkdir()
    pkg.write_text(json.dumps({"dependencies": {"vite": "latest"}}), encoding="utf-8")

    mark_npm_install_current(tmp_path)
    assert npm_install_current(tmp_path) is True

    pkg.write_text(json.dumps({"dependencies": {"vite": "latest", "react": "latest"}}), encoding="utf-8")
    assert npm_install_current(tmp_path) is False


def test_npm_build_stamp_invalidates_when_source_changes(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"vite": "latest"}}),
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    app = src / "App.jsx"
    app.write_text("export default function App(){return <h1>One</h1>}\n", encoding="utf-8")

    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>One</h1>", encoding="utf-8")
    mark_npm_build_current(tmp_path, "build")
    assert npm_build_current(tmp_path, "build") is True

    app.write_text("export default function App(){return <h1>Two</h1>}\n", encoding="utf-8")
    assert npm_build_current(tmp_path, "build") is False


def _production_project(root, script="vite build"):
    (root / "node_modules").mkdir()
    (root / "package.json").write_text(json.dumps({"scripts": {"build": script}}))
    (root / "dist").mkdir()
    (root / "dist" / "index.html").write_text("<h1>Production</h1>")
    (root / "dist" / "app.js").write_bytes(b"console.log('production');")
    return root


def test_proof_metadata_does_not_invalidate_a_valid_build(tmp_path):
    _production_project(tmp_path)
    mark_npm_build_current(tmp_path, "build")
    for name in (
        "skyn3t_manifest.json", "skyn3t-observability.json",
        ".skyn3t/proof-ladder/result.json", ".skyn3t/visual-proof/result.json",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"checked": true}')
    assert npm_build_current(tmp_path, "build")

    contract = tmp_path / ".skyn3t" / "visual-design-contract.json"
    contract.write_text('{"theme": "dark"}')
    assert not npm_build_current(tmp_path, "build")
    mark_npm_build_current(tmp_path, "build")
    authored = tmp_path / "src" / "skyn3t_manifest.json"
    authored.parent.mkdir()
    authored.write_text('{"authored": true}')
    assert not npm_build_current(tmp_path, "build")


def test_allowed_build_environment_is_hashed_without_storing_values(tmp_path, monkeypatch):
    _production_project(tmp_path)
    monkeypatch.setenv("VITE_LABEL", "first-public-label")
    mark_npm_build_current(tmp_path, "build")
    assert npm_build_current(tmp_path, "build")
    receipt = npm_build_stamp_path(tmp_path).read_text()
    assert "first-public-label" not in receipt
    monkeypatch.setenv("VITE_LABEL", "second-public-label")
    assert not npm_build_current(tmp_path, "build")


@pytest.mark.parametrize("name,first,second", [
    ("public/logo.svg", b"<svg/>", b"<svg><path/></svg>"),
    ("public/photo.png", b"\x89PNG\r\n\x1a\n\x00\xff", b"\x89PNG\r\n\x1a\n\x01\xff"),
    ("src/font.woff2", b"wOF2\x00", b"wOF2\x01"),
    ("vite.config.ts", b"export default {}", b"export default {base: '/app/'}"),
    ("pnpm-lock.yaml", b"lockfileVersion: 9", b"lockfileVersion: 10"),
    (".env.production", b"PUBLIC_LABEL=one", b"PUBLIC_LABEL=two"),
    (".nvmrc", b"20", b"22"),
    ("patches/library.patch", b"first patch", b"second patch"),
])
def test_build_receipt_binds_all_local_inputs(tmp_path, name, first, second):
    _production_project(tmp_path)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(first)
    mark_npm_build_current(tmp_path, "build")
    assert npm_build_current(tmp_path, "build")
    path.write_bytes(second)
    assert not npm_build_current(tmp_path, "build")
    path.write_bytes(first)
    assert npm_build_current(tmp_path, "build")
    path.unlink()
    assert not npm_build_current(tmp_path, "build")


@pytest.mark.parametrize("mutation", ["missing", "tampered", "added", "renamed", "empty"])
def test_build_receipt_requires_unchanged_output(tmp_path, mutation):
    _production_project(tmp_path)
    mark_npm_build_current(tmp_path, "build")
    assert npm_build_current(tmp_path, "build")
    output = tmp_path / "dist"
    if mutation == "missing":
        (output / "index.html").unlink()
    elif mutation == "tampered":
        (output / "app.js").write_bytes(b"tampered")
    elif mutation == "added":
        (output / "extra.js").write_bytes(b"extra")
    elif mutation == "renamed":
        output.rename(tmp_path / "other-output")
    else:
        (output / "index.html").write_bytes(b"")
    assert not npm_build_current(tmp_path, "build")


def test_build_receipt_rejects_legacy_and_absent_output(tmp_path):
    _production_project(tmp_path)
    npm_build_stamp_path(tmp_path).write_text(json.dumps({
        "version": 1, "build_cmd": "build",
        "fingerprint": npm_build_fingerprint(tmp_path, "build"),
    }))
    assert not npm_build_current(tmp_path, "build")
    (tmp_path / "dist" / "index.html").unlink()
    mark_npm_build_current(tmp_path, "build")
    assert not npm_build_stamp_path(tmp_path).exists()
    assert not npm_build_current(tmp_path, "build")


@pytest.mark.parametrize("script", [
    "tsc --noEmit", "astro check", "node custom-build.js", "vite build --outDir elsewhere",
    "next build --experimental-build-mode compile",
])
def test_unknown_or_check_only_builds_are_not_cached(tmp_path, script):
    _production_project(tmp_path, script)
    mark_npm_build_current(tmp_path, "build")
    assert not npm_build_current(tmp_path, "build")
    assert not npm_build_stamp_path(tmp_path).exists()


@pytest.mark.parametrize("target", ["src/input.bin", "dist/app.js"])
def test_unreadable_build_data_is_a_miss(tmp_path, monkeypatch, target):
    _production_project(tmp_path)
    path = tmp_path / target
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"data")
    mark_npm_build_current(tmp_path, "build")
    assert npm_build_current(tmp_path, "build")
    original = Path.open

    def deny(self, *args, **kwargs):
        if self == path:
            raise PermissionError("unreadable fixture")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    assert not npm_build_current(tmp_path, "build")
    mark_npm_build_current(tmp_path, "build")
    assert not npm_build_current(tmp_path, "build")
    assert not npm_build_stamp_path(tmp_path).exists()
    if target.startswith("src/"):
        assert npm_build_fingerprint(tmp_path, "build") == ""


@pytest.mark.parametrize("target", ["public", "dist", "dist/app.js"])
def test_build_receipt_rejects_symlinks(tmp_path, target):
    root = tmp_path / "project"
    root.mkdir()
    _production_project(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.js").write_bytes(b"outside")
    path = root / target
    if path.is_dir():
        path.rename(root / "old-output")
    elif path.exists():
        path.unlink()
    try:
        path.symlink_to(outside / "app.js" if target.endswith(".js") else outside,
                        target_is_directory=not target.endswith(".js"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    mark_npm_build_current(root, "build")
    assert not npm_build_current(root, "build")
    assert not npm_build_stamp_path(root).exists()


def test_build_hash_traverses_sorted_directories(tmp_path, monkeypatch):
    _production_project(tmp_path)
    for name in ("z/inner", "a/inner", "m/inner"):
        directory = tmp_path / name
        directory.mkdir(parents=True)
        (directory / "asset.bin").write_bytes(name.encode())
    expected = npm_build_fingerprint(tmp_path, "build")
    assert expected
    walk = os.walk
    visited = []

    def reversed_walk(*args, **kwargs):
        for root, dirs, files in walk(*args, **kwargs):
            visited.append(Path(root).relative_to(tmp_path).as_posix())
            dirs.reverse()
            yield root, dirs, list(reversed(files))
            assert dirs == sorted(dirs), "caller must sort before walk descends"

    monkeypatch.setattr(os, "walk", reversed_walk)
    assert npm_build_fingerprint(tmp_path, "build") == expected
    assert visited == [".", "a", "a/inner", "m", "m/inner", "z", "z/inner"]


def test_build_receipt_binds_installed_tool_version(tmp_path):
    _production_project(tmp_path)
    tool = tmp_path / "node_modules" / "vite"
    tool.mkdir()
    package = tool / "package.json"
    package.write_text('{"version":"6.0.0"}')
    mark_npm_build_current(tmp_path, "build")
    assert npm_build_current(tmp_path, "build")
    package.write_text('{"version":"6.0.1"}')
    assert not npm_build_current(tmp_path, "build")


def test_discard_foreign_node_modules_removes_docker_install(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"vite": "latest"}}))
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / ".skyn3t-docker-install.json").write_text(
        json.dumps({"backend": "docker", "container_os": "linux", "fingerprint": "abc"}),
        encoding="utf-8",
    )
    native = nm / "@esbuild" / "linux-arm64"
    native.mkdir(parents=True)
    (native / "package.json").write_text("{}", encoding="utf-8")

    assert foreign_node_modules_reason(tmp_path) == "docker:linux"

    assert discard_foreign_node_modules(tmp_path) == "docker:linux"
    assert not nm.exists()
