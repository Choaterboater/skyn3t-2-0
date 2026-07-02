"""Deterministic CLI gate — the proof story for python_cli-family deliveries
(the subprocess-tier sibling of qa_playtest the wave-2 §3.6 spec calls for;
the same design later serves swift's compiled binaries).

A CLI's contract is its command surface. This gate drives the delivered
``main.py`` mechanically, with bounded subprocess calls and ZERO side effects:

  ``--help`` must exit 0 with real usage text  →  every SUBCOMMAND the help
  advertises (argparse's ``{a,b,c}`` line) must itself answer ``--help`` with
  exit 0  →  a nonsense invocation must be REJECTED (nonzero exit AND a
  non-empty stderr — a CLI that exits 0 on garbage, or dies silently, fails
  its users).

Design contract — mirrors rag_check/workflow_check:

* **Never hangs a build**: every subprocess call is timeout-bounded; a hung
  CLI surfaces as an issue, never a stuck gate.
* **Never raises**; soft-skips (degrade open, no gaps) when it cannot run —
  a non-CLI stack, no main.py, no runtime, or missing third-party deps
  (``No module named 'X'`` where X is NOT part of the delivery).
* **Records ISSUES** (gaps feed ONE repair) only for real defects. ADVISORY:
  the runner never flips the verdict from this.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from skyn3t.studio.gate_verdict import GateVerdict

CliVerdict = GateVerdict

_CMD_TIMEOUT = 20.0
_MAX_SUBCOMMANDS = 6

_NO_MODULE_RE = re.compile(r"No module named ['\"]([A-Za-z_][\w.]*)['\"]")
# argparse advertises subcommands as a "{a,b,c}" token in usage/help text.
_SUBCOMMANDS_RE = re.compile(r"\{([a-z0-9_,\- ]+)\}")

_NONSENSE = "__definitely_not_a_real_command__"


def _python_exec(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return sys.executable or shutil.which("python") or shutil.which("python3")


def _run(py: str, root: Path, args: list[str], timeout: float) -> tuple[int, str, str]:
    """One bounded CLI invocation. Returns (returncode, stdout, stderr);
    a timeout or spawn failure is (-1, "", reason). Never raises."""
    try:
        proc = subprocess.run(
            [py, "-B", "main.py", *args],
            cwd=str(root), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001 - spawn failure
        return -1, "", str(exc)


def _missing_local_module(err: str, root: Path) -> str | None:
    """The module from a ModuleNotFoundError when it exists IN the delivery —
    the app's own wiring, a real defect. None otherwise (degrade open)."""
    match = _NO_MODULE_RE.search(err or "")
    if not match:
        return None
    mod = match.group(1).split(".")[0]
    try:
        if (root / f"{mod}.py").is_file() or (root / mod / "__init__.py").is_file():
            return mod
    except OSError:
        return None
    return None


def parse_subcommands(help_text: str) -> list[str]:
    """Subcommand names argparse advertises in its help/usage text, deduped in
    order, bounded. Empty for flat (no-subcommand) CLIs."""
    match = _SUBCOMMANDS_RE.search(help_text or "")
    if not match:
        return []
    out: list[str] = []
    for raw in match.group(1).split(","):
        name = raw.strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= _MAX_SUBCOMMANDS:
            break
    return out


def check_cli(
    project_dir: str | Path,
    stack: str = "",
    *,
    python_exec: str | None = None,
    main_rel: str = "main.py",
    cmd_timeout: float = _CMD_TIMEOUT,
) -> CliVerdict:
    """Drive the delivered CLI's command surface. Returns an advisory
    :class:`CliVerdict`. Never raises; never hangs a build; soft-skips when it
    cannot run (see the module docstring)."""
    try:
        s = (stack or "").strip().lower()
        if s and s not in ("python", "cli", "python_cli"):
            return CliVerdict(skipped=True, reason=f"stack '{stack}' is not a cli stack")
        root = Path(project_dir)
        if not root.is_dir():
            return CliVerdict(skipped=True, reason="project dir is not a directory")
        if not (root / main_rel).is_file():
            return CliVerdict(skipped=True, reason=f"no {main_rel} to run")
        py = _python_exec(python_exec)
        if not py:
            return CliVerdict(skipped=True, reason="no python interpreter available")
        return _probe_cli(root, py, cmd_timeout=cmd_timeout)
    except Exception as exc:  # noqa: BLE001 - a gate must never break a build
        return CliVerdict(skipped=True, reason=f"cli check error: {exc}")


def _probe_cli(root: Path, py: str, *, cmd_timeout: float) -> CliVerdict:
    checked: dict[str, Any] = {}
    issues: list[str] = []

    # 1. --help is the front door: exit 0 with real usage text.
    code, out, err = _run(py, root, ["--help"], cmd_timeout)
    if code != 0:
        local = _missing_local_module(err, root)
        if local is not None:
            return CliVerdict(
                issues=[f"main.py failed to import the project's own '{local}' "
                        f"module — fix the import wiring: {err.strip()[-300:]}"],
                checked={"help": "import_error"},
            )
        if _NO_MODULE_RE.search(err):
            return CliVerdict(
                skipped=True,
                reason="third-party deps not importable — gate soft-skipped "
                       "(declared runtime deps; proof env lacks them)")
        issues.append(
            f"`main.py --help` must exit 0 with usage text (got exit {code}): "
            f"{(err or out).strip()[-300:]}")
        return CliVerdict(issues=issues, checked={"help": f"exit {code}"})
    help_text = out + err
    if not help_text.strip():
        issues.append("`main.py --help` exited 0 but printed nothing — users "
                      "discover a CLI through its help text")
    checked["help"] = "ok"

    # 2. every advertised subcommand answers its own --help.
    subcommands = parse_subcommands(help_text)
    checked["subcommands"] = subcommands
    for name in subcommands:
        code, out, err = _run(py, root, [name, "--help"], cmd_timeout)
        if code != 0:
            issues.append(
                f"the help text advertises the '{name}' subcommand but "
                f"`main.py {name} --help` failed (exit {code}): "
                f"{(err or out).strip()[-200:]}")

    # 3. nonsense must be REJECTED: nonzero exit AND a non-empty stderr. Only
    # probed on SUBCOMMAND CLIs — argparse guarantees unknown subcommands are
    # rejected, whereas a flat CLI may legitimately accept any positional
    # value (a greeter, a converter) and exiting 0 would be a FALSE flag.
    if not subcommands:
        checked["nonsense_exit"] = "not_probed_flat_cli"
        return CliVerdict(issues=issues, checked=checked)
    code, out, err = _run(py, root, [_NONSENSE], cmd_timeout)
    checked["nonsense_exit"] = code
    if code == 0:
        issues.append(
            f"`main.py {_NONSENSE}` exited 0 — invalid input must be rejected "
            "with a nonzero exit code and an error on stderr")
    elif code == -1:
        issues.append(
            f"`main.py {_NONSENSE}` hung or could not run ({err.strip()[-200:]}) — "
            "invalid input must fail fast")
    elif not err.strip():
        issues.append(
            f"`main.py {_NONSENSE}` exited {code} but printed nothing to stderr — "
            "errors must be visible, not silent")

    return CliVerdict(issues=issues, checked=checked)
