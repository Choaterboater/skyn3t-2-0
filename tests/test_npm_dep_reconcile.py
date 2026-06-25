"""reconcile_npm_deps — declare imported-but-undeclared npm packages.

Generated apps routinely `import PropTypes from "prop-types"` (or axios,
@testing-library/react, ...) without adding the dep, so `npm install` skips it
and Vite/rollup 500s. This scans the source and appends the missing packages to
package.json.dependencies. Offline/pure; no package.json -> no-op.
"""

from __future__ import annotations

import json
import shutil

import pytest

from skyn3t.studio.proof_run import proof_run, reconcile_npm_deps


def test_adds_only_undeclared_bare_imports(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"react": "^18.2.0"}}), encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text(
        "import React from 'react'\n"
        "import PropTypes from 'prop-types'\n"
        "import axios from 'axios'\n"
        "import { Foo } from './Foo'\n"            # relative -> ignored
        "import './styles.css'\n",                  # relative -> ignored
        encoding="utf-8")
    added = reconcile_npm_deps(tmp_path)
    assert set(added) == {"prop-types", "axios"}    # react already declared
    deps = json.load(open(tmp_path / "package.json"))["dependencies"]
    assert deps["prop-types"] == "^15.8.1"          # known pinned version
    assert deps["axios"] == "^1.6.2"


def test_scoped_subpath_and_builtins(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x","dependencies":{}}', encoding="utf-8")
    (tmp_path / "a.js").write_text(
        "import { render } from '@testing-library/react'\n"  # scoped -> @testing-library/react
        "import { createRoot } from 'react-dom/client'\n"    # subpath -> react-dom
        "import fs from 'node:fs'\n"                          # node: builtin -> skip
        "import path from 'path'\n",                          # builtin -> skip
        encoding="utf-8")
    added = reconcile_npm_deps(tmp_path)
    assert "@testing-library/react" in added
    assert "react-dom" in added
    assert "fs" not in added and "node:fs" not in added and "path" not in added


def test_path_alias_imports_are_not_npm_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"react": "^18.2.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "App.jsx").write_text(
        "import { Widget } from '@/components/Widget'\n"
        "import { useWidgets } from '@/hooks/useWidgets'\n",
        encoding="utf-8",
    )
    assert reconcile_npm_deps(tmp_path) == []
    deps = json.load(open(tmp_path / "package.json"))["dependencies"]
    assert "@/components" not in deps
    assert "@/hooks" not in deps


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_invalid_npm_dependency_names_fail_proof(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "name": "x",
            "scripts": {"build": "node -e \"process.exit(0)\""},
            "dependencies": {"@/components": "latest"},
        }),
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.jsx").write_text("import React from 'react'\nconsole.log(React)\n", encoding="utf-8")

    res = proof_run(tmp_path, stack="react", run_build=False, build_timeout=60)

    assert res.passed is False
    assert res.detail.get("invalid_package_names") == ["@/components"]
    assert "<package.json>" in res.missing


def test_unknown_package_gets_latest(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x","dependencies":{}}', encoding="utf-8")
    (tmp_path / "a.jsx").write_text("import x from 'some-rare-pkg'\n", encoding="utf-8")
    reconcile_npm_deps(tmp_path)
    assert json.load(open(tmp_path / "package.json"))["dependencies"]["some-rare-pkg"] == "latest"


def test_noop_without_package_json(tmp_path):
    (tmp_path / "main.py").write_text("import os\n", encoding="utf-8")
    assert reconcile_npm_deps(tmp_path) == []


def test_idempotent(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x","dependencies":{}}', encoding="utf-8")
    (tmp_path / "a.jsx").write_text("import P from 'prop-types'\n", encoding="utf-8")
    assert reconcile_npm_deps(tmp_path) == ["prop-types"]
    assert reconcile_npm_deps(tmp_path) == []  # already declared the second time
