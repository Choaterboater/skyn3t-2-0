"""Smoke-test an installed SkyN3t wheel and its bundled dashboard."""

from __future__ import annotations

import os
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

import skyn3t


class _IndexAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        values = dict(attrs)
        for key in ("src", "href"):
            value = values.get(key) or ""
            if value.startswith("/"):
                self.paths.add(value)


def main() -> None:
    workspace = os.environ.get("GITHUB_WORKSPACE")
    module_path = Path(skyn3t.__file__).resolve()
    if workspace:
        assert not module_path.is_relative_to(Path(workspace).resolve()), (
            f"smoke test imported checkout source instead of the wheel: {module_path}"
        )

    with tempfile.TemporaryDirectory(prefix="skyn3t-wheel-smoke-") as temp:
        root = Path(temp)
        os.environ.update(
            SKYN3T_DATA_DIR=str(root / "data"),
            SKYN3T_PROJECTS_DIR=str(root / "projects"),
            SKYN3T_LOGS_DIR=str(root / "logs"),
            SKYN3T_VECTOR_DB_PATH=str(root / "vector_db"),
            SKYN3T_DB_URL=f"sqlite+aiosqlite:///{(root / 'data' / 'skyn3t.db').as_posix()}",
        )

        from skyn3t.config.settings import get_settings
        from skyn3t.web.app import create_app

        get_settings.cache_clear()
        with TestClient(create_app(settings=get_settings())) as client:
            index = client.get("/")
            assert index.status_code == 200
            assert "default-src 'self'" in index.headers["content-security-policy"]

            parser = _IndexAssets()
            parser.feed(index.text)
            assert parser.paths, "dashboard index did not reference any bundled assets"
            for path in sorted(parser.paths):
                response = client.get(path)
                assert response.status_code == 200, (path, response.status_code)

            deep_link = client.get("/overview")
            assert deep_link.status_code == 200 and "SkyN3t" in deep_link.text
            notices = client.get("/THIRD_PARTY_NOTICES.txt")
            assert notices.status_code == 200 and "Packages:" in notices.text
            assert client.get("/fonts/OFL-1.1.txt").status_code == 200
            assert client.get("/api/definitely-missing").status_code == 404

    print(f"PASS installed wheel smoke: {module_path}; {len(parser.paths)} index assets")


if __name__ == "__main__":
    main()
