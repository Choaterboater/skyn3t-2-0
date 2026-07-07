# tests/test_iter5_fixes.py
"""Iteration-5 fixes: sandbox hardening/network, LLM cost+crash-safety, channel
proposal-decision payload shape, debate vote bias, retry backoff jitter."""
from __future__ import annotations

import asyncio
import json
import warnings

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus, EventType
from skyn3t.intelligence.debate import _parse_vote

# --- debate: invalid votes must not silently become a vote for proposals[0] --

def test_parse_vote_valid_first_proposal():
    assert _parse_vote("I vote proposal 1", 3) == 0


def test_parse_vote_out_of_range_returns_none():
    assert _parse_vote("proposal 5", 3) is None


def test_parse_vote_unparseable_returns_none():
    assert _parse_vote("they're all equally good", 3) is None


# --- sandbox: tmpfs hardening must actually be applied (was `if False`) -------

def _docker_settings(**over):
    base = dict(sandbox_hardening=True, sandbox_drop_caps=False, execution_backend="docker")
    base.update(over)
    return Settings(**base)


def test_sandbox_hardening_adds_workdir_tmpfs(tmp_path):
    from skyn3t.security.sandbox import SandboxResult, SandboxRunner

    runner = SandboxRunner(settings=_docker_settings())
    captured = {}

    async def fake_exec(cmd, *, cwd, timeout, backend):
        captured["cmd"] = cmd
        return SandboxResult(0, "", "", backend, 0.0)

    runner._exec = fake_exec  # type: ignore[method-assign]
    asyncio.run(runner._run_docker("echo hi", cwd=tmp_path, timeout=5,
                                   image=None, stack=None, env={}, network=False))
    cmd = captured["cmd"]
    # the workdir tmpfs restriction must be present when hardening + cwd
    assert "--tmpfs" in cmd and any("/work/.tmp" in c for c in cmd)


def test_sandbox_docker_timeout_removes_named_container(tmp_path, monkeypatch):
    from skyn3t.security.sandbox import SandboxResult, SandboxRunner
    import skyn3t.security.sandbox as sandbox_mod

    runner = SandboxRunner(settings=_docker_settings())
    captured = {"cleanup": []}

    async def fake_exec(cmd, *, cwd, timeout, backend):
        captured["cmd"] = cmd
        return SandboxResult(124, "", "timeout", backend, 0.0, timed_out=True)

    def fake_run(cmd, **_kwargs):
        captured["cleanup"].append(cmd)

        class _R:
            returncode = 0
        return _R()

    runner._exec = fake_exec  # type: ignore[method-assign]
    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)

    asyncio.run(runner._run_docker("sleep 30", cwd=tmp_path, timeout=1,
                                   image=None, stack=None, env={}, network=False))

    cmd = captured["cmd"]
    name = cmd[cmd.index("--name") + 1]
    assert name.startswith("skyn3t-")
    assert ["docker", "rm", "-f", name] in captured["cleanup"]


def test_sandbox_subprocess_fallback_warns_about_network(tmp_path):
    from skyn3t.security.sandbox import SandboxRunner

    runner = SandboxRunner(settings=Settings(execution_backend="subprocess"))

    async def force_subprocess():
        return "subprocess"
    runner._choose_backend_async = force_subprocess  # type: ignore[method-assign]

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        asyncio.run(runner.run("echo hi", cwd=tmp_path, network=False))
    msgs = " ".join(str(x.message).lower() for x in w)
    assert "network" in msgs  # downgrade is surfaced, not silent


# --- channels: a proposal decision must reach the handler's expected shape ---

def test_channel_decide_emits_approved_key():
    from skyn3t.integrations.channels import Channel, InboundMessage

    class _Ch(Channel):
        name = "test"
        def is_available(self): return True
        async def handle_inbound(self, msg): return {}
        async def send(self, target, text): return True

    bus = EventBus()
    seen = []
    async def on_dec(e): seen.append(e.payload)
    bus.subscribe(EventType.PROPOSAL_DECIDED, on_dec)
    ch = _Ch(event_bus=bus)
    msg = InboundMessage(channel="test", text="/approve p1", sender="u", target="t")

    asyncio.run(ch._decide("p1", "approve", msg))
    assert seen and "approved" in seen[0]
    assert seen[0]["approved"] is True


# --- orchestrator backoff: jitter so concurrent retries don't synchronize ----

def test_backoff_has_jitter_and_stays_bounded():
    from skyn3t.core.orchestrator import _backoff_delay

    # Cap respected (with the jitter band), and not all identical (jittered).
    vals = [_backoff_delay(1) for _ in range(20)]
    assert all(0.0 <= v <= 5.0 * 1.5 for v in vals)
    assert len(set(vals)) > 1  # not deterministic anymore


# --- LLM adapter: missing usage must not report $0 for a paid model ----------

class _Resp:
    def __init__(self, payload, raises=None):
        self._payload = payload
        self._raises = raises
    def raise_for_status(self):
        return None
    def json(self):
        if self._raises:
            raise self._raises
        return self._payload


class _Client:
    def __init__(self, resp):
        self._resp = resp
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k): return self._resp


def _llm_with(monkeypatch, resp):
    import skyn3t.adapters.llm as llm_mod
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", lambda *a, **k: _Client(resp))
    client = llm_mod.LLMClient(Settings(openrouter_api_key="sk-test"))
    return client


def test_openrouter_missing_usage_paid_model_nonzero_cost(monkeypatch):
    client = _llm_with(monkeypatch, _Resp({"choices": [{"message": {"content": "hello world"}}]}))
    res = asyncio.run(client._openrouter("deepseek/deepseek-chat", "a prompt", "", 100, False))
    assert res.text == "hello world"
    assert res.cost_usd > 0.0  # paid model must not be free just because usage was absent


def test_openrouter_malformed_json_degrades_not_crash(monkeypatch):
    client = _llm_with(monkeypatch, _Resp(None, raises=json.JSONDecodeError("x", "y", 0)))
    res = asyncio.run(client._openrouter("deepseek/deepseek-chat", "a prompt", "", 100, False))
    assert res.backend == "stub"  # degraded, did not raise
