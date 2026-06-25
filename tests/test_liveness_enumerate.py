# tests/test_liveness_enumerate.py
from skyn3t.studio.liveness import Route, enumerate_routes


def test_fastapi_routes(tmp_path):
    (tmp_path / "main.py").write_text(
        'from fastapi import FastAPI\napp = FastAPI()\n'
        '@app.get("/")\ndef home(): ...\n'
        '@app.get("/about")\ndef about(): ...\n'
        '@app.post("/api/items")\ndef create(): ...\n')
    routes = {(r.path, r.method) for r in enumerate_routes(tmp_path, "fastapi")}
    assert ("/", "GET") in routes
    assert ("/about", "GET") in routes
    assert ("/api/items", "POST") in routes
    kinds = {r.path: r.kind for r in enumerate_routes(tmp_path, "fastapi")}
    assert kinds["/about"] == "page" and kinds["/api/items"] == "api"


def test_express_routes(tmp_path):
    (tmp_path / "server.js").write_text(
        "const app = express();\napp.get('/', h);\napp.get('/health', h);\n")
    routes = {r.path for r in enumerate_routes(tmp_path, "express")}
    assert {"/", "/health"} <= routes


def test_static_html_files_become_routes(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "about.html").write_text("<h1>about</h1>")
    routes = {r.path for r in enumerate_routes(tmp_path, "static")}
    assert "/" in routes and "/about.html" in routes


def test_react_router_routes(tmp_path):
    (tmp_path / "App.jsx").write_text(
        '<Routes>\n<Route path="/dashboard" element={<D/>} />\n</Routes>\n')
    routes = {r.path for r in enumerate_routes(tmp_path, "react")}
    assert "/dashboard" in routes


def test_always_includes_root_and_dedupes(tmp_path):
    (tmp_path / "main.py").write_text('@app.get("/")\ndef h(): ...\n@app.get("/")\ndef h2(): ...\n')
    paths = [r.path for r in enumerate_routes(tmp_path, "fastapi")]
    assert paths.count("/") == 1


def test_isinstance_route():
    assert Route("/x").kind == "page"
