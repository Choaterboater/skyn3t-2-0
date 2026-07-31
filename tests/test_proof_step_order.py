"""Proof must build before it runs the project's own tests.

Measured on a delivered Astro site whose test script is literally::

    "test": "npm run build && python -m pytest"

The app declares build-then-test. proof_run did the opposite: the generic
Python probe ran first, so every test that reads build output failed on a
missing dist/ — and the failure looked like a missing feature. On that build
`test_youtube_links.py` asserted >= 18 links and found 0, which reads as "the
brief's YouTube requirement was never implemented". The source had 22 such
references; the built output has 29. Nothing was missing except the build.

Node tests were already correct — they run inside the build branch, after the
build. Only the generic probe was inverted, so this aligns the two.

Ordering also fails better: if the build cannot produce an artifact there is
no point asserting against one, and a build failure is a far more actionable
report than a pile of downstream test failures it caused.
"""

from __future__ import annotations

import json

import pytest

from skyn3t.studio import proof_run as pr


@pytest.fixture
def node_project(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "name": "demo-app",
            "version": "1.0.0",
            "scripts": {"build": "echo built", "test": "npm run build && python -m pytest"},
        }),
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script></body></html>\n',
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text(
        "export default function App() { return <h1>hi</h1>; }\n", encoding="utf-8"
    )
    (src / "main.jsx").write_text(
        "import { createRoot } from 'react-dom/client';\n"
        "import App from './App.jsx';\n"
        "createRoot(document.getElementById('root')).render(<App />);\n",
        encoding="utf-8",
    )
    return tmp_path


def _record_order(monkeypatch) -> list[str]:
    """Stub both steps so only their call ORDER is observed."""
    order: list[str] = []

    # Tolerant of extra kwargs: the real signatures gain optional parameters
    # (e.g. _run_node_build's `findings` out-param) and a double that pins the
    # exact arity turns an additive change into a false failure.
    def fake_tests(pdir, stack, timeout, ctx, **_kw):
        order.append("tests")
        return (True, True, "2 passed")

    def fake_node_build(pdir, stack, timeout, ctx, **_kw):
        order.append("build")
        return (True, True, "built")

    monkeypatch.setattr(pr, "_run_generated_tests", fake_tests)
    monkeypatch.setattr(pr, "_run_node_build", fake_node_build)
    # Keep the node-test step out of the picture; it is already post-build.
    monkeypatch.setattr(pr, "_run_node_tests", lambda *a, **k: (False, False, ""))
    return order


def test_build_runs_before_the_generated_tests(node_project, monkeypatch):
    order = _record_order(monkeypatch)

    pr.proof_run(
        node_project, checklist=[], stack="react",
        run_tests=True, run_build=True, test_timeout=30, build_timeout=60,
    )

    assert "build" in order and "tests" in order, order
    assert order.index("build") < order.index("tests"), (
        f"the app's own script is `npm run build && pytest`; proof ran {order}"
    )


def test_tests_are_skipped_when_the_build_fails(node_project, monkeypatch):
    """No point asserting against an artifact that was never produced."""
    order: list[str] = []

    monkeypatch.setattr(
        pr, "_run_generated_tests",
        lambda *a, **k: (order.append("tests"), (True, True, ""))[1],
    )
    monkeypatch.setattr(
        pr, "_run_node_build",
        lambda *a, **k: (order.append("build"), (True, False, "build blew up"))[1],
    )
    monkeypatch.setattr(pr, "_run_node_tests", lambda *a, **k: (False, False, ""))
    monkeypatch.setattr(pr, "_run_ruff_check", lambda *a, **k: (False, False, ""))

    res = pr.proof_run(
        node_project, checklist=[], stack="react",
        run_tests=True, run_build=True, test_timeout=30, build_timeout=60,
    )

    assert order == ["build"], f"tests must not run after a failed build: {order}"
    assert "<build>" in (res.to_dict().get("missing") or [])


def test_a_test_only_run_still_runs_tests(node_project, monkeypatch):
    """run_build=False must not suppress the test step."""
    order = _record_order(monkeypatch)

    pr.proof_run(
        node_project, checklist=[], stack="react",
        run_tests=True, run_build=False, test_timeout=30, build_timeout=60,
    )

    assert order == ["tests"]


def test_a_build_only_run_still_builds(node_project, monkeypatch):
    order = _record_order(monkeypatch)

    pr.proof_run(
        node_project, checklist=[], stack="react",
        run_tests=False, run_build=True, test_timeout=30, build_timeout=60,
    )

    assert order == ["build"]
