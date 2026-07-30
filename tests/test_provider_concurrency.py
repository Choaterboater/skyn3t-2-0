"""Per-provider dispatch admission.

A resource bound, never a gate: it queues work, it never rejects it. Keyed per
event loop because an asyncio.Semaphore binds to its creating loop and this repo
calls asyncio.run() from the CLI and from many tests.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from skyn3t.adapters.provider_limits import (
    _ADMISSIONS,
    provider_slot,
    reset_provider_limits,
    resolve_provider_limit,
)
from skyn3t.config.settings import Settings


async def _peak_inflight(provider: str, limit: int, tasks: int) -> int:
    inflight = 0
    peak = 0

    async def _one() -> None:
        nonlocal inflight, peak
        async with provider_slot(provider, limit):
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            inflight -= 1

    await asyncio.gather(*(_one() for _ in range(tasks)))
    return peak


async def test_dispatches_are_bounded_per_provider():
    reset_provider_limits()

    assert await _peak_inflight("codex_cli", 2, 6) == 2


async def test_limits_are_independent_across_providers():
    reset_provider_limits()

    async def _hold(provider: str, ev: asyncio.Event) -> None:
        async with provider_slot(provider, 1):
            await ev.wait()

    release = asyncio.Event()
    holder = asyncio.create_task(_hold("codex_cli", release))
    await asyncio.sleep(0)

    # A saturated codex_cli must not delay an openrouter dispatch.
    reached = False
    async with provider_slot("openrouter", 1):
        reached = True
    release.set()
    await holder

    assert reached is True


async def test_zero_limit_disables_bounding():
    reset_provider_limits()

    assert await _peak_inflight("codex_cli", 0, 5) == 5
    assert not _ADMISSIONS  # nothing registered when unbounded


async def test_unnamed_provider_yields_immediately():
    reset_provider_limits()

    assert await _peak_inflight("", 1, 3) == 3


async def test_admission_is_torn_down_when_idle():
    reset_provider_limits()

    await _peak_inflight("codex_cli", 2, 4)

    # Refcounted teardown: a long-lived process must not accumulate one
    # semaphore per (loop, provider) pair forever.
    assert not _ADMISSIONS


def test_slots_do_not_leak_across_event_loops():
    reset_provider_limits()

    # A module-level Semaphore would bind to the first loop and blow up (or
    # silently share capacity) in the second.
    assert asyncio.run(_peak_inflight("codex_cli", 2, 4)) == 2
    assert asyncio.run(_peak_inflight("codex_cli", 2, 4)) == 2
    assert not _ADMISSIONS


def test_resolve_limit_precedence(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        cli_max_concurrency=3,
        openrouter_max_concurrency=9,
        provider_max_concurrency={"codex_cli": 1, "openrouter": 12},
    )

    assert resolve_provider_limit(settings, "codex_cli") == 1  # map wins
    assert resolve_provider_limit(settings, "openrouter") == 12
    assert resolve_provider_limit(settings, "claude_cli") == 3  # class default
    assert resolve_provider_limit(settings, "") == 0


def test_resolve_limit_accepts_the_bare_provider_key(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", provider_max_concurrency={"codex": 1})

    assert resolve_provider_limit(settings, "codex_cli") == 1


def test_resolve_limit_never_raises_on_junk():
    junk = SimpleNamespace(provider_max_concurrency={"codex_cli": "lots"})

    # Falls through to the class default rather than exploding mid-build.
    assert resolve_provider_limit(junk, "codex_cli") == 2
    assert resolve_provider_limit(SimpleNamespace(), "openrouter") == 8
