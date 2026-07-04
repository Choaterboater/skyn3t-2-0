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

from skyn3t.studio.proof_run import (
    proof_run,
    reconcile_next_config_peers,
    reconcile_npm_deps,
    sanitize_package_json_deps,
)


# ---- next.config build-tool peer deps (optimizeCss -> critters) --------------
def test_reconcile_adds_critters_for_optimizecss(tmp_path):
    """experimental.optimizeCss runs `critters` to inline CSS during `next build`;
    absent -> export throws on every page (incl. /404). Declare it as a peer."""
    (tmp_path / "next.config.js").write_text(
        "const nextConfig = { experimental: { optimizeCss: true } }\n"
        "module.exports = nextConfig\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "14.2.3"}}), encoding="utf-8")
    added = reconcile_next_config_peers(tmp_path)
    assert added == ["critters"]
    pkg = json.loads((tmp_path / "package.json").read_text())
    assert pkg["devDependencies"]["critters"]


def test_reconcile_no_critters_without_optimizecss(tmp_path):
    (tmp_path / "next.config.mjs").write_text("export default { reactStrictMode: true }\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "14.2.3"}}), encoding="utf-8")
    assert reconcile_next_config_peers(tmp_path) == []


def test_reconcile_critters_already_declared_noop(tmp_path):
    (tmp_path / "next.config.js").write_text("module.exports={experimental:{optimizeCss:true}}", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"next": "14.2.3"}, "devDependencies": {"critters": "^0.0.23"}}), encoding="utf-8")
    assert reconcile_next_config_peers(tmp_path) == []


def test_reconcile_next_config_peers_no_package_json(tmp_path):
    # non-node / no package.json -> no-op, never raises
    assert reconcile_next_config_peers(tmp_path) == []


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
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""
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
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _CP(
        1, "npm error code ENOTFOUND\nnpm error network request to https://registry.npmjs.org failed, reason: getaddrinfo ENOTFOUND"))

    ran, ok, summary = pr._run_node_build(tmp_path, "nextjs", 120)
    assert ran is False, (ran, ok, summary)


def test_run_node_build_skips_build_when_build_stamp_current(tmp_path, monkeypatch):
    import skyn3t.studio.proof_run as pr
    from skyn3t.npm_utils import mark_npm_build_current, mark_npm_install_current

    _node_proj(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export default function App(){return null}\n", encoding="utf-8")
    mark_npm_install_current(tmp_path)
    mark_npm_build_current(tmp_path, "build")
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/npm" if n == "npm" else None)

    def fail_run(*_args, **_kwargs):
        raise AssertionError("current build stamp should skip npm commands")

    monkeypatch.setattr(pr, "_run_proof_command", fail_run)

    ran, ok, summary = pr._run_node_build(tmp_path, "nextjs", 120)
    assert ran is True and ok is True
    assert "npm run build skipped" in summary


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


def test_virtual_imports_are_not_npm_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"vite-plugin-pwa": "^0.20.5"}}),
        encoding="utf-8",
    )
    (tmp_path / "main.jsx").write_text(
        "import { registerSW } from 'virtual:pwa-register'\n",
        encoding="utf-8",
    )

    assert reconcile_npm_deps(tmp_path) == []
    deps = json.load(open(tmp_path / "package.json"))["dependencies"]
    assert "virtual:pwa-register" not in deps
    assert deps["vite-plugin-pwa"] == "^0.20.5"


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


def test_invalid_npm_names_catches_protocol_specifiers():
    from skyn3t.studio.proof_run import _invalid_npm_package_names

    pkg = {"dependencies": {"virtual:pwa-register": "latest", "react": "^18.2.0"}}
    assert _invalid_npm_package_names(pkg) == ["virtual:pwa-register"]


def test_sanitize_package_json_deps_removes_unfixable_names(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "name": "x",
            "dependencies": {
                "react": "^18.2.0",
                "Never Played": "latest",
                "@/components": "latest",
                " slick-carousel": "^1.8.1",
            },
        }),
        encoding="utf-8",
    )

    changed = sanitize_package_json_deps(tmp_path)

    deps = json.load(open(tmp_path / "package.json"))["dependencies"]
    assert set(changed) == {"Never Played", "@/components", " slick-carousel"}
    assert "Never Played" not in deps
    assert "@/components" not in deps
    assert "slick-carousel" in deps
    assert deps["react"] == "^18.2.0"


def test_common_missing_deps_get_pinned_versions(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x","dependencies":{}}', encoding="utf-8")
    (tmp_path / "App.jsx").write_text(
        "import { DndProvider } from 'react-dnd'\n"
        "import { HTML5Backend } from 'react-dnd-html5-backend'\n"
        "import { QueryClient } from '@tanstack/react-query'\n"
        "import { nanoid } from 'nanoid'\n",
        encoding="utf-8",
    )
    added = reconcile_npm_deps(tmp_path)
    deps = json.load(open(tmp_path / "package.json"))["dependencies"]

    assert set(added) == {
        "@tanstack/react-query",
        "nanoid",
        "react-dnd",
        "react-dnd-html5-backend",
    }
    assert deps["react-dnd"] == "^16.0.1"
    assert deps["react-dnd-html5-backend"] == "^16.0.1"
    assert deps["@tanstack/react-query"] == "^5.51.1"
    assert deps["nanoid"] == "^5.0.4"


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


# ---- advisory JS/TS test execution (finding #5) ----------------------------
def test_run_node_tests_advisory_classification(tmp_path, monkeypatch):
    """_run_node_tests runs only with a real runner + node_modules, and a
    failure returns (ran=True, passed=False) — advisory, never a proof gate."""
    import subprocess as _sp

    import skyn3t.studio.proof_run as pr
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x", "scripts": {"test": "vitest run"},
        "devDependencies": {"vitest": "^1.0.0"},
    }), encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/npm" if n == "npm" else None)

    class _CP:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = "2 failed"
            self.stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _CP(1))
    ran, ok, _ = pr._run_node_tests(tmp_path, 60)
    assert ran is True and ok is False

    # no recognized runner -> not run (advisory skip, never asserts on the app)
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x", "scripts": {"test": "echo hi"}, "dependencies": {"react": "^18"},
    }), encoding="utf-8")
    ran, ok, why = pr._run_node_tests(tmp_path, 60)
    assert ran is False


def test_reconcile_normalizes_latest_for_curated_packages(tmp_path):
    """A declared `"latest"` pin for a curated package is normalized to a
    known-good version (codegen's @hookform/resolvers:'latest' broke next build);
    non-curated 'latest' and concrete pins are left untouched."""
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x",
        "dependencies": {
            "@hookform/resolvers": "latest",   # curated -> normalized
            "yup": "latest",                   # curated -> normalized
            "react-hook-form": "7.51.3",       # concrete -> untouched
            "some-rare-pkg": "latest",         # not curated -> left as latest
        },
    }), encoding="utf-8")
    reconcile_npm_deps(tmp_path)
    deps = json.load(open(tmp_path / "package.json"))["dependencies"]
    assert deps["@hookform/resolvers"] != "latest" and deps["@hookform/resolvers"].startswith("^3")
    assert deps["yup"] != "latest" and deps["yup"].startswith("^1")
    assert deps["react-hook-form"] == "7.51.3"
    assert deps["some-rare-pkg"] == "latest"
