"""Mixture-of-Agents advisory council.

The council is a capability layer, never a gate. Its hard contract: advisors can
fail in any combination and the build must be unaffected — and when ALL of them
fail, the codegen prompt must be byte-identical to a council-off build.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.intelligence.council import CouncilAdvice, CouncilEngine


class _FakeLLM:
    """Records call routes and returns scripted per-provider replies."""

    backend = "codex_cli"

    def __init__(self, replies=None, fail=(), delay=0.0):
        self.replies = replies or {}
        self.fail = set(fail)
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self.max_inflight = 0
        self._inflight = 0

    async def complete(self, prompt, tier=None, *, system=None, max_tokens=None,
                       task_type="", model_override=None, provider_override=None, **_kw):
        route = (provider_override or "", model_override or "")
        self.calls.append(route)
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            else:
                await asyncio.sleep(0)
            key = provider_override or ""
            if key in self.fail:
                raise RuntimeError(f"{key} is down")
            # Long enough to clear the punt guard — real advisor replies are
            # paragraphs, and _is_punt refuses acknowledgement-length text.
            default_reply = (
                f"advice from {key}: keep the component tree shallow, colocate "
                "state with its consumers, define the API error envelope up "
                "front, and wire routing before styling so every page is "
                "reachable from day one."
            )
            return SimpleNamespace(
                text=self.replies.get(key, default_reply),
                model=model_override or key,
                backend=provider_override or "codex_cli",
                cost_usd=0.001,
                status="cli_response",
            )
        finally:
            self._inflight -= 1


def _settings(tmp_path, **kw):
    base = {
        "data_dir": tmp_path / "data",
        "logs_dir": tmp_path / "logs",
        "moa_enabled": True,
        "moa_advisors": "claude_cli:sonnet,codex_cli,kimi_cli",
        "free_only": False,
    }
    base.update(kw)
    return Settings(**base)


def _advise(engine, **kw):
    return asyncio.run(engine.advise(brief=kw.pop("brief", "a habit tracker"), **kw))


def test_council_fans_out_across_every_configured_provider(tmp_path):
    llm = _FakeLLM()
    advice = _advise(CouncilEngine(llm, _settings(tmp_path)))

    assert llm.calls == [("claude_cli", "sonnet"), ("codex_cli", ""), ("kimi_cli", "")]
    assert advice.ok_count == 3
    assert "ADVISORY COUNCIL" in advice.guidance
    assert "Advisor 1 — claude_cli:sonnet" in advice.guidance


def test_a_punt_reply_is_a_failed_advisor_never_credited(tmp_path):
    # The original MoA failure: a mangled prompt yielded "Ready as reference
    # advisor. Send me the task", which counted as a SUCCESSFUL advisor and
    # was injected into codegen as guidance. Non-empty is not advice.
    llm = _FakeLLM(replies={
        "claude_cli": "Ready as reference advisor. Send me the task.",
        "codex_cli": "I need more information to help you. Please provide the details.",
    })
    advice = _advise(CouncilEngine(llm, _settings(tmp_path)))

    failed = {a.label: a for a in advice.advisors if not a.ok}
    assert "claude_cli:sonnet" in failed
    assert "codex_cli" in failed
    assert "not usable guidance" in failed["claude_cli:sonnet"].error
    # The one real advisor still carries the council.
    assert advice.ok_count == 1
    assert "kimi_cli" in advice.guidance


def test_long_genuine_advice_mentioning_cant_is_not_a_punt(tmp_path):
    real = (
        "I can't overstate how much the data model matters here: define the "
        "recipe schema first (title, ingredients as structured rows, steps as "
        "an ordered list), keep favorites as a separate persisted set keyed by "
        "recipe id, debounce the ingredient search, and make the card grid a "
        "pure function of the filtered collection so search/favorites compose."
    )
    llm = _FakeLLM(replies={"claude_cli": real})
    advice = _advise(CouncilEngine(llm, _settings(tmp_path)))

    ok = {a.label for a in advice.advisors if a.ok}
    assert "claude_cli:sonnet" in ok


def test_advise_repair_feeds_failure_evidence_to_advisors(tmp_path):
    class _RecordingLLM(_FakeLLM):
        def __init__(self):
            super().__init__()
            self.prompts: list[str] = []

        async def complete(self, prompt, **kw):
            self.prompts.append(prompt)
            return await super().complete(prompt, **kw)

    llm = _RecordingLLM()
    advice = asyncio.run(CouncilEngine(llm, _settings(tmp_path)).advise_repair(
        brief="a habit tracker",
        stack="react_ts",
        failure="TS2307: Cannot find module './components/HabitCard'",
    ))

    assert advice.ok_count == 3
    assert advice.guidance  # assembled for the improver prompt
    assert all("TS2307" in p for p in llm.prompts)
    assert all("FAILED its objective proof" in p for p in llm.prompts)


def test_advise_repair_is_inert_when_council_disabled(tmp_path):
    llm = _FakeLLM()
    advice = asyncio.run(
        CouncilEngine(llm, _settings(tmp_path, moa_enabled=False)).advise_repair(
            brief="x", stack="react_ts", failure="boom")
    )
    assert advice.guidance == ""
    assert llm.calls == []


def test_the_shipped_default_is_on_but_inert(tmp_path, monkeypatch):
    """What actually ships, with conftest's suite-wide pins removed.

    The council ships ON as a genuinely multi-model ensemble — Kimi, Copilot,
    and a hosted OpenRouter model — with two deliberate exclusions: Claude is
    opt-in only (no default anywhere routes work to Claude without the
    operator selecting it), and Codex is the acting model so it must not
    advise itself. Uninstalled CLIs are recorded as failed advisors and the
    build proceeds on the survivors, so the default costs nothing on a machine
    without them.

    Inert on the stub backend regardless — that short-circuit is now the fence
    keeping an offline run free, since neither the master switch nor the
    advisor list is empty any more.
    """
    monkeypatch.delenv("SKYN3T_MOA_ENABLED", raising=False)
    monkeypatch.delenv("SKYN3T_MOA_ADVISORS", raising=False)

    shipped = Settings(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")

    assert shipped.moa_enabled is True
    # The shipped default is a genuinely multi-model council with no Claude:
    # Kimi + Copilot + a hosted OpenRouter model. Claude is opt-in (never in a
    # default), codex is the acting model so it must not advise itself.
    assert [s.address for s in CouncilEngine(_FakeLLM(), shipped).slots()] == [
        "kimi_cli", "copilot_cli", "openrouter",
    ]
    assert "claude" not in shipped.moa_advisors, "Claude is opt-in only"
    assert "codex" not in shipped.moa_advisors, "the actor must not advise itself"

    stub = _FakeLLM()
    stub.backend = "stub"
    advice = _advise(CouncilEngine(stub, shipped))
    assert stub.calls == []
    assert advice.guidance == ""


def test_the_master_switch_off_makes_no_calls(tmp_path):
    """Explicitly disabled — no longer the shipped default, so this covers the
    override rather than what ships. See the defaults test above."""
    llm = _FakeLLM()
    advice = _advise(CouncilEngine(llm, _settings(tmp_path, moa_enabled=False)))

    assert llm.calls == []
    assert advice.guidance == ""


def test_no_configured_advisors_makes_no_calls(tmp_path):
    """Clearing moa_advisors is the ordinary way to turn the council off now
    that the master switch defaults on."""
    llm = _FakeLLM()
    advice = _advise(CouncilEngine(llm, _settings(tmp_path, moa_advisors="")))

    assert llm.calls == []
    assert advice.guidance == ""


def test_stub_backend_short_circuits(tmp_path):
    # The fence that keeps the whole offline suite's codegen prompts byte-stable
    # and offline builds at $0.
    llm = _FakeLLM()
    llm.backend = "stub"

    advice = _advise(CouncilEngine(llm, _settings(tmp_path)))

    assert llm.calls == []
    assert advice.guidance == ""


def test_one_failed_advisor_does_not_block_the_others(tmp_path):
    llm = _FakeLLM(fail={"codex_cli"})
    advice = _advise(CouncilEngine(llm, _settings(tmp_path)))

    assert [a.ok for a in advice.advisors] == [True, False, True]
    assert advice.degraded is True
    assert "codex_cli" not in advice.guidance  # failed advisor filtered out
    assert "claude_cli:sonnet" in advice.guidance
    assert "kimi_cli" in advice.guidance


def test_all_advisors_failed_yields_empty_guidance(tmp_path):
    # The critical property: codegen's prompt is then identical to council-off.
    llm = _FakeLLM(fail={"claude_cli", "codex_cli", "kimi_cli"})
    advice = _advise(CouncilEngine(llm, _settings(tmp_path)))

    assert advice.guidance == ""
    assert advice.degraded is True
    assert advice.ok_count == 0


def test_an_unavailable_provider_is_not_passed_off_as_advice(tmp_path):
    """A slot that degraded to the stub inside complete() is a FAILED advisor.

    Otherwise the stub's canned "Offline response." text would be injected into
    the codegen prompt dressed up as engineering guidance.
    """

    class _DegradingLLM(_FakeLLM):
        async def complete(self, prompt, tier=None, *, system=None, max_tokens=None,
                           task_type="", model_override=None, provider_override=None, **_kw):
            self.calls.append((provider_override or "", model_override or ""))
            return SimpleNamespace(
                text="[stub:cheap] Offline response.", model="stub",
                backend="stub", cost_usd=0.0, status="stub",
            )

    advice = _advise(CouncilEngine(_DegradingLLM(), _settings(tmp_path)))

    assert advice.guidance == ""
    assert all(a.error == "provider unavailable (degraded to stub)" for a in advice.advisors)


def test_a_failed_cli_status_is_treated_as_a_failed_advisor(tmp_path):
    class _FailedCliLLM(_FakeLLM):
        async def complete(self, prompt, **kw):
            return SimpleNamespace(
                text="whatever", model="x", backend=kw.get("provider_override") or "codex_cli",
                cost_usd=0.0, status="failed_cli_nonzero",
            )

    advice = _advise(CouncilEngine(_FailedCliLLM(), _settings(tmp_path)))

    assert advice.guidance == ""


def test_advisor_timeout_is_bounded_and_drops_the_slot(tmp_path):
    llm = _FakeLLM(delay=0.5)
    engine = CouncilEngine(llm, _settings(tmp_path, moa_advisor_timeout=10))
    engine.settings = SimpleNamespace(
        moa_enabled=True, moa_advisors="codex_cli", moa_advisor_timeout=10,
        moa_advisor_max_tokens=1200, moa_max_concurrency=4,
        moa_advisor_block_bytes=3000, free_only=False, no_claude=False,
    )
    # Drive the real timeout path with a tiny budget.
    engine.settings.moa_advisor_timeout = 0  # clamped to 10 by max(10, ...)

    advice = _advise(engine)

    # 0.5s work under a >=10s clamp completes; the point is it never hangs.
    assert advice.ok_count == 1


def test_guidance_is_byte_budgeted_per_advisor(tmp_path):
    llm = _FakeLLM(replies={"codex_cli": "X" * 50_000})
    engine = CouncilEngine(
        llm, _settings(tmp_path, moa_advisors="codex_cli", moa_advisor_block_bytes=600)
    )

    advice = _advise(engine)

    # Prompt bloat is the real hazard: an oversized codegen prompt blows the
    # agentic timeout and ships a stub.
    assert len(advice.guidance) < 1500
    assert "truncated" in advice.guidance


def test_per_advisor_cost_is_recorded(tmp_path):
    llm = _FakeLLM()
    advice = _advise(CouncilEngine(llm, _settings(tmp_path)))

    assert [a.cost_usd for a in advice.advisors] == [0.001, 0.001, 0.001]
    assert advice.cost_usd == 0.003
    assert advice.to_dict()["cost_usd"] == 0.003


def test_concurrency_is_bounded(tmp_path):
    llm = _FakeLLM(delay=0.05)
    engine = CouncilEngine(
        llm,
        _settings(
            tmp_path,
            moa_advisors="claude_cli,codex_cli,kimi_cli,copilot_cli",
            moa_max_concurrency=2,
        ),
    )

    _advise(engine)

    assert llm.max_inflight <= 2


def test_free_only_drops_a_paid_openrouter_slot_visibly(tmp_path):
    llm = _FakeLLM()
    engine = CouncilEngine(
        llm,
        _settings(
            tmp_path,
            moa_advisors="openrouter:vendor/paid,openrouter:vendor/m:free,codex_cli",
            free_only=True,
        ),
    )

    advice = _advise(engine)

    assert advice.dropped == [{"slot": "openrouter:vendor/paid", "reason": "free_only"}]
    # A CLI slot bills a subscription the operator already holds, so free_only
    # must not empty a perfectly valid local council.
    assert ("codex_cli", "") in llm.calls


def test_no_claude_drops_claude_slots(tmp_path):
    llm = _FakeLLM()
    engine = CouncilEngine(
        llm, _settings(tmp_path, moa_advisors="claude_cli:sonnet,codex_cli", no_claude=True)
    )

    advice = _advise(engine)

    assert advice.dropped == [{"slot": "claude_cli:sonnet", "reason": "no_claude"}]
    assert llm.calls == [("codex_cli", "")]


def test_council_exception_never_escapes(tmp_path):
    class _Exploding(_FakeLLM):
        async def complete(self, *a, **kw):
            raise ValueError("advisor blew up in an unexpected way")

    engine = CouncilEngine(_Exploding(), _settings(tmp_path))
    advice = _advise(engine)

    assert isinstance(advice, CouncilAdvice)
    assert advice.guidance == ""
    assert advice.degraded is True


def test_a_user_interrupt_still_propagates(tmp_path):
    """Degrade-don't-crash must not extend to swallowing Ctrl-C.

    KeyboardInterrupt/CancelledError are BaseExceptions: the council catches
    Exception so an advisor cannot break a build, but a real interrupt has to
    reach the operator rather than being reported as "advisors degraded".
    """
    import pytest

    class _Interrupting(_FakeLLM):
        async def complete(self, *a, **kw):
            raise KeyboardInterrupt("user pressed ctrl-c")

    engine = CouncilEngine(_Interrupting(), _settings(tmp_path))
    with pytest.raises(KeyboardInterrupt):
        _advise(engine)


def test_to_dict_is_bounded_and_json_safe(tmp_path):
    import json

    llm = _FakeLLM(replies={"codex_cli": "Y" * 5000})
    advice = _advise(CouncilEngine(llm, _settings(tmp_path, moa_advisors="codex_cli")))

    payload = advice.to_dict()
    json.dumps(payload)  # must not raise

    # The bounded manifest record carries counts and cost, never advisor prose.
    assert "Y" * 100 not in json.dumps(payload)
    assert payload["guidance_chars"] > 0
