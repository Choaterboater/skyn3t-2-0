"""Adapters for external models, services, and runtimes."""

from typing import Any

__all__ = ["LLMClient", "ReplicateClient", "TypeSafeAdapter"]


def __getattr__(name: str) -> Any:
    if name == "LLMClient":
        from skyn3t.adapters.llm import LLMClient
        return LLMClient
    if name == "ReplicateClient":
        from skyn3t.adapters.replicate import ReplicateClient
        return ReplicateClient
    if name == "TypeSafeAdapter":
        from skyn3t.adapters.typesafe import TypeSafeAdapter
        return TypeSafeAdapter
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

