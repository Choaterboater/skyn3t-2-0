# tests/test_liveness_crawl.py
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from skyn3t.studio.liveness import Route, _extract_links, crawl_routes, merge_routes


def test_extract_links_keeps_same_origin_paths():
    html = (
        '<a href="/about">About</a>'
        '<a href="/products/1">P</a>'
        '<a href="https://external.example/x">ext</a>'
        '<a href="#anchor">a</a>'
        '<a href="mailto:x@y.z">m</a>'
        '<link rel="stylesheet" href="/style.css">'
        '<script src="/app.js"></script>'
    )
    links = _extract_links(html)
    assert "/about" in links and "/products/1" in links
    assert "https://external.example/x" not in links  # external dropped
    assert "#anchor" not in links and "mailto:x@y.z" not in links
    assert "/style.css" not in links and "/app.js" not in links


def test_extract_links_drops_framework_static_assets():
    html = (
        '<script src="/_next/static/chunks/app.js"></script>'
        '<script src="/@vite/client"></script>'
        '<link href="/assets/app.css" rel="stylesheet">'
        '<a href="/portfolio">Portfolio</a>'
    )
    links = _extract_links(html)
    assert links == {"/portfolio"}


def test_merge_routes_dedupes_by_path_method():
    a = [Route("/"), Route("/about")]
    b = [Route("/about"), Route("/new")]
    merged = merge_routes(a, b)
    paths = sorted(r.path for r in merged)
    assert paths == ["/", "/about", "/new"]


def test_crawl_routes_discovers_linked_pages():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<a href="/about">a</a><a href="/contact">c</a>')

        def log_message(self, *a):
            ...

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        routes = {r.path for r in asyncio.run(crawl_routes(base))}
        assert {"/about", "/contact"} <= routes
    finally:
        srv.shutdown()
