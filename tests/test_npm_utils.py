from __future__ import annotations

import json

from skyn3t.npm_utils import (
    discard_foreign_node_modules,
    foreign_node_modules_reason,
    mark_npm_build_current,
    mark_npm_install_current,
    npm_build_current,
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

    mark_npm_build_current(tmp_path, "build")
    assert npm_build_current(tmp_path, "build") is True

    app.write_text("export default function App(){return <h1>Two</h1>}\n", encoding="utf-8")
    assert npm_build_current(tmp_path, "build") is False


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
