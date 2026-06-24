"""Regression tests for bug-hunt batch 2."""

from __future__ import annotations

import hashlib

from skyn3t.cortex.components import _stable_hash
from skyn3t.agents._scaffold import scaffold_for


# --- #1 ReviewWatcher dedupe uses a STABLE (sha256) hash, not builtin hash() --

def test_stable_hash_is_deterministic_sha256():
    # Deterministic across processes (unlike builtin hash, which is salted).
    assert _stable_hash("Traceback ... ImportError") == \
        hashlib.sha256(b"Traceback ... ImportError").hexdigest()[:16]
    assert _stable_hash("x") == _stable_hash("x")


# --- #8 react_vite scaffold escapes the brief so App.jsx compiles -----------

def test_react_vite_brief_is_escaped_not_raw_jsx():
    files = scaffold_for("react_vite", "demo", "Bob's <app> {data} & more")
    app = files["src/App.jsx"]
    # The brief is wrapped as a JS-string JSX expression, not injected raw.
    assert "<h1>{'" in app
    assert "\\'" in app                      # the apostrophe is JS-escaped
    assert "<h1>Bob's <app>" not in app      # NOT injected as raw JSX
    # index.html <title> is HTML-escaped.
    html = files["index.html"]
    assert "&lt;app&gt;" in html
    assert "<title>Bob's <app>" not in html


# --- #2 static_html scaffold escapes the brief into valid HTML --------------

def test_static_html_brief_is_html_escaped():
    files = scaffold_for("static_html", "demo", "Dashboard for <users> & {data}")
    html = files["index.html"]
    assert "&lt;users&gt;" in html
    assert "&amp;" in html
    assert "<h1>Dashboard for <users>" not in html  # no stray tag from the brief
