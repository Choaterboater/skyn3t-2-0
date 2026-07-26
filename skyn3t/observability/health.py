"""Health registry + doctor checks.

A :class:`HealthRegistry` holds named async checks; ``run`` executes them all
(isolating failures) and returns an aggregate status. ``doctor`` ships a set of
default environment checks (live Docker/Playwright/Maestro readiness, effective
LLM backend, writable dirs, optional deps) so ``skyn3t doctor`` can report
what's available and what will degrade or block required proof. A signed-in
local Codex CLI counts as ready even when no hosted-provider key is configured.

Import is side-effect free; checks run only when invoked.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.stacks import WEB_STACKS
from skyn3t.studio.lab_tools import (
    LabToolchainReport,
    ToolCheck,
    inspect_lab_toolchain,
)


class Status(StrEnum):
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


class _PreparedHealthRegistry(HealthRegistry):
    """Refresh shared doctor inputs once before each registry run."""

    def __init__(self, prepare: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self._prepare = prepare

    async def run(self) -> dict[str, Any]:
        await self._prepare()
        return await super().run()


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def lab_tool_check_result(
    check: ToolCheck,
    *,
    proof_ladder_required: bool,
    stack: str = "",
) -> CheckResult:
    """Translate live tool readiness into the doctor health contract."""
    normalized_stack = str(stack or "").strip().lower()
    effective_required = bool(proof_ladder_required and check.required)
    if effective_required:
        scope = "required for proof ladder"
    elif check.required:
        scope = "not required because proof ladder is disabled"
    else:
        scope = f"not required for stack {normalized_stack or 'default web'}"

    if check.ready:
        status = Status.OK
    elif effective_required:
        status = Status.FAIL
    elif check.required:
        status = Status.DEGRADED
    else:
        status = Status.OK
    detail = f"{scope}; {check.detail}" if check.detail else scope
    return CheckResult(check.name, status, detail)


def lab_tool_unknown_result(
    name: str,
    *,
    proof_ladder_required: bool,
    stack: str = "",
    reason: str,
) -> CheckResult:
    """Classify an unavailable probe without claiming the tool is missing."""
    normalized_stack = str(stack or "").strip().lower()
    is_web = not normalized_stack or normalized_stack in WEB_STACKS
    required_for_stack = (
        (name == "docker" and is_web)
        or (name == "playwright" and is_web)
        or (name == "maestro" and normalized_stack == "react_native")
    )
    classified = lab_tool_check_result(
        ToolCheck(
            name=name,
            installed=False,
            ready=False,
            required=required_for_stack,
        ),
        proof_ladder_required=proof_ladder_required,
        stack=normalized_stack,
    )
    return CheckResult(
        classified.name,
        classified.status,
        f"{classified.detail}; readiness unknown; {reason}",
    )


def doctor_registry(
    settings: Settings | None = None,
    *,
    stack: str = "",
) -> HealthRegistry:
    """Build a registry pre-loaded with default environment doctor checks."""
    s = settings or get_settings()
    normalized_stack = str(stack or "").strip().lower()
    lab_report: LabToolchainReport | None = None
    lab_error = ""

    async def refresh_lab_report() -> None:
        nonlocal lab_error, lab_report
        lab_error = ""
        lab_report = None
        try:
            candidate = await asyncio.to_thread(
                inspect_lab_toolchain,
                stack=normalized_stack,
            )
            if not isinstance(getattr(candidate, "checks", None), dict):
                raise TypeError("toolchain report checks must be a mapping")
            lab_report = candidate
        except Exception as exc:  # noqa: BLE001 - becomes per-tool evidence
            lab_error = f"toolchain inspection failed: {exc}"[:500]

    reg = _PreparedHealthRegistry(refresh_lab_report)

    async def check_lab_tool(name: str) -> CheckResult:
        required = bool(getattr(s, "proof_ladder_required", True))
        report = lab_report
        if lab_error:
            return lab_tool_unknown_result(
                name,
                proof_ladder_required=required,
                stack=normalized_stack,
                reason=lab_error,
            )
        if report is None:
            return lab_tool_unknown_result(
                name,
                proof_ladder_required=required,
                stack=normalized_stack,
                reason="toolchain report unavailable",
            )
        check = report.checks.get(name)
        if check is None:
            return lab_tool_unknown_result(
                name,
                proof_ladder_required=required,
                stack=normalized_stack,
                reason=f"lab toolchain report omitted {name}",
            )
        try:
            return lab_tool_check_result(
                check,
                proof_ladder_required=required,
                stack=normalized_stack,
            )
        except Exception as exc:  # noqa: BLE001 - malformed report stays diagnostic
            return lab_tool_unknown_result(
                name,
                proof_ladder_required=required,
                stack=normalized_stack,
                reason=f"malformed tool check: {exc}",
            )

    async def check_llm() -> CheckResult:
        try:
            from skyn3t.adapters.llm import LLMClient

            def inspect_backend() -> tuple[str, dict[str, Any]]:
                client = LLMClient(s)
                status = client.backend_status()
                return str(status.get("active") or client.backend), status

            active, backend_status = await asyncio.to_thread(inspect_backend)
            requested = str(
                backend_status.get("requested")
                or getattr(s, "llm_backend", "auto")
            )
            state = str(backend_status.get("state") or "ready")
            detail = f"{active} (requested {requested}, {state})"
            reason = str(backend_status.get("reason") or "").strip()
            if reason:
                detail = f"{detail}; {reason}"
            if active != "stub" and state == "ready":
                return CheckResult("llm", Status.OK, detail)
            return CheckResult("llm", Status.DEGRADED, detail)
        except Exception:
            # Narrow settings doubles and partially configured installations
            # predate runtime-aware status. Preserve their old diagnostic
            # contract without letting a health check crash the registry.
            if bool(getattr(s, "has_any_llm", False)):
                return CheckResult(
                    "llm",
                    Status.OK,
                    "provider key configured; runtime backend status unavailable",
                )
            return CheckResult(
                "llm",
                Status.DEGRADED,
                "no usable runtime backend detected; offline stub available",
            )

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

    for tool_name in ("docker", "playwright", "maestro"):
        async def check(name: str = tool_name) -> CheckResult:
            return await check_lab_tool(name)

        reg.register(tool_name, check)
    reg.register("llm", check_llm)
    reg.register("dirs", check_dirs)
    reg.register("optional_deps", check_optional_deps)
    return reg


def _writable(path) -> bool:
    import os
    return os.access(str(path), os.W_OK)


async def doctor(
    settings: Settings | None = None,
    *,
    stack: str = "",
) -> dict[str, Any]:
    """Convenience: run the default doctor checks and return the report."""
    return await doctor_registry(settings, stack=stack).run()
