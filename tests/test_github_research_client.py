from __future__ import annotations

from skyn3t.studio.github_research import GitHubResearchClient


class _Response:
    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


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
        if url.endswith("/readme"):
            return _Response(200, text="# Useful App\n## Offline mode")
        if url.endswith("/contents"):
            return _Response(
                200,
                [
                    {"type": "file", "name": "package.json"},
                    {"type": "dir", "name": "docs"},
                    {"type": "dir", "name": "src"},
                ],
            )
        if url.endswith("/contents/docs"):
            return _Response(
                200,
                [{"type": "file", "name": "architecture.md", "path": "docs/architecture.md"}],
            )
        if url.endswith("/contents/package.json"):
            return _Response(200, text='{"dependencies":{"react":"^19"}}')
        if url.endswith("/contents/docs/architecture.md"):
            return _Response(200, text="# Architecture\nAdapters first")
        raise AssertionError(f"unexpected request: {url}")

    client = GitHubResearchClient(token="secret", request=request)
    repos = await client.search_repositories("offline dashboard")
    inspected = await client.inspect_repository(repos[0])

    assert repos[0]["full_name"] == "example/useful-app"
    assert inspected["readme"].startswith("# Useful App")
    assert inspected["manifests"]["package.json"]["dependencies"]["react"] == "^19"
    assert inspected["docs"]["docs/architecture.md"].startswith("# Architecture")
    assert all("/src" not in url for url, _accept in calls)


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
