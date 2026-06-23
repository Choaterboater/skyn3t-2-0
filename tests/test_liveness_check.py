# tests/test_liveness_check.py
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from skyn3t.studio.liveness import Route, check_liveness


def _serve():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            code = 500 if self.path == "/broken" else 200
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):  # silence
            ...

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_check_flags_dead_route():
    srv, base = _serve()
    try:
        report = asyncio.run(check_liveness(base, [Route("/"), Route("/broken")]))
        assert report.total == 2 and report.ok == 1 and report.dead == 1
        assert report.dead_routes == ["/broken"]
        assert report.health == 0.5
    finally:
        srv.shutdown()


def test_unreachable_base_marks_all_dead():
    # Nothing listening -> connection refused -> status 0 -> dead.
    report = asyncio.run(check_liveness("http://127.0.0.1:9", [Route("/"), Route("/x")]))
    assert report.total == 2 and report.ok == 0 and report.dead == 2 and report.health == 0.0


def test_empty_routes_is_healthy():
    report = asyncio.run(check_liveness("http://127.0.0.1:9", []))
    assert report.total == 0 and report.health == 1.0


def test_report_to_dict_roundtrip():
    srv, base = _serve()
    try:
        report = asyncio.run(check_liveness(base, [Route("/")]))
        d = report.to_dict()
        assert d["total"] == 1 and d["results"][0]["path"] == "/" and d["results"][0]["ok"] is True
    finally:
        srv.shutdown()
