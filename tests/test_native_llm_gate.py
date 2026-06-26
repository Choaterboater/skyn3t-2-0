# tests/test_native_llm_gate.py
"""Delivery backstop: a generated app that requires a NATIVE provider LLM key
(e.g. `import anthropic` + ANTHROPIC_API_KEY) must never grade 'go' — the user
holds only an OpenRouter key, so such an app crashes at run. The codegen-time
validate_source guard should already have regenerated it the OpenRouter way; this
gate is the last line of defense in the verdict."""
from __future__ import annotations

from skyn3t.studio.runner import StudioRunner


def test_gate_flags_native_anthropic_app(tmp_path):
    (tmp_path / "main.py").write_text(
        "import anthropic\n"
        "import os\n"
        "client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))\n"
    )
    violates, reason = StudioRunner._native_llm_gate(str(tmp_path))
    assert violates
    assert "main.py" in reason


def test_gate_flags_native_key_in_env_template(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / ".env.example").write_text("ANTHROPIC_API_KEY=sk-ant-xxx\n")
    violates, reason = StudioRunner._native_llm_gate(str(tmp_path))
    assert violates
    assert "ANTHROPIC_API_KEY" in reason


def test_gate_passes_compliant_openrouter_app(tmp_path):
    # The prescribed client: openai SDK at OpenRouter base_url, OPENROUTER_API_KEY.
    (tmp_path / "llm.py").write_text(
        "import os\n"
        "from openai import OpenAI\n"
        "client = OpenAI(base_url='https://openrouter.ai/api/v1',\n"
        "                api_key=os.getenv('OPENROUTER_API_KEY'))\n"
    )
    (tmp_path / ".env.example").write_text(
        "OPENROUTER_API_KEY=sk-or-xxx\nOPENROUTER_MODEL=anthropic/claude-3.5-haiku\n"
    )
    violates, reason = StudioRunner._native_llm_gate(str(tmp_path))
    assert not violates and reason is None


def test_gate_passes_non_llm_app(tmp_path):
    (tmp_path / "index.js").write_text("console.log('static site');\n")
    violates, _ = StudioRunner._native_llm_gate(str(tmp_path))
    assert not violates


def test_gate_ignores_node_modules(tmp_path):
    # A native SDK living in a dependency tree must not fail the app.
    nm = tmp_path / "node_modules" / "@anthropic-ai" / "sdk"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = class Anthropic {}\n")
    (tmp_path / "app.js").write_text("console.log('ok');\n")
    violates, _ = StudioRunner._native_llm_gate(str(tmp_path))
    assert not violates


def test_gate_never_raises_on_missing_dir(tmp_path):
    violates, reason = StudioRunner._native_llm_gate(str(tmp_path / "nope"))
    assert not violates and reason is None
