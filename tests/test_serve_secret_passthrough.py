"""Tests for narrow serve-time secret passthrough.

A served preview scrubs host secrets (filter_env) so a generated app can't steal
credentials. But an app that legitimately needs a key (e.g. a Claude app doing
`AsyncAnthropic()`) then crashes on boot. Narrow passthrough resolves ONLY the
keys the app declared it needs — detected from explicit env refs AND from SDK
dependencies that read a key implicitly — from the host env or the SkyN3t secrets
store, while every other host secret stays scrubbed.
"""
from __future__ import annotations

import json
from pathlib import Path

import sys

from skyn3t.config.settings import Settings
from skyn3t.security.secrets import SecretsStore, is_secret_name
from skyn3t.studio import app_runner as ar
from skyn3t.studio.app_runner import (
    AppRunner,
    RunSpec,
    build_run_spec,
    cleanup_serve,
    free_port,
    needed_secret_names,
    resolve_serve_secrets,
)


def _node_pkg(tmp_path: Path, deps: dict[str, str] | None = None) -> None:
    pkg = {"name": "app", "scripts": {"dev": "vite"}}
    if deps:
        pkg["dependencies"] = deps
    (tmp_path / "package.json").write_text(json.dumps(pkg))


# ---- needed-key detection ------------------------------------------------

def test_needed_keys_from_explicit_env_ref(tmp_path: Path):
    _node_pkg(tmp_path)
    (tmp_path / "main.js").write_text(
        "const k = process.env.ANTHROPIC_API_KEY;\n")
    assert "ANTHROPIC_API_KEY" in needed_secret_names(tmp_path, "react")


def test_needed_keys_from_sdk_dependency_implicit_read(tmp_path: Path):
    # `new Anthropic()` reads ANTHROPIC_API_KEY internally — the app code never
    # names the var, so only the dependency reveals the need.
    _node_pkg(tmp_path, deps={"@anthropic-ai/sdk": "^0.30.0"})
    (tmp_path / "main.js").write_text(
        "import Anthropic from '@anthropic-ai/sdk';\nconst c = new Anthropic();\n")
    assert "ANTHROPIC_API_KEY" in needed_secret_names(tmp_path, "react")


def test_needed_keys_from_python_requirements(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("fastapi\nanthropic>=0.30\nuvicorn\n")
    (tmp_path / "app.py").write_text("from anthropic import AsyncAnthropic\n")
    assert "ANTHROPIC_API_KEY" in needed_secret_names(tmp_path, "fastapi")


def test_no_needs_for_plain_app(tmp_path: Path):
    _node_pkg(tmp_path)
    (tmp_path / "main.js").write_text("console.log('hi');\n")
    assert needed_secret_names(tmp_path, "react") == set()


def test_needed_keys_from_pyproject_pep621(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "svc"\ndependencies = ["fastapi", "anthropic>=0.30", "uvicorn"]\n')
    (tmp_path / "app.py").write_text("from anthropic import AsyncAnthropic\n")
    assert "ANTHROPIC_API_KEY" in needed_secret_names(tmp_path, "fastapi")


# ---- leak guard: look-alike deps / prose must NOT declare a need -----------

def test_lookalike_dependency_names_do_not_false_positive(tmp_path: Path):
    # The exact repros the adversarial review found against substring matching.
    _node_pkg(tmp_path, deps={"openai-tokenizer": "^1.0.0", "my-cohere-helper": "^1.0.0"})
    assert needed_secret_names(tmp_path, "react") == set()

    other = tmp_path / "py"
    other.mkdir()
    (other / "requirements.txt").write_text("# uses the openai-compatible api\ncoherence-checker==1.0\n")
    assert needed_secret_names(other, "fastapi") == set()


def test_sdk_token_in_metadata_fields_does_not_false_positive(tmp_path: Path):
    # provider names in name/description/scripts/repository must not match.
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "openai-ui-clone",
        "description": "an anthropic-style chat built with groq vibes",
        "repository": "github.com/openai/x",
        "scripts": {"dev": "vite", "replicate-dist": "cp -r dist out"},
        "dependencies": {"react": "^18.0.0"},
    }))
    assert needed_secret_names(tmp_path, "react") == set()


