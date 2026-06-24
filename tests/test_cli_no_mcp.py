"""Headless build CLIs run without the host's ambient MCP servers.

A skyn3t build shells out to `claude -p` / `kimi -p` for codegen. Without
isolation each call boots the user's whole ~/.claude MCP fleet (Aruba,
context7, playwright, ...) — pure per-build startup tax and a sandboxing
concern. `cli_disable_mcp` (default True) appends the provider's MCP-off flag.
"""

from __future__ import annotations

from skyn3t.adapters.llm import _CLI_COMMANDS, _no_mcp_args
from skyn3t.config.settings import Settings


def test_disable_mcp_default_on():
    assert Settings().cli_disable_mcp is True


def test_no_mcp_args_per_provider_when_enabled():
    s = Settings()  # cli_disable_mcp=True by default
    # claude + kimi (a claude-compatible fork) ignore all ambient MCP config.
    assert _no_mcp_args(s, "claude") == ["--strict-mcp-config"]
    assert _no_mcp_args(s, "kimi") == ["--strict-mcp-config"]
    # copilot has no --strict-mcp-config; it disables its built-in MCP servers.
    assert _no_mcp_args(s, "copilot") == ["--disable-builtin-mcps"]
    # Unknown providers get nothing (no flag we can vouch for).
    assert _no_mcp_args(s, "openai") == []


def test_no_mcp_args_suppressed_when_disabled():
    s = Settings(cli_disable_mcp=False)
    for prov in ("claude", "kimi", "copilot"):
        assert _no_mcp_args(s, prov) == []


def test_assembled_argv_includes_isolation_flag():
    s = Settings()
    argv = [*_CLI_COMMANDS["claude"], *_no_mcp_args(s, "claude")]
    assert argv == ["claude", "-p", "--strict-mcp-config"]


def test_returned_list_is_a_copy():
    """Callers splat the result into argv; it must not alias the module constant."""
    s = Settings()
    got = _no_mcp_args(s, "claude")
    got.append("--mutated")
    assert _no_mcp_args(s, "claude") == ["--strict-mcp-config"]
