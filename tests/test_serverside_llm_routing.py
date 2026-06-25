"""Keep generated LLM apps' OpenRouter key SERVER-SIDE.

A pure-frontend stack (Vite SPA / static) has no backend to hold OPENROUTER_API_KEY,
so an LLM call there leaks the key into the browser bundle. Three guards:
- planner bumps an LLM brief off a server-less web stack onto nextjs (which co-hosts
  app/api/* via a single `npm run dev`);
- app_runner serves next with the right host flag (next rejects --host);
- integration_verifier advises (never fails) when an LLM key is browser-exposed.
"""
from __future__ import annotations

import json
from pathlib import Path

from skyn3t.agents import integration_verifier as iv
from skyn3t.studio.app_runner import build_run_spec
from skyn3t.studio.planner import detect_stack, file_checklist


# ---- planner: LLM brief must not land on a server-less web stack ----------

def test_llm_web_briefs_bump_to_nextjs():
    assert detect_stack("a react dashboard that summarizes notes with AI") == "nextjs"
    assert detect_stack("a chatbot web app") == "nextjs"
    assert detect_stack("an openrouter landing page") == "nextjs"


def test_non_llm_web_briefs_stay_serverless():
    assert detect_stack("a react dashboard ui") == "react"
    assert detect_stack("a landing page website") == "static"


def test_server_and_mobile_stacks_not_bumped():
    # These already own a server (or aren't web) — bumping would be wrong.
    assert detect_stack("a fastapi chatbot backend") == "fastapi"
    assert detect_stack("an expo mobile ai assistant app") == "react_native"


def test_word_boundary_prevents_false_bumps():
    # "email"/"retail"/"detail" contain "ai" as a substring, NOT a word — the LLM
    # bump must NOT fire on them.
    assert detect_stack("an email inbox web app") == "static"
    assert detect_stack("a retail product website") == "static"
    for brief in ("an email inbox web app", "a retail product website",
                  "a detailed report dashboard ui"):
        assert detect_stack(brief) != "nextjs"


def test_explicit_hint_is_honored_not_bumped():
    # An explicit stack hint is a deliberate override; the bump runs only on
    # keyword-detected stacks (documents the residual hint-bypass).
    assert detect_stack("a chatbot", hint="react") == "react"


def test_nextjs_checklist_unchanged_no_route_file():
    # Guard against the rejected edit: adding app/api/llm/route.js to the checklist
    # would fail delivery for every plain (non-LLM) Next.js build.
    cl = file_checklist("nextjs")
    assert "app/page.jsx" in cl
    assert not any("api/llm" in f for f in cl)


# ---- app_runner: next rejects --host (it uses --hostname) -----------------

def test_next_dev_gets_hostname_flag(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "next dev"}}))
    spec = build_run_spec(tmp_path, "nextjs", port=9200)
    assert "--hostname" in spec.cmd
    assert "--host" not in spec.cmd


def test_vite_dev_keeps_host_flag(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    spec = build_run_spec(tmp_path, "react", port=9201)
    assert "--host" in spec.cmd
    assert "--hostname" not in spec.cmd


# ---- integration_verifier: advisory browser-exposed-key guard -------------

def test_flags_client_prefixed_llm_key(tmp_path: Path):
    (tmp_path / "App.jsx").write_text(
        "const k = import.meta.env.VITE_OPENROUTER_API_KEY;\n")
    found = iv.find_browser_exposed_llm_key(tmp_path)
    assert any("VITE_OPENROUTER_API_KEY" in f for f in found)


def test_flags_direct_provider_endpoint_in_frontend(tmp_path: Path):
    (tmp_path / "App.jsx").write_text(
        "fetch('https://openrouter.ai/api/v1/chat/completions');\n")
    found = iv.find_browser_exposed_llm_key(tmp_path)
    assert any("openrouter.ai/api/" in f for f in found)


def test_does_not_flag_server_side_or_benign(tmp_path: Path):
    # The CORRECT pattern (unprefixed, server-read) and benign names/links.
    (tmp_path / "route.jsx").write_text(
        "const k = process.env.OPENROUTER_API_KEY;\n"
        "const base = import.meta.env.VITE_API_BASE_URL;\n"
        "const sb = import.meta.env.NEXT_PUBLIC_SUPABASE_URL;\n"
        "// docs: https://openrouter.ai/docs and https://platform.openai.com\n")
    assert iv.find_browser_exposed_llm_key(tmp_path) == []


def test_guard_is_advisory_only_never_fails_verdict(tmp_path: Path):
    # A client-exposed key must not flip the verifier's pass/fail verdict.
    import asyncio

    from skyn3t.core.agent import TaskRequest
    (tmp_path / "index.html").write_text("<h1>app</h1>")
    (tmp_path / "App.jsx").write_text(
        "const k = import.meta.env.VITE_OPENAI_API_KEY;\n")
    agent = iv.IntegrationVerifierAgent()
    res = asyncio.run(agent.execute(TaskRequest(type="verify", payload={"project_dir": str(tmp_path)})))
    assert res.success
    # finding present in both channels, verdict computed solely from wiring gaps
    assert any("VITE_OPENAI_API_KEY" in a for a in res.output["config_advice"])
    assert res.output["wiring"]["browser_exposed_llm_key"]
    assert res.output["verdict"] in ("pass", "fail")  # exists, not crashed by the advisory
