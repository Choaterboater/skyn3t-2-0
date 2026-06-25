"""`skyn3t studio approve`/`reject` must POST to /api/studio/approve.

The web router is mounted under the `/api` prefix; posting to the bare
`/studio/approve` falls through to the SPA catch-all (GET-only) and returns
405 Method Not Allowed — which silently broke CLI approve/reject. This pins
the corrected URL so it can't regress.
"""
from __future__ import annotations

import httpx

from skyn3t.cli.main import _decide_build


def test_decide_build_posts_to_api_prefixed_route(monkeypatch):
    captured: dict[str, str] = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            return _FakeResp()

    # _decide_build does `import httpx; httpx.AsyncClient(...)` inside, so patching
    # the module attribute is picked up at call time.
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    _decide_build("build-123", approve=True)  # 200 -> succeeds without raising

    assert captured["url"].endswith("/api/studio/approve"), captured["url"]
