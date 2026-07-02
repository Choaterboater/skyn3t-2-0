# tests/test_liveness_check.py
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from skyn3t.studio.liveness import Route, check_liveness


def _serve():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            # An API-stack shape: POST-only and required-param routes answer a
            # GET probe with 405/422 (routed = alive), /missing 404s (dead).
            code = {"/broken": 500, "/ingest": 405, "/query": 422,
                    "/missing": 404}.get(self.path, 200)
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


def test_post_only_and_param_routes_count_as_wired():
    # 405 (method not allowed) / 422 (validation ran) prove the framework ROUTED
    # the request — an API app (fastapi/rag/express) must not lose health score
    # because a read-only GET probe can't satisfy its POST/param routes.
    srv, base = _serve()
    try:
        report = asyncio.run(check_liveness(
            base, [Route("/ingest", "POST"), Route("/query"), Route("/missing")]))
        assert report.ok == 2 and report.dead == 1
        assert report.dead_routes == ["/missing"]
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
