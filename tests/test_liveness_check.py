# tests/test_liveness_check.py
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from skyn3t.studio.liveness import Route, check_liveness, enumerate_routes


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


def test_static_route_discovery_excludes_fragments_but_keeps_real_pages(tmp_path):
    (tmp_path / "index.html").write_text("<main>Home</main>", encoding="utf-8")
    (tmp_path / "about.html").write_text("<main>About</main>", encoding="utf-8")
    includes = tmp_path / "includes"
    includes.mkdir()
    (includes / "footer.html").write_text("<footer>Partial</footer>", encoding="utf-8")

    paths = {route.path for route in enumerate_routes(tmp_path, "static")}

    assert paths == {"/", "/about.html"}


def test_crawled_manifest_link_is_not_a_page_route():
    from skyn3t.studio.liveness import _extract_links

    links = _extract_links(
        '<link rel="manifest" href="/site.webmanifest"><a href="/about.html">About</a>'
    )

    assert links == {"/about.html"}


def test_report_to_dict_roundtrip():
    srv, base = _serve()
    try:
        report = asyncio.run(check_liveness(base, [Route("/")]))
        d = report.to_dict()
        assert d["total"] == 1 and d["results"][0]["path"] == "/" and d["results"][0]["ok"] is True
    finally:
        srv.shutdown()


def test_visual_failures_are_reported_in_health(tmp_path, monkeypatch):
    from skyn3t.studio import visual_check, visual_proof
    from skyn3t.studio.visual_proof import ResponsiveVisualProof, ViewportProof

    def fake_responsive(pages, artifact_dir, **kwargs):
        return [ResponsiveVisualProof(
            url=pages[0][1],
            route=pages[0][0],
            stack="",
            passed=True,
            viewports=[ViewportProof(
                "desktop", 1440, 900, passed=True, screenshot="root/desktop.png",
            )],
        )]

    monkeypatch.setattr(visual_proof, "audit_responsive_pages", fake_responsive)
    monkeypatch.setattr(
        visual_check,
        "inspect",
        lambda *a, **k: type("Verdict", (), {
            "skipped": False,
            "matches": False,
            "issues": ["stuck loading"],
        })(),
    )
    srv, base = _serve()
    try:
        report = asyncio.run(check_liveness(
            base, [Route("/")], vision_fn=lambda *_a, **_k: None,
            screenshot_dir=str(tmp_path)))
        assert report.health == 1.0
        assert report.visual_health == 0.0
        assert report.visual_failed_routes == ["/"]
        d = report.to_dict()
        assert d["visual_failed"] == 1
    finally:
        srv.shutdown()


def test_deterministic_visual_proof_runs_without_vision_provider(tmp_path, monkeypatch):
    from skyn3t.studio import visual_proof
    from skyn3t.studio.visual_proof import ResponsiveVisualProof, ViewportProof

    def fake_responsive(pages, artifact_dir, **kwargs):
        return [ResponsiveVisualProof(
            url=pages[0][1],
            route=pages[0][0],
            stack=kwargs["stack"],
            passed=True,
            viewports=[
                ViewportProof("desktop", 1440, 900, passed=True),
                ViewportProof("mobile", 390, 844, passed=True),
            ],
        )]

    monkeypatch.setattr(visual_proof, "audit_responsive_pages", fake_responsive)
    srv, base = _serve()
    try:
        report = asyncio.run(check_liveness(
            base,
            [Route("/")],
            screenshot_dir=str(tmp_path),
            artifact_dir_label=".skyn3t/visual-proof",
            stack="react",
        ))
    finally:
        srv.shutdown()

    assert report.visual_total == 1
    assert report.visual_failed == 0
    assert report.visual_skipped == 0
    assert report.visual_health == 1.0
    assert report.results[0].visual["matches"] is True
    assert report.results[0].visual["vision"]["skipped"] is True
    report_json = (tmp_path / "liveness-report.json")
    assert report_json.is_file()
    assert '"visual_artifact_dir": ".skyn3t/visual-proof"' in report_json.read_text()


def test_missing_browser_visual_evidence_is_skipped_not_passed(tmp_path, monkeypatch):
    from skyn3t.studio import visual_proof
    from skyn3t.studio.visual_proof import ResponsiveVisualProof, ViewportProof

    def fake_responsive(pages, artifact_dir, **kwargs):
        return [ResponsiveVisualProof(
            url=pages[0][1],
            route=pages[0][0],
            stack=kwargs["stack"],
            passed=False,
            skipped=True,
            reason="playwright chromium unavailable",
            viewports=[ViewportProof(
                "desktop", 1440, 900, skipped=True,
                reason="playwright chromium unavailable",
            )],
        )]

    monkeypatch.setattr(visual_proof, "audit_responsive_pages", fake_responsive)
    srv, base = _serve()
    try:
        report = asyncio.run(check_liveness(
            base,
            [Route("/")],
            screenshot_dir=str(tmp_path),
            stack="react",
        ))
    finally:
        srv.shutdown()

    assert report.visual_total == 0
    assert report.visual_failed == 0
    assert report.visual_skipped == 1
    assert report.visual_health is None
    assert report.results[0].visual["matches"] is None
    assert report.results[0].visual["skipped"] is True
    serialized = report.to_dict()
    assert serialized["visual_health"] is None
    assert serialized["visual_skipped_routes"] == ["/"]
