"""Output-side secret masking (``skyn3t.security.secrets.mask_secrets``).

``filter_env`` scrubs host credentials on the way IN to sandboxed code; this
pins the complement on the way OUT (ported from OpenHands SecretRegistry): any
REGISTERED secret value that turns up in gate verdicts, captured proof command
output, agent tool observations, or the CLI prose tail is replaced with
``<secret-hidden>`` before the text is recorded or returned.

The four masked surfaces, one test each: the MCP gate, the CLI gate, proof
command capture (build/test output → build_summary/manifests), and the LLM
adapter's tool-result + output_text paths. Plus the unit contract: short /
benign / unset values are never masked, and ``reset_mask_cache`` re-discovers.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

import skyn3t.adapters.llm as llm
from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import Settings, get_settings
from skyn3t.security.secrets import (
    MASKED,
    MOCK_PROOF_VALUE,
    mask_secrets,
    reset_mask_cache,
)
from skyn3t.studio.cli_check import check_cli
from skyn3t.studio.mcp_check import check_mcp
from skyn3t.studio.proof_run import _ProofCommandResult

# Long enough to register (>= 12) and matching NONE of the high-precision token
# patterns — proving the masking is value-based, not pattern-based.
FAKE_KEY = "TESTKEY-not-a-real-key-0123456789abcdef"


@pytest.fixture
def registered_key(monkeypatch):
    """Register FAKE_KEY as a host secret and reset the compiled mask cache
    around the test (the cache is process-global; never leak it across tests)."""
    monkeypatch.setenv("SKYN3T_OPENROUTER_API_KEY", FAKE_KEY)
    get_settings.cache_clear()
    reset_mask_cache()
    yield FAKE_KEY
    reset_mask_cache()
    get_settings.cache_clear()


# ---- unit contract ----------------------------------------------------------


def test_masks_every_occurrence_of_a_registered_key(registered_key):
    masked = mask_secrets(f"prefix {FAKE_KEY} middle {FAKE_KEY} suffix")
    assert FAKE_KEY not in masked
    assert masked.count(MASKED) == 2


def test_bare_provider_env_var_registers(monkeypatch):
    key = "bare-provider-key-abcdef0123456789"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    get_settings.cache_clear()
    reset_mask_cache()
    try:
        assert mask_secrets(f"token: {key}!") == f"token: {MASKED}!"
    finally:
        reset_mask_cache()


def test_short_values_are_never_masked(monkeypatch):
    # < 12 chars: masking ordinary words/paths would false-positive constantly.
    monkeypatch.setenv("SKYN3T_OPENROUTER_API_KEY", "short-key")
    get_settings.cache_clear()
    reset_mask_cache()
    try:
        assert mask_secrets("the short-key stays put") == "the short-key stays put"
    finally:
        reset_mask_cache()


def test_benign_words_numbers_and_the_mock_sentinel_are_never_masked(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTH_TOKEN", "true")
    monkeypatch.setenv("SKYN3T_KIMI_API_KEY", "12345678901234567890")
    monkeypatch.setenv("SKYN3T_ANTHROPIC_API_KEY", MOCK_PROOF_VALUE)
    get_settings.cache_clear()
    reset_mask_cache()
    try:
        text = f"true 12345678901234567890 {MOCK_PROOF_VALUE}"
        assert mask_secrets(text) == text
    finally:
        reset_mask_cache()


def test_no_secret_registered_is_a_pure_passthrough():
    reset_mask_cache()
    try:
        text = "ordinary build output with /a/path, words and a fake-ish TESTKEY"
        assert mask_secrets(text) == text
    finally:
        reset_mask_cache()


def test_empty_and_tiny_inputs_pass_through(registered_key):
    assert mask_secrets("") == ""
    assert mask_secrets(None) is None
    assert mask_secrets("tiny") == "tiny"


def test_reset_mask_cache_picks_up_newly_registered_values(monkeypatch):
    key = "late-registered-key-abcdef012345"
    reset_mask_cache()
    try:
        assert mask_secrets(f"echo {key}") == f"echo {key}"  # not registered yet
        monkeypatch.setenv("SKYN3T_REPLICATE_API_TOKEN", key)
        get_settings.cache_clear()
        reset_mask_cache()
        assert mask_secrets(f"echo {key}") == f"echo {MASKED}"
    finally:
        reset_mask_cache()


# ---- surface 1: the MCP gate -------------------------------------------------


def test_mcp_gate_masks_secrets_in_gate_output(tmp_path, registered_key):
    # The key is hardcoded in the delivered server — filter_env keeps host keys
    # out of the subprocess env, so this simulates a credential that reached
    # the delivered code by ANY other channel and is parroted onto stderr.
    (tmp_path / "server.py").write_text(
        "import sys\n"
        f"print('fatal: handshake needs {FAKE_KEY}', file=sys.stderr)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    verdict = check_mcp(tmp_path, stack="mcp")
    assert not verdict.ok and not verdict.skipped  # a real crash issue, recorded
    recorded = json.dumps(verdict.to_dict())  # issues + checked + gaps
    assert FAKE_KEY not in recorded
    assert MASKED in recorded


# ---- surface 2: the CLI gate -------------------------------------------------


def test_cli_gate_masks_secrets_in_gate_output(tmp_path, registered_key):
    (tmp_path / "main.py").write_text(
        "import sys\n"
        "print('usage: main.py ...', file=sys.stderr)\n"
        f"print('auth failed for {FAKE_KEY}', file=sys.stderr)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    verdict = check_cli(tmp_path, stack="python_cli")
    assert not verdict.ok and not verdict.skipped
    recorded = json.dumps(verdict.to_dict())
    assert FAKE_KEY not in recorded
    assert MASKED in recorded


# ---- surface 3: proof command capture (build_summary / manifests) ------------


def test_proof_command_output_is_masked_at_capture(registered_key):
    res = _ProofCommandResult(1, f"build ok {FAKE_KEY}", f"error: {FAKE_KEY}\n")
    assert FAKE_KEY not in res.stdout
    assert FAKE_KEY not in res.stderr
    assert MASKED in res.stdout
    assert MASKED in res.stderr
    # The ANSI strip still applies alongside masking.
    colored = _ProofCommandResult(1, "\x1b[96mtests/links.test.ts\x1b[0m", "")
    assert "\x1b" not in colored.stdout
    # Benign output is untouched.
    plain = _ProofCommandResult(0, "compiled 12 modules in 3s", "")
    assert plain.stdout == "compiled 12 modules in 3s"


# ---- surface 4: the LLM adapter (tool observation + output_text) -------------


def test_capture_agentic_output_text_masks_prose(registered_key):
    parts, budget = [], [10_000]
    llm._capture_agentic_output_text(
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"reading key {FAKE_KEY} now"}]}},
        parts,
        budget,
    )
    llm._capture_agentic_output_text(
        {"type": "result", "result": f"done with {FAKE_KEY}"}, parts, budget
    )
    joined = "\n".join(parts)
    assert FAKE_KEY not in joined
    assert joined.count(MASKED) == 2


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _RecordingClient:
    """Replays canned OpenRouter responses, recording every request body."""

    def __init__(self, turns):
        self._turns, self.i, self.bodies = list(turns), 0, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        # Deep-copy: the loop keeps mutating its canonical `messages` list, and
        # a shrunk copy may alias it — recording by reference would "rewrite"
        # earlier requests with later turns.
        self.bodies.append(copy.deepcopy(json))
        if self.i >= len(self._turns):
            return _FakeResp({"choices": [{"message": {"content": "done"}}]})
        payload = self._turns[self.i]
        self.i += 1
        return _FakeResp(payload)


def _tool_turn(name, args, tcid="t1"):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": tcid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def test_agentic_loop_masks_tool_observation_and_output_text(
    tmp_path, monkeypatch, registered_key
):
    # A credential that reached the worktree is read back by the agent's own
    # read_file tool; the observation must be masked before it re-enters the
    # message history sent to the provider.
    (tmp_path / "config.txt").write_text(f"api_key = {FAKE_KEY}\n", encoding="utf-8")
    client = _RecordingClient([
        _tool_turn("read_file", {"path": "config.txt"}),
        {"choices": [{"message": {"content": f"final answer echoes {FAKE_KEY}"}}]},
    ])
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: client)
    res = asyncio.run(
        LLMClient(Settings(llm_backend="openrouter", openrouter_api_key="x"))
        ._openrouter_agentic(
            "build", str(tmp_path), "m", stack="fastapi", verify_on_stop=False)
    )
    # The request carrying the read_file observation has it masked.
    tool_bodies = [
        body for body in client.bodies
        if any(m.get("role") == "tool" for m in body["messages"])
    ]
    assert tool_bodies, "expected the read_file tool result in the resent history"
    sent = json.dumps(tool_bodies[0])
    assert FAKE_KEY not in sent
    tool_msgs = [m for m in tool_bodies[0]["messages"] if m.get("role") == "tool"]
    assert any(MASKED in m["content"] for m in tool_msgs)
    # The bounded prose tail returned to the agent loop is masked too.
    assert FAKE_KEY not in res["output_text"]
    assert MASKED in res["output_text"]
