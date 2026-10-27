# tests/test_validate_source.py
from __future__ import annotations

from skyn3t.agents.validate import validate_source


def test_valid_python_passes():
    ok, err = validate_source("m.py", "def f():\n    return 1\n")
    assert ok and err == ""


def test_broken_python_fails_with_message():
    ok, err = validate_source("m.py", "def f(:\n    return 1\n")
    assert not ok and "line" in err.lower()


def test_valid_json_passes():
    ok, _ = validate_source("package.json", '{"a": 1}')
    assert ok


def test_broken_json_fails():
    ok, err = validate_source("package.json", '{"a": 1,}')
    assert not ok and err


def test_unvalidatable_extension_soft_skips():
    # No validator for .md -> treated as valid (never block).
    ok, err = validate_source("README.md", "# anything {[(")
    assert ok and err == ""


def test_valid_toml_passes():
    ok, _ = validate_source("pyproject.toml", '[tool.x]\nname = "a"\n')
    assert ok


def test_broken_toml_fails():
    ok, err = validate_source("pyproject.toml", "[tool.x\nname = 1")
    assert not ok and err


def test_balanced_js_with_block_comment_passes():
    # brace inside a block comment must NOT trip the balance check
    ok, _ = validate_source("app.jsx", "/* note: {issue} */\nfunction f() { return 1 }\n")
    assert ok


def test_unbalanced_js_fails():
    ok, err = validate_source("app.jsx", "function f() { return 1 \n")
    assert not ok and err


# --- native-provider-LLM guard ----------------------------------------------
# SkyN3t routes every LLM call through OpenRouter (the only key the user holds).
# A generated app that imports the native Anthropic SDK / reads ANTHROPIC_API_KEY
# ships go-graded then crashes at run for a key the host never set. validate_source
# must reject it so codegen's retry regenerates the compliant OpenRouter way — but
# must NEVER flag the prescribed `openai`-over-OpenRouter client.

def test_native_anthropic_sdk_import_rejected():
    ok, err = validate_source("client.py", "import anthropic\nc = anthropic.Anthropic()\n")
    assert not ok
    assert "openrouter" in err.lower()


def test_native_anthropic_key_read_rejected():
    src = (
        "import os\n"
        "key = os.getenv('ANTHROPIC_API_KEY')\n"
        "if not key:\n"
        "    raise ValueError('ANTHROPIC_API_KEY environment variable is required')\n"
    )
    ok, err = validate_source("main.py", src)
    assert not ok
    assert "openrouter" in err.lower()


def test_js_native_anthropic_sdk_rejected():
    ok, err = validate_source(
        "llm.ts", "import Anthropic from '@anthropic-ai/sdk'\nconst c = new Anthropic()\n"
    )
    assert not ok and err


def test_compliant_openai_over_openrouter_passes():
    # The PRESCRIBED client: openai SDK pointed at OpenRouter, OPENROUTER_API_KEY.
    src = (
        "import os\n"
        "from openai import OpenAI\n"
        "client = OpenAI(base_url='https://openrouter.ai/api/v1',\n"
        "                api_key=os.getenv('OPENROUTER_API_KEY'))\n"
    )
    ok, err = validate_source("llm.py", src)
    assert ok and err == ""


def test_openai_import_is_never_flagged_as_native():
    # `import openai` IS the OpenRouter client — flagging it would break every
    # compliant LLM app, so it must always pass the native-provider guard.
    ok, _ = validate_source("svc.py", "import openai\n\n\ndef ask(q):\n    return q\n")
    assert ok


# _balanced() false-positives on regex literals and nested template literals;
# when Node is available a parseable .js file must not be rejected. Real-world
# case: StoreInv's public/app.js was refused on every improve as "Unclosed '['".
import shutil

import pytest

_NEEDS_NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_VANILLA_FRONTEND_JS = (
    "const esc = (s) => String(s ?? '').replace(/[&<>\"']/g, (c) => ({ '&': '&amp;' }[c] || c));\n"
    "function card(p) {\n"
    "  return `<p>${p.url ? `<a href=\"${esc(p.url)}\">${esc(p.name)}</a>` : esc(p.name)}</p>`;\n"
    "}\n"
)


@_NEEDS_NODE
def test_js_regex_and_nested_template_literals_pass_when_node_parses():
    ok, err = validate_source("public/app.js", _VANILLA_FRONTEND_JS)
    assert ok, err


@_NEEDS_NODE
def test_js_real_syntax_error_still_fails_with_node_available():
    ok, _ = validate_source("public/app.js", "function f( {\n  return 1;\n")
    assert not ok


def test_jsx_in_js_still_passes_balance_check_without_node_parse():
    # Node cannot parse JSX, so acceptance must still come from _balanced.
    ok, err = validate_source("src/App.js", "export default function App() { return <div>{1}</div>; }\n")
    assert ok, err
