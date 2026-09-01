"""Execution broker — the hardened entry point for running real toolchain
commands (Python import smoke-tests, npm/pip/swift install+build) against a
generated project tree.

:class:`~skyn3t.agents.boot_verifier.BootVerifierAgent` and
:class:`~skyn3t.agents.build_verifier.BuildVerifierAgent` used to do this with
raw ``asyncio.create_subprocess_exec`` calls: the full, unscrubbed host
environment (secrets included), no network isolation, and no audit trail —
exactly the gap flagged in ``docs/reports/2026-07-06-skyn3t-assessment.md``
(finding #16): the sandbox + audit substrate already existed but the verify
agents never used it, unlike ``studio/proof_run.py``, which already routes its
own generated-app proof commands through
:class:`~skyn3t.security.sandbox.SandboxRunner`.

``ExecutionBroker`` composes the EXISTING security substrate rather than
adding a parallel one:

* :class:`~skyn3t.security.sandbox.SandboxRunner` — Docker (or a loudly
  degraded hardened-local-subprocess fallback) execution, dropped
  capabilities, no network by default, resource limits.
* :class:`~skyn3t.security.secrets.SecretsStore` — env + text scrubbing so no
  host credential crosses into the sandboxed process or the audit trail.
* :class:`~skyn3t.security.audit.AuditLog` — every execution becomes a
  hash-chained JSONL record (today only "permission"/"approval" events were
  ever recorded; sandbox execs were invisible).

Deliberately NOT wired: :class:`~skyn3t.security.permissions.PermissionManager`
approval gating. Boot/build verification is a mandatory, deterministic
pipeline stage — every build runs it, unconditionally — not an autonomous
agent decision, and a broker built fresh per call has no human-in-the-loop
backstop. Routing it through ``PermissionManager.classify()`` would resolve
any "needs_approval" outcome via the fire-and-forget deny-by-default
``approval_fn`` and could silently and permanently break verification for any
deployment that sets ``cortex_auto_approve_safe=False``. Sandboxing + the
audit trail is the right control for this call site; gate genuinely dangerous
*autonomous* actions (deploys, spends, force-pushes) through
``PermissionManager`` instead.

Import has zero side effects; nothing runs until :meth:`run_generated_code`.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from skyn3t.config.settings import Settings, get_settings
from skyn3t.security.audit import AuditLog
from skyn3t.security.sandbox import SandboxRunner
from skyn3t.security.secrets import SecretsStore


class SecurityProfile(StrEnum):
    """Execution posture requested by the caller.

    Only ``hardened`` exists today: sandboxed (Docker, or a loudly-degraded
    local-subprocess fallback when Docker is unavailable), dropped
    capabilities, resource limits, secret-scrubbed environment, audit-logged.
    Kept as an enum (not a bare bool) so a stricter or looser posture can be
    added later without callers branching on a string.
    """

    hardened = "hardened"


class Disposition(StrEnum):
    EXECUTED = "executed"  # ran to completion with a zero exit code
    FAILED = "failed"  # ran (or timed out) with a non-zero outcome
    ERROR = "error"  # the broker itself could not attempt the run


@dataclass(frozen=True)
class ExecutionReceipt:
    """Outcome of one :meth:`ExecutionBroker.run_generated_code` call."""

    disposition: Disposition
    exit_code: int | None
    stdout: str
    stderr: str
    backend: str
    duration_ms: float
    timed_out: bool = False
    warning: str = ""

    @property
    def ok(self) -> bool:
        return self.disposition == Disposition.EXECUTED

    @property
    def text(self) -> str:
        """Combined stdout+stderr, uncapped — callers truncate for display.

        ``SandboxResult`` captures the two streams separately; the raw
        subprocess helpers this replaces merged them (``stderr=STDOUT``).
        Concatenation is an adequate substitute for tail-truncated
        diagnostics (stream ordering was never guaranteed either way).
        """
        return f"{self.stdout}{self.stderr}"


_DEFAULT_TIMEOUT = 120.0


@dataclass
class ExecutionBroker:
    """Runs one command for a verify-stage agent under a security profile.

    One instance per call is intentional and cheap: :class:`SandboxRunner`
    only caches the Docker-availability probe per instance, and
    :class:`AuditLog` re-derives its hash-chain tail lazily on first write.
    Built from the CALLER's own ``settings`` (never the process-global
    singleton) so isolated/test settings (see ``studio.golden_bench``) are
    honoured instead of silently falling back to the real environment.
    """

    settings: Settings = field(default_factory=get_settings)
    secrets: SecretsStore = field(init=False)
    audit_log: AuditLog = field(init=False)
    sandbox: SandboxRunner = field(init=False)

    def __post_init__(self) -> None:
        self.secrets = SecretsStore(settings=self.settings)
        self.audit_log = AuditLog(
            path=self.settings.logs_dir / "audit.jsonl", secrets=self.secrets,
        )
        self.sandbox = SandboxRunner(settings=self.settings, secrets=self.secrets)

    def run_generated_code(
        self,
        spec: str,
        cwd: str,
        *,
        profile: SecurityProfile = SecurityProfile.hardened,
        secrets: dict[str, str] | None = None,
        network: bool = False,
        timeout: float = _DEFAULT_TIMEOUT,
        stack: str | None = None,
        actor: str = "system",
    ) -> ExecutionReceipt:
        """Run ``spec`` — a shell command line, e.g. ``shlex.join(argv)`` or
        ``shlex.join([python, "-c", code])`` — against ``cwd``.

        Synchronous by design: both call sites are async agents that already
        offload this through ``asyncio.to_thread`` (``SandboxRunner`` itself
        is async), so the broker drives its own event loop internally rather
        than asking every caller to. ``secrets`` — the name inherited from
        the two call sites — is a dict of EXTRA environment variables merged
        on top of the (scrubbed) host environment, e.g.
        ``PYTHONDONTWRITEBYTECODE``; real credentials never need to be passed
        explicitly since :func:`~skyn3t.security.secrets.filter_env` strips
        anything secret-shaped before it crosses into the sandbox regardless
        of source. Never raises — a broker-side failure is reported as
        ``Disposition.ERROR``, never propagated.
        """
        del profile  # only one profile exists today; kept for call-site stability
        env = {**os.environ, **(secrets or {})}
        try:
            result = asyncio.run(
                self.sandbox.run(
                    spec, cwd=Path(cwd), timeout=timeout,
                    stack=stack, env=env, network=network,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a broker bug must degrade, not crash the caller
            receipt = ExecutionReceipt(
                disposition=Disposition.ERROR, exit_code=None, stdout="", stderr="",
                backend="", duration_ms=0.0, warning=f"execution broker error: {exc}",
            )
            self._audit(spec, cwd=cwd, actor=actor, receipt=receipt)
            return receipt

        receipt = ExecutionReceipt(
            disposition=Disposition.EXECUTED if result.ok else Disposition.FAILED,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            backend=result.backend,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            warning=result.warning or "",
        )
        self._audit(spec, cwd=cwd, actor=actor, receipt=receipt)
        return receipt

    def _audit(self, spec: str, *, cwd: str, actor: str, receipt: ExecutionReceipt) -> None:
        try:
            self.audit_log.record(
                "execute_sandboxed",
                actor=actor,
                outcome=str(receipt.disposition),
                detail={
                    "cwd": cwd,
                    "spec": spec,
                    "backend": receipt.backend,
                    "exit_code": receipt.exit_code,
                    "duration_ms": round(receipt.duration_ms, 1),
                    "timed_out": receipt.timed_out,
                },
            )
        except Exception:  # noqa: BLE001 - auditing must never break execution
            pass