_OPENROUTER_JS = (
    "import OpenAI from 'openai';\n"
    "const c = new OpenAI({ baseURL: 'https://openrouter.ai/api/v1', "
    "apiKey: process.env.OPENROUTER_API_KEY });\n"
)


def test_openrouter_routed_app_does_not_also_need_native_openai_key(tmp_path: Path):
    # A generated app following the LLM directive: openai SDK pointed at OpenRouter,
    # reading OPENROUTER_API_KEY. It must NOT be told it also needs OPENAI_API_KEY.
    _node_pkg(tmp_path, deps={"openai": "^4.0.0"})
    (tmp_path / "main.js").write_text(_OPENROUTER_JS)
    needed = needed_secret_names(tmp_path, "react")
    assert "OPENROUTER_API_KEY" in needed
    assert "OPENAI_API_KEY" not in needed


def test_serve_passes_openrouter_key_to_routed_app_no_setup(tmp_path: Path, monkeypatch):
    # The loop closes: a generated OpenRouter app previews using the user's
    # configured OpenRouter key, with nothing reported missing.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _node_pkg(tmp_path, deps={"openai": "^4.0.0"})
    (tmp_path / "main.js").write_text(_OPENROUTER_JS)
    spec = build_run_spec(tmp_path, "react", port=9140)
    assert spec.env.get("OPENROUTER_API_KEY") == "sk-or-real"
    assert "OPENAI_API_KEY" not in spec.env
    assert spec.missing_secrets == ()


def test_serve_does_not_leak_unrelated_secret_from_lookalike_dep(tmp_path: Path, monkeypatch):
    # Host has a real OPENAI key; the app merely has a look-alike dep name. The
    # key must NOT be passed through (this is the high-severity leak, closed).
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-host-openai")
    _node_pkg(tmp_path, deps={"openai-tokenizer": "^1.0.0"})
    (tmp_path / "main.js").write_text("import { encode } from 'openai-tokenizer';\n")
    spec = build_run_spec(tmp_path, "react", port=9130)
    assert "OPENAI_API_KEY" not in spec.env
    assert spec.injected == ()


# ---- value resolution ----------------------------------------------------

def test_resolve_from_host_env(tmp_path: Path):
    resolved, missing = resolve_serve_secrets(
        {"ANTHROPIC_API_KEY"}, env={"ANTHROPIC_API_KEY": "sk-ant-host"}, store=SecretsStore())
    assert resolved == {"ANTHROPIC_API_KEY": "sk-ant-host"}
    assert missing == []


def test_resolve_from_secrets_store_when_absent_from_env(tmp_path: Path):
    # The SkyN3t-configured key (stored under ANTHROPIC_API_KEY, sourced from
    # SKYN3T_ANTHROPIC_API_KEY) bridges to the app's plain ANTHROPIC_API_KEY.
    store = SecretsStore()
    store.put("ANTHROPIC_API_KEY", "sk-ant-from-skyn3t")
    resolved, missing = resolve_serve_secrets({"ANTHROPIC_API_KEY"}, env={}, store=store)
    assert resolved == {"ANTHROPIC_API_KEY": "sk-ant-from-skyn3t"}
    assert missing == []


def test_resolve_reports_missing_when_nowhere(tmp_path: Path):
    resolved, missing = resolve_serve_secrets(
        {"ANTHROPIC_API_KEY"}, env={}, store=SecretsStore())
    assert resolved == {}
    assert missing == ["ANTHROPIC_API_KEY"]


