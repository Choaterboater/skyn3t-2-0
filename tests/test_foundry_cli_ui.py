"""Source contract tests for the Foundry CLI backend controls."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_SRC = ROOT / "skyn3t" / "web" / "ui" / "src"


def test_foundry_cli_options_use_global_backend_contract() -> None:
    helper = (UI_SRC / "cliBackends.js").read_text(encoding="utf-8")
    settings = (UI_SRC / "routes" / "Settings.jsx").read_text(encoding="utf-8")
    studio = (UI_SRC / "routes" / "Studio.jsx").read_text(encoding="utf-8")

    for backend in ("codex_cli", "claude_cli", "copilot_cli"):
        assert f'id: "{backend}"' in helper

    assert 'queryFn("/llm/backends")' in settings
    assert 'apiPost("/llm/backend", { backend: b })' in settings
    assert 'queryFn("/llm/backends")' in studio
    assert 'apiPost("/llm/backend", { backend })' in studio
    assert "Persisted globally for all future Foundry runs" in studio


def test_foundry_cli_options_expose_availability_and_account_billing() -> None:
    helper = (UI_SRC / "cliBackends.js").read_text(encoding="utf-8")
    settings = (UI_SRC / "routes" / "Settings.jsx").read_text(encoding="utf-8")
    studio = (UI_SRC / "routes" / "Studio.jsx").read_text(encoding="utf-8")

    assert "cli_details" in helper
    assert "cli_available" in helper
    assert "Usage limits and billing follow" in helper
    assert "plan or subscription" in helper
    billing_copy = helper.split("export function cliAccountBillingText", 1)[1].split(
        "export function", 1
    )[0]
    assert "free" not in billing_copy.lower()
    assert "An explicitly selected CLI never falls back to OpenRouter" in settings
    assert "selectedFoundryCliUnavailable" in studio
    assert "selectedFoundryOpenRouterMissing" in studio
    assert "selectedFoundryUnavailable" in studio
    assert "codegenCliUnavailable" in studio
    assert "codegen-only {codegenCliProvider} CLI override is unavailable" in studio
    assert "OpenRouter is selected but its API key is missing" in studio
    assert "does not silently switch to OpenRouter" in studio
