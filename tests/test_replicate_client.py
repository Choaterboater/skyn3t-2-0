"""ReplicateClient — offline, all HTTP mocked. Verifies the degrade-don't-crash
discipline: token presence gates availability; a successful prediction yields
image bytes; no-token / failed / timed-out predictions return [] without raising.
"""

from __future__ import annotations

import asyncio

import skyn3t.adapters.replicate as rep_mod
from skyn3t.adapters.replicate import DEFAULT_MODEL, ReplicateClient, coloring_prompt
from skyn3t.config.settings import Settings

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _client(**kw) -> ReplicateClient:
    return ReplicateClient(Settings(**kw))


# ---- availability ----------------------------------------------------------
def test_available_reflects_token():
    assert _client(replicate_api_token="r8_x").available is True
    assert _client(replicate_api_token="").available is False


def test_default_model_used_when_unset():
    assert _client(replicate_api_token="r8_x").model == DEFAULT_MODEL


def test_configured_model_overrides_default():
    c = _client(replicate_api_token="r8_x", replicate_model="stability-ai/sdxl")
    assert c.model == "stability-ai/sdxl"


def test_coloring_prompt_mentions_subject():
    p = coloring_prompt("elephant")
    assert "elephant" in p and "coloring-book" in p


# ---- HTTP mock harness -----------------------------------------------------
class _Resp:
    def __init__(
        self,
        *,
        json_data=None,
        content=b"",
        status_ok=True,
        status_code=200,
        headers=None,
    ):
        self._json = json_data
        self.content = content
        self._ok = status_ok
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if not self._ok:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._json


def _install_client(monkeypatch, *, create_resp, image_bytes=_PNG, get_resp=None):
    """Patch httpx.AsyncClient so POST returns ``create_resp``, GET (poll) returns
    ``get_resp`` (or ``create_resp`` again), and GET (image) returns the bytes."""

    class _AsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return create_resp

        async def get(self, url, headers=None):
            # A poll hits /predictions/<id>; an image fetch is any other url.
            if url.startswith("https://api.replicate.com/v1/predictions/"):
                return get_resp or create_resp
            return _Resp(content=image_bytes)

    monkeypatch.setattr(rep_mod.httpx, "AsyncClient", _AsyncClient)


async def test_generate_images_success(monkeypatch):
    create = _Resp(json_data={
        "id": "pred1", "status": "succeeded",
        "output": ["https://replicate.delivery/out.png"],
    })
    _install_client(monkeypatch, create_resp=create)
    imgs = await _client(replicate_api_token="r8_x").generate_images("a cat", n=1)
    assert imgs == [_PNG]


async def test_generate_images_n_gt_1_returns_n(monkeypatch):
    # n>1 must work: predictions run concurrently (not serialized past the outer
    # wait_for budget), so two images come back.
    create = _Resp(json_data={
        "id": "pred", "status": "succeeded",
        "output": ["https://replicate.delivery/out.png"],
    })
    _install_client(monkeypatch, create_resp=create)
    imgs = await _client(replicate_api_token="r8_x").generate_images("a cat", n=2)
    assert imgs == [_PNG, _PNG]


async def test_generate_images_polls_until_succeeded(monkeypatch):
    create = _Resp(json_data={"id": "pred2", "status": "processing"})
    done = _Resp(json_data={
        "id": "pred2", "status": "succeeded",
        "output": ["https://replicate.delivery/out.png"],
    })
    monkeypatch.setattr(ReplicateClient, "_poll_interval", 0.0)
    _install_client(monkeypatch, create_resp=create, get_resp=done)
    imgs = await _client(replicate_api_token="r8_x").generate_images("a dog", n=1)
    assert imgs == [_PNG]


async def test_generate_images_honors_rate_limit_reset_and_retries(monkeypatch):
    limited = _Resp(status_ok=False, status_code=429, headers={"ratelimit-reset": "10"})
    created = _Resp(json_data={
        "id": "pred-rate", "status": "succeeded",
        "output": ["https://replicate.delivery/out.png"],
    }, status_code=201)
    posts = [limited, created]
    delays = []

    class _AsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return posts.pop(0)

        async def get(self, url, headers=None):
            return _Resp(content=_PNG)

    async def _sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(rep_mod.httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(rep_mod.asyncio, "sleep", _sleep)

    images = await _client(replicate_api_token="r8_x").generate_images("a course", n=1)

    assert images == [_PNG]
    assert delays == [10.0]


async def test_no_token_returns_empty(monkeypatch):
    # No HTTP should even be attempted; patch to blow up if it is.
    def _boom(*a, **k):
        raise AssertionError("must not call HTTP without a token")

    monkeypatch.setattr(rep_mod.httpx, "AsyncClient", _boom)
    assert await _client(replicate_api_token="").generate_images("x") == []


async def test_failed_prediction_returns_empty(monkeypatch):
    create = _Resp(json_data={"id": "p", "status": "failed", "output": None})
    _install_client(monkeypatch, create_resp=create)
    assert await _client(replicate_api_token="r8_x").generate_images("x", n=1) == []


async def test_http_error_returns_empty_not_raises(monkeypatch):
    class _AsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            raise rep_mod.httpx.ConnectError("network down")

        async def get(self, url, headers=None):
            raise rep_mod.httpx.ConnectError("network down")

    monkeypatch.setattr(rep_mod.httpx, "AsyncClient", _AsyncClient)
    assert await _client(replicate_api_token="r8_x").generate_images("x") == []


async def test_timeout_returns_empty_not_raises(monkeypatch):
    class _AsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            await asyncio.sleep(10)

        async def get(self, url, headers=None):
            await asyncio.sleep(10)

    monkeypatch.setattr(rep_mod.httpx, "AsyncClient", _AsyncClient)
    c = _client(replicate_api_token="r8_x")
    # Tiny deadline so the bounded wait fires fast.
    assert await c.generate_images("x", timeout=0.05) == []


async def test_unreadable_output_image_returns_empty(monkeypatch):
    create = _Resp(json_data={
        "id": "p", "status": "succeeded",
        "output": ["https://replicate.delivery/out.png"],
    })

    class _AsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return create

        async def get(self, url, headers=None):
            raise rep_mod.httpx.ConnectError("image gone")

    monkeypatch.setattr(rep_mod.httpx, "AsyncClient", _AsyncClient)
    assert await _client(replicate_api_token="r8_x").generate_images("x", n=1) == []


def test_output_urls_normalizes_shapes():
    f = ReplicateClient._output_urls
    assert f("https://x/a.png") == ["https://x/a.png"]
    assert f(["https://x/a.png", "https://x/b.png"]) == ["https://x/a.png", "https://x/b.png"]
    assert f({"image": "https://x/a.png"}) == ["https://x/a.png"]
    assert f(None) == []
    assert f("not-a-url") == []
