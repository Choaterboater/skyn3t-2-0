# tests/test_sandbox_timeout_kill.py
"""A timed-out sandbox ``_exec`` must kill the whole process tree and return.

Pre-fix, the timeout branch called only ``proc.kill()`` (on Windows that
terminates npm.cmd's cmd.exe, not the node grandchildren that inherit the
stdout/stderr pipes) and then awaited ``proc.communicate()`` with NO bound —
blocking until every grandchild closed the pipes. A stalled install or a
watcher hung the entire proof, and the orphans kept node_modules locked so
the NEXT build's npm install failed EPERM/EBUSY.

``_exec`` now tree-kills first (taskkill /PID /T /F on Windows, killpg on
POSIX via start_new_session) and bounds the drain, so a timed-out step
returns promptly with ``timed_out=True`` and no surviving grandchildren.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from skyn3t.config.settings import Settings
from skyn3t.security.sandbox import SandboxRunner

# The sandboxed parent spawns a grandchild that inherits the stdout/stderr
# pipes and sleeps for 60s, records the grandchild's pid in a file (stdout is
# unreliable: the cancelled first communicate() discards partial reads), then
# sleeps past the sandbox timeout itself. Killing only the parent leaves the
# grandchild holding the pipes open (the pre-fix hang) and leaked (the
# pre-fix orphan that kept node_modules locked).
_TREE_SCRIPT = (
    "import pathlib, subprocess, sys, time\n"
    "child = subprocess.Popen("
    "[sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "pathlib.Path('grandchild.pid').write_text(str(child.pid))\n"
    "time.sleep(60)\n"
)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True
    return True


async def test_exec_timeout_kills_grandchildren_and_returns_promptly(tmp_path):
    runner = SandboxRunner(settings=Settings(execution_backend="inline"))
    start = time.monotonic()
    with pytest.warns(RuntimeWarning):  # loud subprocess-fallback warning
        res = await runner.run(
            [sys.executable, "-c", _TREE_SCRIPT], cwd=tmp_path, timeout=3,
        )
    elapsed = time.monotonic() - start

    assert res.timed_out is True
    assert res.ok is False
    # Pre-fix the unbounded communicate() blocked for the grandchild's full
    # 60s sleep; the tree-kill + bounded drain must return well before that.
    assert elapsed < 25, f"timed-out exec took {elapsed:.1f}s — tree not killed"

    # Recover the grandchild and prove it is dead — an orphaned grandchild is
    # what kept node_modules locked and broke the NEXT build's npm install.
    pid_file = tmp_path / "grandchild.pid"
    assert pid_file.exists(), "sandboxed parent never spawned its grandchild"
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(pid), f"grandchild {pid} leaked past the tree-kill"
