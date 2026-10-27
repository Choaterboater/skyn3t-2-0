"""Internal child-process harness for offline MCP HTTP failure checks.

This replaces supported Python HTTP clients and blocks Python socket/process
operations. It is a diagnostic harness, not an OS security sandbox.
"""

from __future__ import annotations

import io
import json
import os
import runpy
import socket
import sys
import urllib.error
import urllib.request
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from unittest.mock import patch


def _local_socketpair(allowed_connections, family=None, type=socket.SOCK_STREAM, proto=0):
    """Build local IPC while authorizing only its exact internal connection.

    Windows asyncio uses TCP socketpair emulation for its self-pipe. Unlike
    allowing loopback traffic generally, this admits only the helper's client
    socket connecting to the listener it just bound, during that single call.
    Peer authentication mirrors CPython's fallback socketpair implementation.
    """
    family = socket.AF_INET if family is None else family
    if family not in (socket.AF_INET, socket.AF_INET6):
        raise ValueError("Only AF_INET and AF_INET6 socketpairs are supported")
    if type != socket.SOCK_STREAM or proto != 0:
        raise ValueError("Only SOCK_STREAM with protocol zero is supported")
    host = "127.0.0.1" if family == socket.AF_INET else "::1"
    client = socket.socket(family, type, proto)
    peer = None
    try:
        with socket.socket(family, type, proto) as listener:
            listener.bind((host, 0))
            listener.listen()
            address = listener.getsockname()[:2]
            client.setblocking(False)
            allowed_connections[client] = address
            try:
                try:
                    client.connect(address)
                except (BlockingIOError, InterruptedError):
                    pass
            finally:
                allowed_connections.pop(client, None)
            client.setblocking(True)
            peer, _ = listener.accept()
        if peer.getsockname() != client.getpeername() or client.getsockname() != peer.getpeername():
            raise ConnectionError("Unexpected peer connection")
        return peer, client
    except BaseException:
        client.close()
        if peer is not None:
            peer.close()
        raise


def _is_socketpair_connection(event, args, allowed_connections):
    return (
        event == "socket.connect" and len(args) == 2
        and args[0] in allowed_connections
        and args[1] == allowed_connections[args[0]]
    )


def main() -> None:
    server, fault, receipt_name, environment = sys.argv[1:]
    receipt = Path(receipt_name)
    os.environ.update(json.loads(environment))
    status = 401 if fault == "HTTP 401" else 503
    body = b'{"error":"injected_upstream_failure"}'
    hooks = ExitStack()

    def record(kind: str) -> None:
        with receipt.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"kind": kind, "fault": fault}) + "\n")

    try:
        import httpx
    except ModuleNotFoundError as exc:
        if exc.name != "httpx":
            raise
    else:
        def send(self, request, **kwargs):
            record("http")
            if fault == "timeout":
                raise httpx.ConnectTimeout("injected upstream timeout", request=request)
            return httpx.Response(status, content=body, request=request)

        async def send_async(self, request, **kwargs):
            return send(self, request, **kwargs)

        hooks.enter_context(patch.object(httpx.Client, "send", send))
        hooks.enter_context(patch.object(httpx.AsyncClient, "send", send_async))

    try:
        import requests
    except ModuleNotFoundError as exc:
        if exc.name != "requests":
            raise
    else:
        def send_requests(self, request, **kwargs):
            record("http")
            if fault == "timeout":
                raise requests.exceptions.Timeout("injected upstream timeout")
            response = requests.Response()
            response.status_code = status
            response._content = body
            response.request = request
            response.url = request.url
            return response

        hooks.enter_context(patch.object(requests.sessions.Session, "send", send_requests))

    def open_url(self, request, *args, **kwargs):
        record("http")
        if fault == "timeout":
            raise TimeoutError("injected upstream timeout")
        raise urllib.error.HTTPError(
            "https://skyn3t-probe.invalid", status, "injected upstream failure",
            {}, io.BytesIO(body),
        )

    hooks.enter_context(patch.object(urllib.request.OpenerDirector, "open", open_url))

    allowed_connections: dict[socket.socket, tuple[str, int]] = {}
    if socket.socketpair is getattr(socket, "_fallback_socketpair", None):
        hooks.enter_context(patch.object(
            socket, "socketpair", partial(_local_socketpair, allowed_connections),
        ))

    def prevent_external_execution(event: str, args: tuple) -> None:
        if _is_socketpair_connection(event, args, allowed_connections):
            return
        if event in {
            "socket.connect", "socket.getaddrinfo", "socket.sendto",
            "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
        }:
            record("blocked_operation")
            raise OSError("External execution is blocked in the offline MCP HTTP probe")

    sys.addaudithook(prevent_external_execution)
    script = Path(server).resolve()
    sys.path.insert(0, str(script.parent))
    sys.argv = [str(script)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        hooks.close()


if __name__ == "__main__":
    main()
