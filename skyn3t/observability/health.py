"""Health registry + doctor checks.

A :class:`HealthRegistry` holds named async checks; ``run`` executes them all
(isolating failures) and returns an aggregate status. ``doctor`` ships a set of
default environment checks (Docker, LLM keys, writable dirs, optional deps) so
``skyn3t doctor`` can report what's available and what will degrade.

Import is side-effect free; checks run only when invoked.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from skyn3t.config.settings import Settings, get_settings


class Status(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


CheckFn = Callable[[], Awaitable[CheckResult]]


@dataclass
class HealthRegistry:
    checks: dict[str, CheckFn] = field(default_factory=dict)

    def register(self, name: str, fn: CheckFn) -> None:
        self.checks[name] = fn

    async def run(self) -> dict[str, Any]:
        async def _safe(name: str, fn: CheckFn) -> CheckResult:
            try:
                return await fn()
            except Exception as exc:  # noqa: BLE001 - a broken check degrades
                return CheckResult(name, Status.FAIL, f"check raised: {exc}")

        results = await asyncio.gather(
            *(_safe(n, f) for n, f in self.checks.items())
        ) if self.checks else []
        worst = Status.OK
        for r in results:
            if r.status is Status.FAIL:
                worst = Status.FAIL
                break
            if r.status is Status.DEGRADED:
                worst = Status.DEGRADED
        return {
            "status": worst.value,
            "checks": [r.to_dict() for r in results],
        }


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def doctor_registry(settings: Settings | None = None) -> HealthRegistry:
    """Build a registry pre-loaded with default environment doctor checks."""
    s = settings or get_settings()
    reg = HealthRegistry()

    async def check_docker() -> CheckResult:
        if shutil.which("docker"):
            return CheckResult("docker", Status.OK, "docker CLI present")
        return CheckResult(
            "docker", Status.DEGRADED,
            "docker not found; sandbox falls back to hardened subprocess",
        )

    async def check_llm() -> CheckResult:
        if s.has_any_llm:
            return CheckResult("llm", Status.OK, "at least one LLM key configured")
        return CheckResult("llm", Status.DEGRADED, "no LLM key; using offline stub backend")

    async def check_dirs() -> CheckResult:
        bad = [str(p) for p in (s.data_dir, s.logs_dir) if not p.exists() or not _writable(p)]
        if bad:
            return CheckResult("dirs", Status.FAIL, f"not writable: {', '.join(bad)}")
        return CheckResult("dirs", Status.OK, "data/log dirs writable")

    async def check_optional_deps() -> CheckResult:
        optional = (
            "chromadb", "sentence_transformers", "tree_sitter", "fastapi",
            "uvicorn", "docker", "prometheus_client", "playwright",
        )
        missing = [m for m in optional if not _has_module(m)]
        if not missing:
            return CheckResult("optional_deps", Status.OK, "all optional deps present")
        return CheckResult(
            "optional_deps", Status.DEGRADED,
            f"missing (features degrade): {', '.join(missing)}",
        )

    reg.register("docker", check_docker)
    reg.register("llm", check_llm)
    reg.register("dirs", check_dirs)
    reg.register("optional_deps", check_optional_deps)
    return reg


def _writable(path) -> bool:
    import os
    return os.access(str(path), os.W_OK)


async def doctor(settings: Settings | None = None) -> dict[str, Any]:
    """Convenience: run the default doctor checks and return the report."""
    return await doctor_registry(settings).run()
