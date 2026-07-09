"""Per-build prompt exposure: the list reports a prompt_count, and a lazy
endpoint returns the captured prompt text from the manifest — so the dashboard
can show 'what did this build actually ask the model?' without bloating the
project list (prompts run 10-50 KB each).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skyn3t.web.routes import get_project_prompts, list_projects


def _state(projects_dir):
    return SimpleNamespace(
        settings=SimpleNamespace(projects_dir=str(projects_dir)),
        preview_signing_key=b"p" * 32,
    )


def _make_project(root, slug, prompts):
    d = root / slug
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(
        json.dumps({
            "slug": slug, "stack": "react", "status": "completed",
            "extra": {"prompts": prompts, "build_cost_usd": 0.1},
        })
    )
    return d


async def test_list_reports_prompt_count_not_the_text(tmp_path):
    _make_project(tmp_path, "app-one",
                  [{"stage": "codegen", "chars": 5, "text": "hello world prompt"}])
    res = await list_projects(_state(tmp_path))
    row = next(p for p in res["projects"] if p["slug"] == "app-one")
    assert row["prompt_count"] == 1
    # the heavy text stays OUT of the list payload
    assert "prompts" not in row


async def test_get_prompts_returns_the_captured_text(tmp_path):
    _make_project(tmp_path, "app-two",
                  [{"stage": "codegen", "chars": 16, "text": "build a todo app"}])
    res = await get_project_prompts(_state(tmp_path), "app-two")
    assert res["slug"] == "app-two"
    assert res["prompts"][0]["text"] == "build a todo app"
    assert res["prompts"][0]["stage"] == "codegen"


async def test_get_prompts_empty_when_none_recorded(tmp_path):
    _make_project(tmp_path, "app-three", [])
    res = await get_project_prompts(_state(tmp_path), "app-three")
    assert res["prompts"] == []


async def test_get_prompts_rejects_path_traversal(tmp_path):
    # the slug is attacker-controllable in the URL — must not escape projects_dir
    with pytest.raises(ValueError):
        await get_project_prompts(_state(tmp_path), "../../etc")
