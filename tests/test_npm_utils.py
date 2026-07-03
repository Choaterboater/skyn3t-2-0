from __future__ import annotations

import json

from skyn3t.npm_utils import mark_npm_install_current, npm_install_current


def test_npm_install_stamp_invalidates_when_manifest_changes(tmp_path):
    pkg = tmp_path / "package.json"
    (tmp_path / "node_modules").mkdir()
    pkg.write_text(json.dumps({"dependencies": {"vite": "latest"}}), encoding="utf-8")

    mark_npm_install_current(tmp_path)
    assert npm_install_current(tmp_path) is True

    pkg.write_text(json.dumps({"dependencies": {"vite": "latest", "react": "latest"}}), encoding="utf-8")
    assert npm_install_current(tmp_path) is False
