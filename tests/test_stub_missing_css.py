"""A near-complete app must not be no_go'd by a single missing imported stylesheet.

When codegen is cut short (e.g. the agentic stage times out), the entry often
imports `./index.css` that was never written — proof flags the unresolved import
and fails the whole build. An empty stub for a LOCAL imported .css resolves it.
"""

from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent


def _repair(files):
    return CodeAgent.__new__(CodeAgent)._repair_entrypoints("react_vite", files, "app")


def test_stubs_missing_local_css_import():
    files = {
        "src/main.tsx": "import './index.css'\nimport App from './App'\n",
        "src/App.tsx": "export default function App(){ return null }\n",
    }
    out = _repair(files)
    assert "src/index.css" in out          # the dangling import now resolves
    assert out["src/index.css"].strip()    # non-empty stub


def test_resolves_parent_relative_css():
    files = {"src/pages/Home.tsx": "import '../styles/home.css'\n"}
    out = _repair(files)
    assert "src/styles/home.css" in out


def test_does_not_stub_package_css_import():
    files = {"src/main.tsx": "import 'normalize.css'\n"}  # package import (no leading .)
    out = _repair(files)
    assert not any("normalize.css" in k for k in out)


def test_does_not_overwrite_existing_css():
    files = {"src/main.tsx": "import './index.css'\n", "src/index.css": "body{color:red}"}
    out = _repair(files)
    assert out["src/index.css"] == "body{color:red}"  # untouched
