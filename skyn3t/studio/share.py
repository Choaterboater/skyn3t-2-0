"""Public tunnels for a local preview (``skyn3t studio share``).

The preview itself stays loopback-only (``studio serve`` / PreviewSupervisor);
this module is the explicit opt-in that puts it on the internet. Both providers
are external binaries the user already has — nothing is installed, no account
is provisioned, and no new hosted service is involved:

1. ``cloudflared tunnel --url http://localhost:<port>`` — Cloudflare quick
   tunnel. Free and account-less; the URL is printed in its log output.
2. ``ssh -R 80:localhost:<port> nokey@localhost.run`` — the no-auth
   localhost.run form, using the OpenSSH client that ships with
   Windows 10+/macOS/Linux.

A tunnel failure NEVER fails the underlying preview: the caller keeps serving
locally and reports. Never raises.
"""
from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from skyn3t.studio.app_runner import _kill_group

# cloudflared prints its quick-tunnel URL inside an ASCII box, e.g.
#   |  https://wan-named-pierce-across.trycloudflare.com  |
_CLOUDFLARED_URL_RE = re.compile(r"https://[A-Za-z0-9-]+\.trycloudflare\.com")
# localhost.run prints the forwarding address on a line of its banner, e.g.
#   https://9f8e7d6a5b.lhr.life is now forwarding to localhost:8080
# Older clients emitted <sub>.localhost.run instead of <sub>.lhr.life.
_LOCALHOST_RUN_URL_RE = re.compile(r"https://[A-Za-z0-9-]+\.(?:lhr\.life|localhost\.run)")


@dataclass(frozen=True, slots=True)
class TunnelProvider:
    """One resolved tunnel provider: the command to run + how to read its URL."""

    key: str            # "cloudflared" | "localhost_run"
    label: str          # human-readable, for the CLI output
    argv: tuple[str, ...]
    url_pattern: re.Pattern[str]

    def extract_url(self, text: str) -> str | None:
        """First public URL in ``text``, or None. Never raises."""
        match = self.url_pattern.search(text or "")
        return match.group(0) if match else None


def cloudflared_provider(binary: str, port: int) -> TunnelProvider:
    """Cloudflare quick tunnel: free, no account, URL printed to the log."""
    return TunnelProvider(
        key="cloudflared",
        label="cloudflared (Cloudflare quick tunnel)",
        argv=(binary, "tunnel", "--url", f"http://localhost:{port}"),
        url_pattern=_CLOUDFLARED_URL_RE,
    )


def localhost_run_provider(binary: str, port: int) -> TunnelProvider:
    """localhost.run over the no-auth ``nokey@`` form (OpenSSH client only)."""
    return TunnelProvider(
        key="localhost_run",
        label="localhost.run (ssh)",
        argv=(
            binary,
            "-T",                      # no pseudo-terminal — pure port forward
            "-o", "BatchMode=yes",     # never hang on an interactive prompt
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-R", f"80:localhost:{port}",
            "nokey@localhost.run",
        ),
        url_pattern=_LOCALHOST_RUN_URL_RE,
    )


def detect_provider(
    port: int,
    *,
    which: Callable[[str], str | None] | None = None,
) -> TunnelProvider | None:
    """Pick the best available tunnel provider, cloudflared first.

    ``which`` is looked up at call time (default ``shutil.which``) so tests can
    monkeypatch binary discovery. Returns None when neither binary exists."""
    find = shutil.which if which is None else which
    cloudflared = find("cloudflared")
    if cloudflared:
        return cloudflared_provider(cloudflared, port)
    ssh = find("ssh")
    if ssh:
        return localhost_run_provider(ssh, port)
    return None


def install_hint() -> str:
    """Precise install guidance for the no-provider path."""
    return (
        "Install a tunnel provider (either one works):\n"
        "  cloudflared — free Cloudflare quick tunnel, no account needed:\n"
        "    Windows:  winget install Cloudflare.cloudflared   (or: choco install cloudflared)\n"
        "    macOS:    brew install cloudflared\n"
        "    Linux:    https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n"
        "  ssh — an OpenSSH client (already on Windows 10+/macOS/Linux) enables\n"
        "    the localhost.run fallback automatically."
    )


class PublicTunnel:
    """Own one tunnel subprocess and parse the public URL out of its output.

    Never raises: ``start()`` returns the URL or None (timeout / process died /
    launch error), and a failed start kills the half-open tunnel so nothing
    leaks. ``stop()`` tears down the whole process tree using the same native
    mechanism as the preview runner.
    """

    def __init__(self, provider: TunnelProvider, *, url_timeout: float = 30.0) -> None:
        self.provider = provider
        self.url_timeout = max(1.0, float(url_timeout))
        self.url: str | None = None
        self.output_tail: str = ""
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> str | None:
        """Launch the tunnel and wait for its public URL. None on failure."""
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - binary resolved via shutil.which
                list(self.provider.argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,  # own process group for clean teardown
            )
        except OSError:
            self._proc = None
            return None

        lines: queue.Queue[str | None] = queue.Queue()

        def _pump() -> None:
            proc = self._proc
            try:
                if proc is not None and proc.stdout is not None:
                    for line in proc.stdout:
                        lines.put(line)
            except (OSError, ValueError):  # pipe torn down during stop()
                pass
            finally:
                lines.put(None)

        threading.Thread(target=_pump, daemon=True).start()

        collected: list[str] = []
        deadline = time.monotonic() + self.url_timeout
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if self._proc is not None and self._proc.poll() is not None:
                    break  # died without printing a URL
                continue
            if line is None:
                break  # EOF — the process closed its output
            collected.append(line)
            url = self.provider.extract_url(line)
            if url:
                self.url = url
                self.output_tail = "".join(collected)[-2000:]
                return url
        self.output_tail = "".join(collected)[-2000:]
        # No URL in time — kill the half-open tunnel; the local preview stays.
        self.stop()
        return None

    def stop(self) -> None:
        """Terminate the tunnel process tree. Safe to call twice."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                _kill_group(proc.pid, wait_s=5.0)
            proc.wait(timeout=5.0)
        except (OSError, subprocess.SubprocessError):
            pass
