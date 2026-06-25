"""Guard against shipping LLM chat-prose as source code.

A real delivered build shipped a `server.js` whose entire contents were a coding
agent's conversational reply ("No problem — I won't block on that. Here's where
things stand: ..."), so `node server.js` threw SyntaxError. Two gaps let it
through: (1) `validate_source`'s brace-balance check accepts prose (balanced
braces), and (2) the agentic path measures delivery by byte count, so a 1.4 KB
prose file clears the threshold and ships. Both are now closed.
"""

from __future__ import annotations

from skyn3t.agents.validate import validate_source

PROSE = (
    "No problem — I won't block on that. Here's where things stand:\n\n"
    "**Delivered:** the rewritten server.js above. It fixes the 404-on-click by "
    "resolving clean URLs and serving a friendly 404 page. Drop it in and run it.\n\n"
    "**Not yet done:** the drawings and more animals. Point me at the app and I'll "
    "do it for real."
)


# ---------------------------------------------------------------------------
# validate_source rejects prose for source files, accepts real code
# ---------------------------------------------------------------------------

def test_validate_rejects_prose_js():
    ok, err = validate_source("server.js", PROSE)
    assert ok is False
    assert "prose" in err.lower()


def test_validate_accepts_real_js():
    code = (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.get('/', (req, res) => res.send('hi'));\n"
        "app.listen(3000);\n"
    )
    assert validate_source("server.js", code) == (True, "")


def test_validate_accepts_jsx_markup():
    assert validate_source("App.jsx", "export default () => <div>Hello world here</div>;")[0] is True


def test_validate_accepts_short_js_snippet():
    # Too short to judge as prose — must not false-reject.
    assert validate_source("x.js", "doThing()")[0] is True


def test_validate_rejects_prose_in_unchecked_language():
    # .go has no syntax checker, but prose must still be caught by the guard.
    ok, err = validate_source("main.go", PROSE)
    assert ok is False and "prose" in err.lower()


def test_validate_still_rejects_unbalanced_js():
    ok, err = validate_source("a.js", "function f() { return 1;")  # missing }
    assert ok is False and "prose" not in err.lower()


# ---------------------------------------------------------------------------
# Agentic path: prose files the CLI wrote are reverted to the scaffold
# ---------------------------------------------------------------------------

def test_clean_agentic_files_reverts_prose_to_scaffold():
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    agent = CodeAgent(event_bus=EventBus(), llm=object())
    disk = {
        "server.js": PROSE,                              # prose the agent wrote
        "index.js": "const x = 1;\nmodule.exports = x;\n",  # real code
        "README.md": "# notes",                          # non-code, left alone
    }
    scaffold = {"server.js": "const http = require('http');\nhttp.createServer().listen(0);\n"}
    clean, rejected = agent._clean_agentic_files(disk, scaffold)

    assert "server.js" in rejected
    assert clean["server.js"] == scaffold["server.js"]   # reverted to runnable baseline
    assert clean["index.js"] == disk["index.js"]         # real code kept
    assert clean["README.md"] == disk["README.md"]       # non-code untouched


def test_clean_agentic_files_drops_prose_without_scaffold():
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    agent = CodeAgent(event_bus=EventBus(), llm=object())
    clean, rejected = agent._clean_agentic_files({"server.js": PROSE}, {})
    assert rejected == ["server.js"]
    assert "server.js" not in clean  # no scaffold baseline → dropped, not shipped
