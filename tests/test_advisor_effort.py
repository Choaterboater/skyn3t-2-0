"""Per-slot reasoning effort for the MoA advisory council.

Ported from hermes-agent v0.19.0 "Quicksilver": a slot may pin its own
reasoning effort with an ``@effort`` suffix (``claude_cli:sonnet@high``), the
council resolves every advisor's effort through ONE chokepoint
(``resolve_advisor_effort``: slot pin > ``moa_advisor_effort`` > "medium",
junk -> default + warning), OpenRouter receives it as
``{"reasoning": {"effort": X}}``, and CLI backends — whose invocation
templates carry no effort flag — treat it as a logged-once no-op. Everything
else about the council (advice assembly, output caps, punt guards) is
unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

import skyn3t.adapters.llm as llm
from skyn3t.adapters.llm import LLMClient, LLMResult
from skyn3t.adapters.model_slot import EFFORT_LEVELS, ModelSlot, parse_slot, parse_slots
from skyn3t.config.settings import Settings
from skyn3t.intelligence.council import CouncilEngine, resolve_advisor_effort

# ---------------------------------------------------------------------------
# Grammar: the "@effort" suffix.
# ---------------------------------------------------------------------------


def test_effort_levels_are_the_three_public_values():
    assert EFFORT_LEVELS == ("low", "medium", "high")


def test_effort_levels_match_the_llm_adapter():
    # llm must not import model_slot (the same independence KNOWN_CLI_PROVIDERS
    # keeps), so the level set is duplicated over there; pin the two together.
    assert set(EFFORT_LEVELS) == set(llm._REASONING_EFFORT_LEVELS)


@pytest.mark.parametrize(
    ("raw", "provider", "model", "effort"),
    [
        ("claude_cli:sonnet@high", "claude_cli", "sonnet", "high"),
        ("openrouter:deepseek/deepseek-v4-flash@low",
         "openrouter", "deepseek/deepseek-v4-flash", "low"),
        ("kimi_cli@medium", "kimi_cli", "", "medium"),  # bare provider + effort
        ("codex_cli:gpt-5.5@HIGH", "codex_cli", "gpt-5.5", "high"),  # case-insensitive
        ("meta-llama/llama-4-70b:free@low", "", "meta-llama/llama-4-70b:free", "low"),
    ],
)
def test_slot_grammar_parses_the_effort_suffix(raw, provider, model, effort):
    slot = parse_slot(raw)

    assert (slot.provider, slot.model, slot.effort) == (provider, model, effort)


def test_address_round_trips_with_and_without_effort():
    for raw in (
        "claude_cli:sonnet@high",
        "openrouter:deepseek/deepseek-v4-flash@low",
        "kimi_cli@medium",
        "claude_cli:sonnet",
        "codex_cli",
        "gpt-5.5",
    ):
        slot = parse_slot(raw)
        assert slot.address == raw
        assert parse_slot(slot.address) == slot


def test_slots_without_a_suffix_inherit():
    assert parse_slot("claude_cli:sonnet").effort is None
    assert parse_slot("codex_cli").effort is None
    assert parse_slots("claude_cli:sonnet@high,kimi_cli")[1].effort is None


def test_a_non_effort_suffix_stays_part_of_the_model_id():
    # "@extreme" is not a level: nothing is consumed and the token parses
    # exactly as it would have before the suffix existed — no silent mangling.
    slot = parse_slot("claude_cli:sonnet@extreme")

    assert slot.effort is None
    assert slot.model == "sonnet@extreme"


def test_parse_slots_round_trips_mixed_effort_entries():
    slots = parse_slots("claude_cli:sonnet@high, kimi_cli ,openrouter:x/y@low")

    assert [(s.address, s.effort) for s in slots] == [
        ("claude_cli:sonnet@high", "high"),
        ("kimi_cli", None),
        ("openrouter:x/y@low", "low"),
    ]
    assert parse_slots(",".join(s.address for s in slots)) == slots


def test_effort_is_part_of_slot_identity():
    pinned = ModelSlot(provider="codex_cli", model="x", effort="high")

    assert pinned != ModelSlot(provider="codex_cli", model="x")
    assert pinned.to_dict()["effort"] == "high"
    assert ModelSlot(provider="codex_cli", model="x").to_dict()["effort"] == ""


# ---------------------------------------------------------------------------
# The one resolution chokepoint.
# ---------------------------------------------------------------------------


def test_chokepoint_slot_pin_wins_over_settings():
    settings = SimpleNamespace(moa_advisor_effort="low")

    assert resolve_advisor_effort(parse_slot("claude_cli:sonnet@high"), settings) == "high"


def test_chokepoint_settings_apply_when_the_slot_does_not_pin():
    settings = SimpleNamespace(moa_advisor_effort="low")

    assert resolve_advisor_effort(parse_slot("claude_cli:sonnet"), settings) == "low"


def test_chokepoint_default_when_nothing_pins():
    assert resolve_advisor_effort(parse_slot("kimi_cli"), SimpleNamespace()) == "medium"
    assert (
        resolve_advisor_effort(parse_slot("kimi_cli"), SimpleNamespace(moa_advisor_effort=""))
        == "medium"
    )


def test_chokepoint_junk_settings_fall_back_to_default_with_a_warning():
    with capture_logs() as logs:
        eff = resolve_advisor_effort(
            parse_slot("kimi_cli"), SimpleNamespace(moa_advisor_effort="extreme")
        )

    assert eff == "medium"
    assert any(
        e["event"] == "moa.advisor_effort_invalid" and e.get("value") == "extreme"
        for e in logs
    )


def test_chokepoint_junk_slot_effort_falls_back_with_a_warning():
    # parse_slot never yields this, but a programmatically-built slot can.
    slot = ModelSlot(provider="kimi_cli", effort="ludicrous")
    with capture_logs() as logs:
        eff = resolve_advisor_effort(slot, SimpleNamespace(moa_advisor_effort="low"))

    assert eff == "medium"
    assert any(e["event"] == "moa.advisor_effort_invalid" for e in logs)


# ---------------------------------------------------------------------------
# llm passthrough: OpenRouter reasoning param, CLI no-op.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _RecordingHTTP:
    """Stands in for the shared keep-alive client; records every POSTed body."""

    def __init__(self):
        self.bodies: list[dict] = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.bodies.append(json)
        return _FakeResp({
            "choices": [{"message": {"content": "advice"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })


def _openrouter_client(tmp_path, monkeypatch):
    http = _RecordingHTTP()
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: http)
    client = LLMClient(Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="openrouter",
        openrouter_api_key="x",
        free_only=False,
    ))
    # Skip router/catalog resolution entirely: pin the (free) model the call
    # lands on so no cost lookup can reach for the live catalog either.
    monkeypatch.setattr(client, "_resolve_pinned_model", lambda **kw: "vendor/m:free")
    return client, http


def test_effort_reaches_the_openrouter_request_body(tmp_path, monkeypatch):
    client, http = _openrouter_client(tmp_path, monkeypatch)

    result = asyncio.run(client.complete("advise", effort="high", max_tokens=64))

    assert result.text == "advice"
    assert http.bodies[-1]["reasoning"] == {"effort": "high"}


def test_no_effort_leaves_the_request_body_untouched(tmp_path, monkeypatch):
    client, http = _openrouter_client(tmp_path, monkeypatch)

    asyncio.run(client.complete("advise", max_tokens=64))

    assert "reasoning" not in http.bodies[-1]


def test_junk_effort_never_reaches_the_wire(tmp_path, monkeypatch):
    client, http = _openrouter_client(tmp_path, monkeypatch)

    asyncio.run(client.complete("advise", effort="extreme", max_tokens=64))

    assert "reasoning" not in http.bodies[-1]


def test_cli_backend_effort_is_a_noop_logged_once(tmp_path, monkeypatch):
    client = LLMClient(Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        llm_backend="codex_cli",
    ))
    seen = {}

    async def fake_cli(provider, prompt, system, json_mode, images=None, *, model=""):
        # The signature has NO effort parameter: if complete() tried to forward
        # effort into the CLI path this would TypeError. argv is untouched.
        seen["provider"] = provider
        return LLMResult(
            text="ok", model="codex-cli", backend="codex_cli",
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0,
        )

    # Pin the route: a host without the codex binary would degrade to stub
    # before the CLI branch, which is exactly the path under test.
    monkeypatch.setattr(client, "_effective_backend", lambda provider_override=None: "codex_cli")
    monkeypatch.setattr(client, "_cli", fake_cli)
    with capture_logs() as logs:
        asyncio.run(client.complete("one", effort="high"))
        asyncio.run(client.complete("two", effort="high"))

    assert seen["provider"] == "codex"
    noops = [e for e in logs if e["event"] == "llm.cli_effort_noop"]
    assert len(noops) == 1  # logged once per provider, not once per call
    assert noops[0]["provider"] == "codex"


# ---------------------------------------------------------------------------
# Council behaviour with effort set: everything else is identical.
# ---------------------------------------------------------------------------


class _FakeCouncilLLM:
    """Records effort + max_tokens per call and returns scripted replies."""

    backend = "codex_cli"

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls: list[dict] = []

    async def complete(self, prompt, tier=None, *, system=None, max_tokens=None,
                       task_type="", model_override=None, provider_override=None,
                       effort=None, **_kw):
        self.calls.append({
            "provider": provider_override or "",
            "model": model_override or "",
            "effort": effort,
            "max_tokens": max_tokens,
        })
        key = provider_override or ""
        reply = self.replies.get(key) or (
            f"advice from {key}: keep the component tree shallow, colocate "
            "state with its consumers, define the API error envelope up front, "
            "and wire routing before styling so every page is reachable."
        )
        return SimpleNamespace(
            text=reply, model=model_override or key,
            backend=provider_override or "codex_cli",
            cost_usd=0.0, status="cli_response",
        )


def _settings(tmp_path, **kw):
    base = {
        "data_dir": tmp_path / "data",
        "logs_dir": tmp_path / "logs",
        "moa_enabled": True,
        "moa_advisors": "claude_cli:sonnet@high,kimi_cli",
        "free_only": False,
        "no_claude": False,
    }
    base.update(kw)
    return Settings(**base)


def test_shipped_default_effort_is_medium(tmp_path):
    settings = _settings(tmp_path)

    assert settings.moa_advisor_effort == "medium"
    assert resolve_advisor_effort(parse_slot("kimi_cli"), settings) == "medium"


def test_every_advisor_call_carries_a_resolved_effort(tmp_path):
    fake = _FakeCouncilLLM()
    advice = asyncio.run(
        CouncilEngine(fake, _settings(tmp_path, moa_advisor_effort="low")).advise(
            brief="a habit tracker"
        )
    )

    assert advice.ok_count == 2
    assert [(c["provider"], c["effort"]) for c in fake.calls] == [
        ("claude_cli", "high"),  # the slot's own pin wins
        ("kimi_cli", "low"),  # no pin -> moa_advisor_effort
    ]
    # The output caps ride along unchanged.
    assert all(c["max_tokens"] == 1200 for c in fake.calls)


def test_council_output_is_identical_whether_or_not_effort_is_set(tmp_path):
    """Effort changes how advisors THINK, never what the council ASSEMBLES."""
    replies = {"kimi_cli": "X" * 5000}
    plain = asyncio.run(CouncilEngine(
        _FakeCouncilLLM(replies),
        _settings(tmp_path / "a", moa_advisors="claude_cli:sonnet,kimi_cli",
                  moa_advisor_block_bytes=600),
    ).advise(brief="a habit tracker", stack="react_ts", plan="src/App.tsx"))
    effort = asyncio.run(CouncilEngine(
        _FakeCouncilLLM(replies),
        _settings(tmp_path / "b", moa_advisors="claude_cli:sonnet@high,kimi_cli@low",
                  moa_advisor_block_bytes=600),
    ).advise(brief="a habit tracker", stack="react_ts", plan="src/App.tsx"))

    assert effort.ok_count == plain.ok_count == 2
    assert [a.text for a in effort.advisors] == [a.text for a in plain.advisors]
    # The byte caps still bind with effort set, and the only guidance
    # difference is that labels name the configured pin.
    assert "truncated" in effort.guidance
    assert effort.guidance.replace("@high", "").replace("@low", "") == plain.guidance


def test_punt_guard_still_fires_with_effort_pinned(tmp_path):
    fake = _FakeCouncilLLM(replies={
        "claude_cli": "Ready as reference advisor. Send me the task.",
    })
    advice = asyncio.run(
        CouncilEngine(fake, _settings(tmp_path)).advise(brief="a habit tracker")
    )

    by_label = {a.label: a for a in advice.advisors}
    assert not by_label["claude_cli:sonnet@high"].ok
    assert "not usable guidance" in by_label["claude_cli:sonnet@high"].error
    assert by_label["kimi_cli"].ok
    assert "kimi_cli" in advice.guidance
    assert "claude_cli" not in advice.guidance


def test_a_dead_advisor_with_effort_is_recorded_not_retried(tmp_path):
    class _DownLLM(_FakeCouncilLLM):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def complete(self, prompt, **kw):
            if kw.get("provider_override") == "claude_cli":
                self.attempts += 1
                raise RuntimeError("claude_cli is down")
            return await super().complete(prompt, **kw)

    fake = _DownLLM()
    advice = asyncio.run(
        CouncilEngine(fake, _settings(tmp_path)).advise(brief="a habit tracker")
    )

    by_label = {a.label: a for a in advice.advisors}
    assert not by_label["claude_cli:sonnet@high"].ok
    assert "claude_cli is down" in by_label["claude_cli:sonnet@high"].error
    assert advice.degraded is True
    # One attempt exactly — a stale/dead slot is recorded, never retried.
    assert fake.attempts == 1
    assert by_label["kimi_cli"].ok
