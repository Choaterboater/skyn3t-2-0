"""Per-provider concurrency admission for LLM dispatch.

Fan-out in this codebase multiplies: ``best_of_n`` (2) x ``parallel_code_slices``
(4) x ``CodeAgent._gen_concurrency`` (4) can put ~32 ``claude -p`` process trees
on one machine at once, and a Mixture-of-Agents council adds another dimension.
A CLI slot is not an HTTP request — it is a Node/Rust agentic process tree with
its own nested tool subprocesses — and subscription accounts rate-limit per
account, so the useful bound is PER PROVIDER, not global.

This is a resource bound, never a gate: it queues work, it never rejects it. A
limit of ``0`` yields immediately, which is the escape hatch for "behave exactly
as before".

Keyed by ``(event loop, provider)`` rather than a module-level semaphore because
an ``asyncio.Semaphore`` binds to the loop that created it, and this repo calls
``asyncio.run()`` from the CLI and from many tests. The refcounted
create-on-demand/tear-down-when-idle shape is copied from the existing house
pattern at :func:`skyn3t.studio.assets._provider_slot`.

Import has zero side effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

#: Concurrent dispatches per provider when nothing else is configured. CLI
#: providers default low (2): ``CodeAgent._gen_concurrency = 4`` already fixed
#: this repo's ceiling for nested ``claude -p`` children, and an advisor fan-out
#: runs alongside other build work. HTTP providers can afford more.
DEFAULT_CLI_MAX_CONCURRENCY = 2
DEFAULT_HTTP_MAX_CONCURRENCY = 8


class _Admission:
    __slots__ = ("semaphore", "users")

    def __init__(self, limit: int) -> None:
        self.semaphore = asyncio.Semaphore(limit)
        self.users = 0


_ADMISSIONS: dict[tuple[asyncio.AbstractEventLoop, str], _Admission] = {}


def reset_provider_limits() -> None:
    """Drop every admission gate. Test hook; never needed in production."""
    _ADMISSIONS.clear()


def resolve_provider_limit(settings: object, provider: str) -> int:
    """Concurrency for one provider from settings. Never raises.

    Precedence: ``provider_max_concurrency`` map entry > the provider-class
    default setting (``cli_max_concurrency`` /
    ``openrouter_max_concurrency``) > the module default.
    """
    name = str(provider or "").strip().lower()
    if not name:
        return 0
    overrides = getattr(settings, "provider_max_concurrency", None)
    if isinstance(overrides, dict):
        for key in (name, name.removesuffix("_cli")):
            if key in overrides:
                try:
                    return max(0, int(overrides[key]))
                except (TypeError, ValueError):  # a malformed entry must not raise
                    break
    if name.endswith("_cli"):
        field, fallback = "cli_max_concurrency", DEFAULT_CLI_MAX_CONCURRENCY
    else:
        field, fallback = "openrouter_max_concurrency", DEFAULT_HTTP_MAX_CONCURRENCY
    try:
        return max(0, int(getattr(settings, field, fallback)))
    except (TypeError, ValueError):
        return fallback


@asynccontextmanager
async def provider_slot(provider: str, limit: int) -> AsyncIterator[None]:
    """Hold one dispatch slot for ``provider``.

    ``limit <= 0`` (or an unnamed provider) yields immediately, so adopting this
    at a call site is a no-op until an operator configures a bound.
    """
    name = str(provider or "").strip().lower()
    if not name or limit <= 0:
        yield
        return
    loop = asyncio.get_running_loop()
    key = (loop, name)
    admission = _ADMISSIONS.get(key)
    if admission is None:
        admission = _Admission(limit)
        _ADMISSIONS[key] = admission
    admission.users += 1
    try:
        async with admission.semaphore:
            yield
    finally:
        admission.users -= 1
        if admission.users <= 0 and _ADMISSIONS.get(key) is admission:
            _ADMISSIONS.pop(key, None)
