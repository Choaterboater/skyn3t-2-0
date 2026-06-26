"""The agentic-file gate must drop only CLEAR prose (the agent chatting instead of
writing code), not valid code that trips the cheap brace-balance heuristic. A
coding agent's files are validated for real by `next build` / pytest downstream;
discarding good code here reverts the app to the offline scaffold stub -> no_go."""
from __future__ import annotations

from skyn3t.agents.code_agent import CodeAgent


def _clean(disk, scaffold=None):
    return CodeAgent._clean_agentic_files(disk, scaffold or {})


def test_keeps_valid_jsx_with_regex_and_template_literals():
    # Rich, VALID JSX that the cheap _balanced heuristic can false-flag.
    src = (
        '"use client";\n'
        "import { useState } from 'react';\n"
        "export default function ContactForm() {\n"
        "  const re = /^\\d{3}-\\d{4}$/;\n"
        "  const [v, setV] = useState('');\n"
        "  const cls = `field ${v ? 'ok' : 'err'}`;\n"
        "  return <form className={cls}><input onChange={(e) => setV(e.target.value)} /></form>;\n"
        "}\n"
    )
    clean, rejected = _clean({"components/ContactForm.jsx": src})
    assert rejected == []
    assert clean["components/ContactForm.jsx"] == src


def test_drops_clear_prose_file():
    prose = (
        "Here is the page component you asked for. It renders a hero section and a "
        "contact form with validation, and uses a responsive grid layout for the "
        "services. Let me know if you want any changes to the styling or copy.\n"
    )
    clean, rejected = _clean({"app/page.jsx": prose}, scaffold={"app/page.jsx": "export default function P(){return null}\n"})
    assert "app/page.jsx" in rejected
    # reverted to the runnable scaffold baseline
    assert "export default" in clean["app/page.jsx"]


def test_drops_python_syntax_error():
    clean, rejected = _clean({"main.py": "def f(:\n    return 1\n"})
    assert "main.py" in rejected


def test_keeps_code_that_trips_brace_heuristic():
    # A regex literal like /[{]/ false-trips the cheap brace-balance heuristic.
    # A coding agent's valid file must NOT be reverted to the stub over that —
    # the real `next build` is the authoritative syntax gate.
    src = "export default function P(){ const r = /[{]/; return <div>{r.source}</div> }\n"
    clean, rejected = _clean({"app/page.jsx": src}, scaffold={"app/page.jsx": "export default function P(){return null}\n"})
    assert rejected == []
    assert clean["app/page.jsx"] == src


def test_keeps_non_code_files_untouched():
    clean, rejected = _clean({"README.md": "# My App\n\nA great app.\n"})
    assert rejected == [] and "README.md" in clean
