"""Ship pillar — the `deploy_check` gate probes a build at its LIVE url.

check_deploy reuses the liveness HTTP probe against a remote base URL (no local
boot). These tests serve a real local HTTP server to stand in for a live deploy:
a served app verifies; an unreachable url is SKIPPED (could-not-verify never
false-flags a proven build); a reachable-but-broken root is reported as an issue.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import socket
import threading

from skyn3t.studio.deploy_check import check_deploy


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(*, directory=None, handler=None):
    if handler is None:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_check_deploy_verifies_a_served_app(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    httpd, port = _serve(directory=tmp_path)
    try:
        v = asyncio.run(check_deploy(f"http://127.0.0.1:{port}", "static"))
        assert not v.skipped and v.ok
        assert v.checked["root_status"] == 200
    finally:
        httpd.shutdown()


def test_check_deploy_skips_unreachable_url():
    port = _free_port()  # nothing is listening here
    v = asyncio.run(check_deploy(f"http://127.0.0.1:{port}", "fastapi"))
    # Could-not-verify: skipped, and NEVER a gap (no false-flag on a proven build).
    assert v.skipped and v.gaps() == []


def test_check_deploy_no_url_is_skipped():
    v = asyncio.run(check_deploy("", "static"))
    assert v.skipped


def test_check_deploy_flags_a_broken_root():
    class _Broken(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(503)
            self.end_headers()

        def log_message(self, *a):  # silence
            return

    httpd, port = _serve(handler=_Broken)
    try:
        # A UI web stack MUST render at '/', so a 503 root is a real issue.
        v = asyncio.run(check_deploy(f"http://127.0.0.1:{port}", "static"))
        assert not v.skipped and not v.ok
        assert any("503" in issue for issue in v.issues)
        assert v.gaps()  # a reachable-but-broken deploy IS actionable
    finally:
        httpd.shutdown()


def test_check_deploy_allows_an_api_stacks_404_root():
    # API stacks (fastapi/rag/workflow/express) legitimately serve nothing at '/'.
    # A 404 root there must NOT be flagged — the deploy is reachable + verified.
    class _ApiRoot(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            return

    httpd, port = _serve(handler=_ApiRoot)
    try:
        v = asyncio.run(check_deploy(f"http://127.0.0.1:{port}", "fastapi"))
        assert not v.skipped and v.ok  # reachable + no false-flag on the API 404 root
        assert v.checked["root_status"] == 404
    finally:
        httpd.shutdown()


def test_check_deploy_rejects_an_api_stack_500_root_without_routes():
    class _CrashedApiRoot(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(500)
            self.end_headers()

        def log_message(self, *a):
            return

    httpd, port = _serve(handler=_CrashedApiRoot)
    try:
        v = asyncio.run(check_deploy(f"http://127.0.0.1:{port}", "fastapi"))
        assert not v.skipped and not v.ok
        assert v.checked["root_status"] == 500
        assert any("500" in issue for issue in v.issues)
    finally:
        httpd.shutdown()


def test_deploy_check_gate_is_opt_in_by_default():
    from skyn3t.config.settings import Settings

    assert Settings().deploy_check_enabled is False
