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


# ---- npm install failure classification (offline soft-skip vs real defect) ---
def test_npm_install_offline_classifier():
    """Only genuine connectivity failures soft-skip; dependency defects are
    real failures. This is the gate that decides if an un-installable tree can
    still certify proof.passed=True."""
    from skyn3t.studio.proof_run import _npm_install_is_offline

    # genuine connectivity -> offline (soft-skip allowed)
    for off in ("npm error code ENOTFOUND\ngetaddrinfo ENOTFOUND registry.npmjs.org",
                "npm error network request to https://registry.npmjs.org failed",
                "npm error code ECONNREFUSED", "npm error code ETIMEDOUT",
                "npm error code EAI_AGAIN"):
        assert _npm_install_is_offline(off) is True, off

    # real dependency defects -> NOT offline (must hard-fail)
    for real in ("npm error code ERESOLVE unable to resolve dependency tree",
                 "npm error code E404 Not Found - GET https://registry.npmjs.org/bogus",
                 "npm error code ETARGET No matching version found for @react-three/fiber@8.15.21",
                 "npm error code EINVALIDPACKAGENAME Invalid package name"):
        assert _npm_install_is_offline(real) is False, real


def _node_proj(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x", "dependencies": {"react": "^18.2.0"},
        "scripts": {"build": "next build"},
    }), encoding="utf-8")
    return tmp_path


def test_run_node_build_real_install_failure_hard_fails(tmp_path, monkeypatch):
    """ERESOLVE/E404/ETARGET install failures must return ran=True, ok=False so
    proof.passed flips to False (was soft-skipped as 'offline')."""
    import subprocess as _sp
    import skyn3t.studio.proof_run as pr
    _node_proj(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/npm" if n == "npm" else None)

    class _CP:
        def __init__(self, rc, out): self.returncode = rc; self.stdout = out; self.stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _CP(
        1, "npm error code ETARGET\nnpm error notarget No matching version found for @react-three/fiber@8.15.21"))

    ran, ok, summary = pr._run_node_build(tmp_path, "nextjs", 120)
    assert ran is True and ok is False, (ran, ok, summary)


def test_run_node_build_offline_soft_skips(tmp_path, monkeypatch):
    """A genuine ENOTFOUND connectivity failure stays a soft-skip (ran=False)."""
    import subprocess as _sp
    import skyn3t.studio.proof_run as pr
    _node_proj(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/npm" if n == "npm" else None)

    class _CP:
        def __init__(self, rc, out): self.returncode = rc; self.stdout = out; self.stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _CP(
        1, "npm error code ENOTFOUND\nnpm error network request to https://registry.npmjs.org failed, reason: getaddrinfo ENOTFOUND"))

    ran, ok, summary = pr._run_node_build(tmp_path, "nextjs", 120)
    assert ran is False, (ran, ok, summary)


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


def test_invalid_npm_names_catches_whitespace():
    """npm rejects names with leading/trailing/internal spaces
    (EINVALIDPACKAGENAME). The validator must flag them so a generated
    `" slick-carousel"` never sails through the build verifier's dry check."""
    from skyn3t.studio.proof_run import _invalid_npm_package_names

    pkg = {
        "dependencies": {
            " slick-carousel": "^1.8.1",   # leading space (real codegen bug)
            "react": "^18.2.0",            # valid -> not flagged
            "@react-three/fiber": "8.0.0", # valid scoped -> not flagged
        },
        "devDependencies": {
            "trailing ": "1.0.0",          # trailing space
            "has space": "1.0.0",          # internal space
        },
    }
    flagged = _invalid_npm_package_names(pkg)
    assert " slick-carousel" in flagged
    assert "trailing " in flagged
    assert "has space" in flagged
    assert "react" not in flagged
    assert "@react-three/fiber" not in flagged


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
