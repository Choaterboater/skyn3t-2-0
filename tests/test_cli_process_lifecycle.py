from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from skyn3t.adapters.llm import LLMClient


def _running(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-session regression")
@pytest.mark.parametrize("detached_tool", [False, True])
@pytest.mark.parametrize("new_cli_session", [False, True])
async def test_cli_termination_stops_owned_tool_children(
    tmp_path: Path, detached_tool: bool, new_cli_session: bool,
) -> None:
    pid_file = tmp_path / "tool.pid"
    wrapper = """
import pathlib
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    start_new_session=sys.argv[2] == "detached",
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        wrapper,
        str(pid_file),
        "detached" if detached_tool else "attached",
        start_new_session=new_cli_session,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    child_pid: int | None = None
    try:
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                child_pid = int(pid_file.read_text())
                break
            await asyncio.sleep(0.02)
        assert child_pid is not None, "fixture tool process did not start"
        assert _running(child_pid)
        await asyncio.wait_for(LLMClient._terminate(proc), timeout=3)
        for _ in range(50):
            if not _running(child_pid):
                break
            await asyncio.sleep(0.02)
        assert not _running(child_pid), "tool process survived CLI termination and can race rollback/retry"
    finally:
        if child_pid is not None and _running(child_pid):
            os.kill(child_pid, signal.SIGKILL)
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
