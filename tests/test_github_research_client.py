from __future__ import annotations

import httpx
import pytest

from skyn3t.studio.github_research import GitHubResearchClient


class _Response:
    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


PINNED_SHA = "a" * 40


async def test_client_searches_and_inspects_only_docs_and_manifests():
    calls: list[tuple[str, str]] = []

    async def request(url: str, *, accept: str):
        calls.append((url, accept))
        if "/search/repositories?" in url:
            return _Response(
                200,
                {
                    "items": [
                        {
                            "full_name": "example/useful-app",
                            "html_url": "https://github.com/example/useful-app",
                        }
                    ]
                },
            )
        if url.endswith("/repos/example/useful-app"):
            return _Response(200, {"full_name": "example/useful-app", "default_branch": "main"})
        if url.endswith("/repos/example/useful-app/commits/main"):
            return _Response(200, {"sha": PINNED_SHA})
        if f"/readme?ref={PINNED_SHA}" in url:
            return _Response(200, text="# Useful App\n## Offline mode")
        if f"/contents?ref={PINNED_SHA}" in url:
            return _Response(
                200,
                [
                    {"type": "file", "name": "package.json"},
                    {"type": "dir", "name": "docs"},
                    {"type": "dir", "name": "src"},
                ],
            )
        if f"/contents/docs?ref={PINNED_SHA}" in url:
            return _Response(
                200,
                [{"type": "file", "name": "architecture.md", "path": "docs/architecture.md"}],
            )
        if f"/contents/package.json?ref={PINNED_SHA}" in url:
            return _Response(200, text='{"dependencies":{"react":"^19"}}')
        if f"/contents/docs/architecture.md?ref={PINNED_SHA}" in url:
            return _Response(200, text="# Architecture\nAdapters first")
        raise AssertionError(f"unexpected request: {url}")

    client = GitHubResearchClient(token="secret", request=request)
    repos = await client.search_repositories("offline dashboard")
    inspected = await client.inspect_repository(repos[0])

    assert repos[0]["full_name"] == "example/useful-app"
    assert inspected["pinned_revision"] == PINNED_SHA
    assert inspected["verified"] is True
    assert inspected["readme"].startswith("# Useful App")
    assert inspected["manifests"]["package.json"]["dependencies"]["react"] == "^19"
    assert inspected["docs"]["docs/architecture.md"].startswith("# Architecture")
    assert all("/src" not in url for url, _accept in calls)
    content_urls = [url for url, _ in calls if "/contents" in url or "/readme" in url]
    assert all(f"?ref={PINNED_SHA}" in url for url in content_urls)


async def test_client_inspect_pin_failure_returns_unverified():
    async def request(url: str, *, accept: str):
        del accept
        if url.endswith("/repos/example/unverified-app"):
            return _Response(200, {"full_name": "example/unverified-app", "default_branch": "main"})
        if url.endswith("/repos/example/unverified-app/commits/main"):
            return _Response(404, {"message": "Not Found"})
        raise AssertionError(f"unexpected request: {url}")

    client = GitHubResearchClient(request=request)
    inspected = await client.inspect_repository({"full_name": "example/unverified-app"})

    assert inspected["pinned_revision"] is None
    assert inspected["verified"] is False
    assert inspected["readme"] == ""
    assert inspected["docs"] == {}
    assert inspected["manifests"] == {}


async def test_client_reports_rate_limits_without_hiding_them():
    async def request(_url: str, *, accept: str):
        del accept
        return _Response(403, {"message": "API rate limit exceeded"})

    client = GitHubResearchClient(request=request)

    try:
        await client.search_repositories("dashboard")
    except RuntimeError as exc:
        assert "rate limit" in str(exc).lower()
    else:
        raise AssertionError("rate-limit response should fail explicitly")


@pytest.mark.parametrize("full_name", [None, 123, {"owner": "acme"}, "acme/../example", "acme/example?ref=main"])
async def test_client_rejects_invalid_canonical_metadata_before_commit_or_content(
    monkeypatch, full_name,
):
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/repos/alias/example":
            return httpx.Response(200, json={"full_name": full_name, "default_branch": "main"})
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": PINNED_SHA})
        if "/readme" in request.url.path:
            return httpx.Response(200, text="# Unverified")
        return httpx.Response(200, json=[])

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
    )

    inspected = await GitHubResearchClient(token="test-token").inspect_repository({
        "full_name": "alias/example", "default_branch": "main", "commit_sha": PINNED_SHA,
    })

    assert inspected["verified"] is False
    assert inspected["pinned_revision"] is None
    assert inspected["error"]
    assert inspected["readme"] == ""
    assert inspected["docs"] == {}
    assert inspected["manifests"] == {}
    assert len(calls) == 1


async def test_client_surfaces_bounded_redacted_commit_failure(monkeypatch):
    token = "synthetic-private-transport-token"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/example":
            return httpx.Response(200, json={"full_name": "acme/example", "default_branch": "main"})
        return httpx.Response(
            403,
            json={"message": f"API rate limit exceeded: {token} " + "x" * 1000},
        )

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
    )

    inspected = await GitHubResearchClient(token=token).inspect_repository({"full_name": "acme/example"})

    assert inspected["verified"] is False
    assert inspected["pinned_revision"] is None
    assert "403" in inspected["error"]
    assert "rate limit" in inspected["error"].lower()
    assert token not in inspected["error"]
    assert len(inspected["error"]) <= 280
