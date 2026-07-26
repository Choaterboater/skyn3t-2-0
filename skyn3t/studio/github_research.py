"""Minimal live GitHub transport for clean-room similarity research."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import quote, urlencode

_API_ROOT = "https://api.github.com"
_RAW_ACCEPT = "application/vnd.github.raw+json"
_JSON_ACCEPT = "application/vnd.github+json"
_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "cargo.toml",
        "package.swift",
    }
)
_DOC_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".txt"})


class _ResponseLike(Protocol):
    status_code: int

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...


RequestFn = Callable[..., Awaitable[_ResponseLike]]


class GitHubResearchClient:
    """GitHub API adapter that deliberately never downloads source files."""

    def __init__(
        self,
        *,
        token: str | None = None,
        request: RequestFn | None = None,
        timeout: float = 15.0,
        max_results: int = 16,
    ) -> None:
        self.token = (
            token
            or os.environ.get("SKYN3T_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or ""
        ).strip()
        self._request_override = request
        self.timeout = max(1.0, float(timeout))
        self.max_results = max(1, min(int(max_results), 32))

    @property
    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": _JSON_ACCEPT,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "skyn3t-similarity-scout",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(self, url: str, *, accept: str) -> _ResponseLike:
        if self._request_override is not None:
            return await self._request_override(url, accept=accept)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - core installs httpx
            raise RuntimeError("httpx is unavailable for GitHub research") from exc
        headers = {**self._headers, "Accept": accept}
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            return await client.get(url, headers=headers)

    @staticmethod
    def _error_message(response: _ResponseLike) -> str:
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - diagnostic response may not be JSON
            payload = {}
        if isinstance(payload, Mapping) and payload.get("message"):
            return str(payload["message"])[:240]
        return str(response.text or f"HTTP {response.status_code}")[:240]

    async def _json(self, url: str) -> Any:
        response = await self._request(url, accept=_JSON_ACCEPT)
        if response.status_code != 200:
            raise RuntimeError(
                f"GitHub API {response.status_code}: {self._error_message(response)}"
            )
        return response.json()

    async def _text(self, url: str) -> str:
        response = await self._request(url, accept=_RAW_ACCEPT)
        if response.status_code == 404:
            return ""
        if response.status_code != 200:
            raise RuntimeError(
                f"GitHub API {response.status_code}: {self._error_message(response)}"
            )
        return str(response.text or "")[:50_000]

    async def search_repositories(self, query: str) -> Sequence[Mapping[str, Any]]:
        normalized = " ".join(str(query or "").split())
        if not normalized:
            return []
        params = urlencode(
            {
                "q": f"{normalized} archived:false fork:false",
                "sort": "stars",
                "order": "desc",
                "per_page": self.max_results,
            }
        )
        payload = await self._json(f"{_API_ROOT}/search/repositories?{params}")
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            raise RuntimeError("GitHub search returned an invalid response")
        return [
            dict(item)
            for item in payload["items"][: self.max_results]
            if isinstance(item, Mapping)
        ]

    async def inspect_repository(
        self,
        repository: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        full_name = str(repository.get("full_name") or "").strip().strip("/")
        parts = full_name.split("/")
        if (
            len(parts) != 2
            or any(not part for part in parts)
            or any(not all(ch.isalnum() or ch in "._-" for ch in part) for part in parts)
        ):
            raise ValueError("repository full_name must be owner/name")
        encoded = "/".join(quote(part, safe="._-") for part in parts)
        root = f"{_API_ROOT}/repos/{encoded}"
        result: dict[str, Any] = {"readme": "", "docs": {}, "manifests": {}}
        result["readme"] = await self._text(f"{root}/readme")
        listing = await self._json(f"{root}/contents")
        if not isinstance(listing, list):
            return result

        manifest_names = [
            str(item.get("name") or "")
            for item in listing
            if isinstance(item, Mapping)
            and item.get("type") == "file"
            and str(item.get("name") or "").casefold() in _MANIFEST_NAMES
        ][:5]
        for name in manifest_names:
            raw = await self._text(f"{root}/contents/{quote(name, safe='._-')}")
            if name.casefold() == "package.json":
                try:
                    result["manifests"][name] = json.loads(raw)
                except json.JSONDecodeError:
                    result["manifests"][name] = raw
            else:
                result["manifests"][name] = raw

        has_docs = any(
            isinstance(item, Mapping)
            and item.get("type") == "dir"
            and str(item.get("name") or "").casefold() == "docs"
            for item in listing
        )
        if has_docs:
            docs = await self._json(f"{root}/contents/docs")
            if isinstance(docs, list):
                allowed = [
                    str(item.get("path") or "")
                    for item in docs
                    if isinstance(item, Mapping)
                    and item.get("type") == "file"
                    and any(
                        str(item.get("name") or "").casefold().endswith(ext)
                        for ext in _DOC_EXTENSIONS
                    )
                ][:4]
                for path in allowed:
                    safe_path = "/".join(
                        quote(part, safe="._-")
                        for part in path.split("/")
                        if part not in {"", ".", ".."}
                    )
                    if safe_path:
                        result["docs"][path] = await self._text(
                            f"{root}/contents/{safe_path}"
                        )
        return result
