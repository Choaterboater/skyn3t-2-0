"""Required local toolchain checks for high-confidence lab builds."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from skyn3t.core.stacks import WEB_STACKS
from skyn3t.exec_paths import find_executable

_MOBILE_STACKS = frozenset({"react_native"})


@dataclass(frozen=True, slots=True)
class ToolCheck:
    name: str
    installed: bool
    ready: bool
    required: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LabToolchainReport:
    stack: str
    checks: dict[str, ToolCheck] = field(default_factory=dict)

    @property
    def missing_required(self) -> list[str]:
        return sorted(
            name for name, check in self.checks.items() if check.required and not check.ready
        )

    @property
    def ready(self) -> bool:
        return not self.missing_required

    def to_dict(self) -> dict[str, Any]:
        return {
            "stack": self.stack,
            "ready": self.ready,
            "missing_required": self.missing_required,
            "checks": {name: check.to_dict() for name, check in self.checks.items()},
        }


def _probe(
    argv: list[str],
    *,
    run: Callable[..., Any],
    timeout: float = 8.0,
) -> tuple[bool, str]:
    try:
        result = run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - doctor must remain diagnostic
        return False, str(exc)[:240]
    output = str(result.stdout or result.stderr or "").strip()
    return result.returncode == 0, output[:240]


def inspect_lab_toolchain(
    *,
    stack: str = "",
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> LabToolchainReport:
    """Inspect Docker, Playwright, and Maestro without installing or mutating."""
    normalized = str(stack or "").strip().lower()
    is_web = not normalized or normalized in WEB_STACKS
    is_mobile = normalized in _MOBILE_STACKS
    checks: dict[str, ToolCheck] = {}

    # Preserve the injectable PATH lookup, then cover Docker Desktop's
    # documented per-user location when this long-lived process has stale PATH.
    docker_path = which("docker") or find_executable("docker")
    docker_ready = False
    docker_detail = "docker CLI not installed"
    if docker_path:
        if is_web:
            docker_ready, docker_detail = _probe(
                [docker_path, "info", "--format", "{{.ServerVersion}}"],
                run=run,
            )
        else:
            docker_ready = True
            docker_detail = "installed; live probe skipped for non-web stack"
        if not docker_detail:
            docker_detail = "daemon ready" if docker_ready else "daemon unavailable"
    checks["docker"] = ToolCheck(
        "docker",
        installed=bool(docker_path),
        ready=docker_ready,
        required=is_web,
        detail=docker_detail,
    )

    playwright_path = which("playwright")
    playwright_ready = False
    playwright_detail = "playwright CLI not installed"
    if playwright_path:
        if is_web:
            playwright_ready, playwright_detail = _probe(
                [playwright_path, "--version"],
                run=run,
            )
        else:
            playwright_ready = True
            playwright_detail = "installed; live probe skipped for non-web stack"
        if not playwright_detail:
            playwright_detail = "CLI ready" if playwright_ready else "CLI failed"
    checks["playwright"] = ToolCheck(
        "playwright",
        installed=bool(playwright_path),
        ready=playwright_ready,
        required=is_web,
        detail=playwright_detail,
    )

    maestro_path = which("maestro")
    maestro_ready = False
    maestro_detail = "maestro CLI not installed"
    if maestro_path:
        if is_mobile:
            maestro_ready, maestro_detail = _probe(
                [maestro_path, "--version"],
                run=run,
            )
        else:
            maestro_ready = True
            maestro_detail = "installed; live probe skipped for non-mobile stack"
        if not maestro_detail:
            maestro_detail = "CLI ready" if maestro_ready else "CLI failed"
    checks["maestro"] = ToolCheck(
        "maestro",
        installed=bool(maestro_path),
        ready=maestro_ready,
        required=is_mobile,
        detail=maestro_detail,
    )
    return LabToolchainReport(stack=normalized, checks=checks)
