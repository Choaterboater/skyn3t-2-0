"""Deterministic agent-workflow gate (wave-2 §3.2) — the proof story for the
``workflow`` app type, with ZERO LLM and ZERO live delivery.

A workflow app is an HTTP multi-step runner: a user triggers a workflow and
inspects its run in the ledger. This gate is that user, mechanically: it boots
the delivered ``main.py`` on a free port (with WEBHOOK_URL and every LLM seam
scrubbed, so delivery is deterministically UNCONFIGURED) and drives the spec's
contract —

  GET /health  →  GET /v1/stats (must list the app's workflow names)  →
  POST /trigger {dry_run: true} (→ status done, delivery.status "dry_run",
  the {run_id, dry_run, status, brief, delivery} envelope)  →  POST /trigger
  {dry_run: false} with NO delivery configured (→ "skipped_no_delivery",
  NEVER a crash — the spec's optional-delivery rule)  →  GET /runs (the
  ledger recorded BOTH runs, append-only)  →  one unknown-workflow trigger
  (must yield a structured 4xx, not a 5xx and not a silent 2xx).

Design contract — mirrors :mod:`skyn3t.studio.rag_check`, and deliberately
REUSES its HTTP-gate plumbing (spawn/drain/await/probe helpers + the verdict
shape) rather than duplicating it; if a THIRD HTTP gate appears, extract the
shared module then. Same guarantees: never hangs a build (every wait is
deadline-bounded, child pipes drained), never raises, soft-skips when it
cannot run (non-workflow stack, no main.py, no runtime, missing third-party
deps), and records ISSUES (gaps feed ONE repair) only for real defects.
ADVISORY: the runner never flips the verdict from this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from skyn3t.studio.rag_check import (
    RagVerdict,
    _await_health,
    _free_port,
    _http,
    _json_dict,
    _PipeDrain,
    _python_exec,
    _shutdown,
    _spawn_server,
)

# The verdict shape is gate-agnostic ({skipped, issues, checked, reason, gaps});
# alias it so workflow call sites read naturally.
WorkflowVerdict = RagVerdict

_BOOT_TIMEOUT = 25.0
_HTTP_TIMEOUT = 10.0


def _int_field(payload: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        val = payload.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            return val
        if isinstance(val, float) and val.is_integer():
            return int(val)
    return None


def check_workflow(
    project_dir: str | Path,
    stack: str = "",
    *,
    python_exec: str | None = None,
    main_rel: str = "main.py",
    boot_timeout: float = _BOOT_TIMEOUT,
    http_timeout: float = _HTTP_TIMEOUT,
) -> WorkflowVerdict:
    """Boot the delivered workflow app and drive its /trigger contract. Returns
    an advisory :class:`WorkflowVerdict`. Never raises; never hangs a build;
    soft-skips when it cannot run (see the module docstring)."""
    try:
        s = (stack or "").strip().lower()
        if s and s != "workflow":
            return WorkflowVerdict(
                skipped=True, reason=f"stack '{stack}' is not a workflow stack")
        root = Path(project_dir)
        if not root.is_dir():
            return WorkflowVerdict(skipped=True, reason="project dir is not a directory")
        main_py = root / main_rel
        if not main_py.is_file():
            return WorkflowVerdict(skipped=True, reason=f"no {main_rel} to run")
        py = _python_exec(python_exec)
        if not py:
            return WorkflowVerdict(skipped=True, reason="no python interpreter available")
        return _probe_server(
            root, main_py.name, py,
            boot_timeout=boot_timeout, http_timeout=http_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - a gate must never break a build
        return WorkflowVerdict(skipped=True, reason=f"workflow check error: {exc}")


def _probe_server(
    root: Path,
    main_name: str,
    py: str,
    *,
    boot_timeout: float,
    http_timeout: float,
) -> WorkflowVerdict:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    # _spawn_server scrubs WEBHOOK_URL + the LLM seams — delivery is
    # deterministically unconfigured for the live-trigger probe below.
    proc, spawn_err = _spawn_server(root, main_name, py, port)
    if proc is None:
        return WorkflowVerdict(skipped=True, reason=f"could not spawn server: {spawn_err}")

    drain = _PipeDrain(proc)
    checked: dict[str, Any] = {"port": port}
    try:
        failed = _await_health(
            proc, drain, base, root, main_name, port, checked,
            boot_timeout=boot_timeout, http_timeout=http_timeout)
        if failed is not None:
            return failed
        checked["booted"] = True
        checked["health"] = "ok"

        issues: list[str] = []

        # 1. /v1/stats — must exist and NAME the app's workflows so a caller
        # (and this gate) can discover what to trigger.
        status, text = _http("GET", f"{base}/v1/stats", timeout=http_timeout)
        stats = _json_dict(text) if status == 200 else None
        baseline = _int_field(stats, ("runs", "run_count", "total_runs")) if stats else None
        workflows = (stats or {}).get("workflows")
        wf_name = ""
        if status != 200 or stats is None:
            issues.append(
                f"GET /v1/stats must return 200 with a JSON object describing the "
                f"runner (e.g. {{\"runs\": 0, \"workflows\": [\"briefing\"]}}); "
                f"got status {status}")
        else:
            if baseline is None:
                issues.append(
                    "GET /v1/stats returned JSON without an integer run-count field — "
                    "include a numeric \"runs\" count")
            if isinstance(workflows, list) and workflows and isinstance(workflows[0], str):
                wf_name = workflows[0]
            else:
                issues.append(
                    "GET /v1/stats must list the registered workflow names under "
                    "\"workflows\" — callers discover what to trigger from it")
        checked["stats_runs_baseline"] = baseline
        checked["workflow_name"] = wf_name

        # Without a discoverable workflow name the trigger probes can't run —
        # the issues above already teach the fix.
        if not wf_name:
            checked["trigger"] = "not_probed"
            return WorkflowVerdict(issues=issues, checked=checked)

        # 2. dry-run trigger: the spec envelope, delivery.status == "dry_run".
        status, text = _http(
            "POST", f"{base}/trigger",
            body={"workflow": wf_name, "dry_run": True},
            timeout=http_timeout,
        )
        payload = _json_dict(text) if status == 200 else None
        if status != 200 or payload is None:
            issues.append(
                f"POST /trigger {{\"workflow\": \"{wf_name}\", \"dry_run\": true}} must "
                f"return 200 with the run envelope; got status {status}: {text[:200]}")
            checked["trigger_dry"] = "failed"
        else:
            missing = [k for k in ("run_id", "dry_run", "status", "brief", "delivery")
                       if k not in payload]
            if missing:
                issues.append(
                    "the /trigger response is missing the contract key(s) "
                    f"{missing} — return {{run_id, dry_run, status, brief, "
                    "delivery: {status}} for every run")
            delivery = payload.get("delivery")
            d_status = delivery.get("status") if isinstance(delivery, dict) else None
            if payload.get("status") == "failed":
                issues.append(
                    f"a dry-run of workflow '{wf_name}' FAILED — a dry run must "
                    f"execute the non-delivery steps successfully: {text[:200]}")
            if d_status != "dry_run":
                issues.append(
                    f"a dry-run trigger must report delivery.status == \"dry_run\" "
                    f"(got {d_status!r}) — delivery steps are SKIPPED under dry_run")
            checked["trigger_dry"] = d_status or "malformed"

        # 3. live trigger with NO delivery configured (env scrubbed): the spec's
        # optional-delivery rule — skipped_no_delivery, never a crash.
        status, text = _http(
            "POST", f"{base}/trigger",
            body={"workflow": wf_name, "dry_run": False},
            timeout=http_timeout,
        )
        payload = _json_dict(text) if status == 200 else None
        if status != 200 or payload is None:
            issues.append(
                f"a LIVE trigger with NO delivery configured must not crash "
                f"(got status {status}) — absent delivery config must yield "
                "delivery.status == \"skipped_no_delivery\"")
            checked["trigger_live"] = "failed"
        else:
            delivery = payload.get("delivery")
            d_status = delivery.get("status") if isinstance(delivery, dict) else None
            if d_status != "skipped_no_delivery":
                issues.append(
                    f"a live trigger with no delivery config must report "
                    f"delivery.status == \"skipped_no_delivery\" (got {d_status!r}) — "
                    "delivery adapters are OPTIONAL env config, never required")
            checked["trigger_live"] = d_status or "malformed"

        # 4. the ledger recorded both runs.
        status, text = _http("GET", f"{base}/runs", timeout=http_timeout)
        payload = _json_dict(text) if status == 200 else None
        runs = payload.get("runs") if payload is not None else None
        if status != 200 or not isinstance(runs, list):
            issues.append(
                f"GET /runs must return 200 with a JSON {{\"runs\": [...]}} ledger; "
                f"got status {status}")
            checked["ledger"] = "malformed"
        elif baseline is not None and len(runs) < baseline + 2:
            issues.append(
                "the run ledger did not record the triggered runs — every /trigger "
                "must append a run record (append-only ledger)")
            checked["ledger"] = f"{len(runs)} runs (expected >= {baseline + 2})"
        else:
            checked["ledger"] = f"{len(runs)} runs"

        # 5. an unknown workflow must be rejected with a structured 4xx.
        status, _text = _http(
            "POST", f"{base}/trigger",
            body={"workflow": "__no_such_workflow__", "dry_run": True},
            timeout=http_timeout,
        )
        checked["unknown_workflow_status"] = status
        if status == -1:
            issues.append(
                "the server stopped responding after an unknown-workflow trigger — "
                "validate the workflow name and return a 4xx")
        elif status >= 500:
            issues.append(
                f"triggering an unknown workflow crashed the handler (status "
                f"{status}) — validate the name and return a structured 4xx")
        elif 200 <= status < 300:
            issues.append(
                "an unknown-workflow trigger was ACCEPTED with a 2xx — the workflow "
                "name must be validated against the registered workflows")

        return WorkflowVerdict(issues=issues, checked=checked)
    finally:
        _shutdown(proc)
