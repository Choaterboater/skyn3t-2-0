"""Swarm-debug fix: complete() must thread task_type to the router.

It was dead-wired (`self.router.resolve(tier, file_hint)` with no task_type),
so the LearnedModelRouter always queried the empty task bucket and could never
serve a learned pick. complete() now passes task_type, and the base router
tolerates the kwarg.
"""
from __future__ import annotations

import asyncio

from skyn3t.adapters.llm import LLMClient
from skyn3t.config.settings import get_settings


def _stub_client() -> LLMClient:
    settings = get_settings().model_copy(update={"llm_backend": "stub"})
    return LLMClient(settings)


def test_complete_threads_task_type_to_router():
    client = _stub_client()
    captured: dict[str, str] = {}

    def spy(tier, file_hint=None, task_type=""):
        captured["task_type"] = task_type
        return "stub-model"

    client.router.resolve = spy
    asyncio.run(client.complete("hello", task_type="code"))
    assert captured["task_type"] == "code"


def test_base_router_resolve_accepts_task_type():
    client = _stub_client()
    out = client.router.resolve(__import__("skyn3t.core.model_router", fromlist=["Tier"]).Tier.CHEAP,
                                task_type="code")
    assert isinstance(out, str)
