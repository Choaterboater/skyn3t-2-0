"""API tests for the batched visual-annotations improve endpoint.

POST /api/projects/{slug}/annotations/improve resolves click-to-comment pins
through the visual editor's click-to-source inspection, shapes ONE numbered
improve goal, and dispatches it through the same ``improve_project`` path the
improve UI uses. Improve dispatch is faked here so the tests stay offline; the
wiring (goal text, slug, acceptance) is what is asserted.
"""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from skyn3t.config.settings import Settings  # noqa: E402
from skyn3t.studio.manifest import BuildManifest  # noqa: E402
from skyn3t.web import routes  # noqa: E402
from skyn3t.web.deps import AppState  # noqa: E402

AUTH = {"Authorization": "Bearer secret"}
URL = "/api/projects/demo/annotations/improve"


def _client(tmp_path, monkeypatch, capture, *, manifest_status="completed"):
    projects = tmp_path / "Projects"
    project = projects / "demo"
    project.mkdir(parents=True)
    (project / "index.html").write_text(
        "<html><head></head><body>\n"
        '<h1 class="hero">Old title</h1>\n'
        '<p id="lede">intro copy</p>\n'
        "</body></html>\n"
    )
    BuildManifest(
        slug="demo",
        brief="Demo",
        stack="static",
        status=manifest_status,
        verdict="go",
        score=90.0,
        files=["index.html"],
    ).save(project)
    state = AppState(
        settings=Settings(projects_dir=projects, auth_token="secret"),
    )

    async def fake_improve(state_arg, slug, goal):
        capture["slug"] = slug
        capture["goal"] = goal
        return {
            "accepted": True,
            "slug": slug,
            "goal": goal,
            "correlation_id": "cid123",
        }

    monkeypatch.setattr(routes, "improve_project", fake_improve)
    app = FastAPI()
    app.include_router(routes.build_router(state))
    return TestClient(app)


def _post(client, payload, headers=AUTH):
    return client.post(URL, json=payload, headers=headers)


# ---- batch accepted + goal shaping -----------------------------------------
def test_batch_accepted_and_goal_shaped_with_per_element_evidence(
    tmp_path, monkeypatch
):
    capture = {}
    client = _client(tmp_path, monkeypatch, capture)
    res = _post(
        client,
        {
            "annotations": [
                {
                    "selector": ".hero",
                    "comment": "Make the headline punchier",
                    "signature": {
                        "tag": "h1",
                        "classes": ["hero"],
                        "text": "Old title",
                    },
                },
                {"selector": "#lede", "comment": "Bump the intro font size"},
                {
                    "selector": ".does-not-exist",
                    "comment": "This card needs more padding",
                },
            ]
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["accepted"] is True
    assert body["correlation_id"] == "cid123"
    assert body["annotation_count"] == 3
    assert capture["slug"] == "demo"

    first, second, third = body["annotations"]
    # Signature- and selector-derived pins both resolve via click-to-source.
    assert first["resolved"] is True
    assert first["source"] == {"file": "index.html", "line": 2}
    assert second["resolved"] is True
    assert second["source"] == {"file": "index.html", "line": 3}
    # Unresolvable pins are still accepted, marked source: null.
    assert third["resolved"] is False
    assert third["source"] is None

    goal = capture["goal"]
    assert goal.startswith("Address these 3 visual annotations")
    assert "#1 [index.html:2 · h1.hero] Make the headline punchier" in goal
    assert "#2 [index.html:3 · #lede] Bump the intro font size" in goal
    assert (
        "#3 [.does-not-exist — source unresolved] This card needs more padding"
        in goal
    )


def test_bare_list_body_and_source_hint(tmp_path, monkeypatch):
    capture = {}
    client = _client(tmp_path, monkeypatch, capture)
    res = _post(
        client,
        [
            {
                "selector": ".zzz",
                "comment": "Swap this icon",
                "source_file": "src/app.jsx",
                "source_line": 7,
            }
        ],
    )
    assert res.status_code == 200, res.text
    (entry,) = res.json()["annotations"]
    assert entry["resolved"] is False
    assert entry["source"] is None
    assert entry["hint"] == {"file": "src/app.jsx", "line": 7}
    assert (
        "#1 [.zzz — source unresolved; user-marked hint src/app.jsx:7] Swap this icon"
        in capture["goal"]
    )


def test_screenshot_accepted_within_cap(tmp_path, monkeypatch):
    capture = {}
    client = _client(tmp_path, monkeypatch, capture)
    shot = base64.b64encode(b"\x89png" * 1024).decode()
    res = _post(
        client,
        {"annotations": [{"selector": ".hero", "comment": "see shot",
                          "screenshot_b64": shot}]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["annotations"][0]["screenshot"] is True


# ---- validation caps --------------------------------------------------------
@pytest.mark.parametrize(
    "annotations",
    [
        [],  # empty batch
        [{"selector": ".a", "comment": "x"}] * 21,  # over the max of 20
        [{"selector": ".a", "comment": ""}],  # empty comment
        [{"selector": ".a", "comment": "x" * 2001}],  # comment over the cap
        [{"selector": "<script>alert(1)</script>", "comment": "x"}],  # charset
        [{"comment": "x"}],  # neither selector nor signature
        [{"selector": ".a", "comment": "x", "source_file": "../evil.html"}],
        [{"selector": ".a", "comment": "x", "source_file": "/abs/path.html"}],
        [{"selector": ".a", "comment": "x", "source_line": 0}],
        [{"selector": ".a", "comment": "x", "source_line": "seven"}],
        [
            {
                "selector": ".a",
                "comment": "x",
                "screenshot_b64": base64.b64encode(b"x" * (512 * 1024 + 1)).decode(),
            }
        ],
        [{"selector": ".a", "comment": "x", "screenshot_b64": "not base64!!"}],
    ],
)
def test_validation_caps_enforced(tmp_path, monkeypatch, annotations):
    capture = {}
    client = _client(tmp_path, monkeypatch, capture)
    res = _post(client, {"annotations": annotations})
    assert res.status_code == 422, res.text
    assert "goal" not in capture  # nothing is dispatched on invalid input


# ---- project guards ---------------------------------------------------------
def test_unknown_project_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, {})
    res = client.post(
        "/api/projects/nope/annotations/improve",
        json={"annotations": [{"selector": ".hero", "comment": "hi"}]},
        headers=AUTH,
    )
    assert res.status_code == 404


def test_undelivered_project_409(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, {}, manifest_status="running")
    res = _post(client, {"annotations": [{"selector": ".hero", "comment": "hi"}]})
    assert res.status_code == 409


# ---- auth -------------------------------------------------------------------
def test_auth_required(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, {})
    payload = {"annotations": [{"selector": ".hero", "comment": "hi"}]}
    assert client.post(URL, json=payload).status_code == 401
    assert (
        client.post(URL, json=payload, headers={"Authorization": "Bearer wrong"})
        .status_code
        == 401
    )


# ---- pure goal shaping ------------------------------------------------------
def test_shape_annotations_goal_singular_and_numbering():
    goal = routes.shape_annotations_goal(
        [
            {
                "index": 1,
                "selector": ".hero",
                "element": "h1.hero",
                "comment": "bigger",
                "source": {"file": "index.html", "line": 2},
                "hint": None,
            }
        ]
    )
    assert goal.splitlines()[0].startswith("Address these 1 visual annotation ")
    assert goal.splitlines()[1] == "#1 [index.html:2 · h1.hero] bigger"