def test_resolve_ignores_non_secret_names(tmp_path: Path):
    # PORT / NODE_ENV aren't scrubbed by filter_env, so they're not "missing".
    assert not is_secret_name("PORT")
    resolved, missing = resolve_serve_secrets(
        {"PORT", "NODE_ENV"}, env={}, store=SecretsStore())
    assert resolved == {} and missing == []


def test_resolve_never_raises_on_none_or_empty_names(tmp_path: Path):
    # resolve_serve_secrets is a public never-raises helper.
    resolved, missing = resolve_serve_secrets(
        {None, "", "ANTHROPIC_API_KEY"}, env={}, store=SecretsStore())
    assert resolved == {}
    assert missing == ["ANTHROPIC_API_KEY"]


def test_store_seeds_replicate_and_github(monkeypatch):
    # The store->app bridge must cover every key the serve path can declare.
    monkeypatch.setenv("SKYN3T_REPLICATE_API_TOKEN", "r-tok")
    monkeypatch.setenv("SKYN3T_GITHUB_TOKEN", "ghp_tok")
    store = SecretsStore(settings=Settings())
    assert store.get("REPLICATE_API_TOKEN") == "r-tok"
    assert store.get("GITHUB_TOKEN") == "ghp_tok"


# ---- end-to-end through build_run_spec -----------------------------------

def test_serve_passes_needed_key_and_scrubs_the_rest(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-unrelated")  # not referenced
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _node_pkg(tmp_path, deps={"@anthropic-ai/sdk": "^0.30.0"})
    (tmp_path / "main.js").write_text("import Anthropic from '@anthropic-ai/sdk';\n")

    spec = build_run_spec(tmp_path, "react", port=9123)
    # the needed key passes through...
    assert spec.env.get("ANTHROPIC_API_KEY") == "sk-ant-secret"
    # ...but an unrelated host secret stays scrubbed (NARROW)
    assert "OPENROUTER_API_KEY" not in spec.env
    assert spec.env.get("PATH") == "/usr/bin:/bin"
    assert "ANTHROPIC_API_KEY" in spec.injected
    assert spec.missing_secrets == ()


def test_serve_reports_missing_secret_when_unconfigured(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SKYN3T_ANTHROPIC_API_KEY", raising=False)
    _node_pkg(tmp_path, deps={"@anthropic-ai/sdk": "^0.30.0"})
    (tmp_path / "main.js").write_text("import Anthropic from '@anthropic-ai/sdk';\n")

    spec = build_run_spec(tmp_path, "react", port=9124)
    assert "ANTHROPIC_API_KEY" not in spec.env
    assert "ANTHROPIC_API_KEY" in spec.missing_secrets


def test_serve_plain_app_still_strips_all_secrets(tmp_path: Path, monkeypatch):
    # Regression guard for the original boundary: an app that needs nothing must
    # not receive any host secret.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    spec = build_run_spec(tmp_path, "static", port=9125)
    assert "ANTHROPIC_API_KEY" not in spec.env
    assert spec.injected == ()


# ---- start() surfaces the missing-secret hint ----------------------------

async def test_start_turns_missing_secret_into_actionable_hint(tmp_path: Path, monkeypatch):
    # A served app that exits while missing a required key gets a clear hint
    # instead of the opaque SDK crash. Monkeypatch build_run_spec so the test is
    # independent of the real .env / settings.
    fake = RunSpec(
        cmd=[sys.executable, "-c", "import sys; sys.exit(1)"],  # exits immediately
        cwd=str(tmp_path), env={}, kind="python_web", port=free_port(),
        injected=(), missing_secrets=("ANTHROPIC_API_KEY",),
    )
    monkeypatch.setattr(ar, "build_run_spec", lambda *a, **k: fake)

    app = await AppRunner().start(tmp_path, "fastapi", ready_timeout=5)
    assert app.status == "failed"
    assert app.detail.get("missing_secrets") == ["ANTHROPIC_API_KEY"]
    assert "ANTHROPIC_API_KEY" in app.detail.get("hint", "")
    cleanup_serve(app)
