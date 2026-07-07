from __future__ import annotations

from skyn3t.studio.collaboration import (
    COLLABORATION_FILENAME,
    append_collaboration_event,
    list_collaboration_events,
)
from skyn3t.studio.manifest import BuildManifest


def test_append_collaboration_event_updates_manifest(tmp_path):
    BuildManifest(slug="demo", brief="Build a dashboard", stack="react_vite").save(tmp_path)

    event = append_collaboration_event(
        tmp_path,
        actor="stephen",
        action="note",
        body="Needs a better empty state.",
        metadata={"file": "src/App.jsx"},
    )

    assert event["actor"] == "stephen"
    assert event["action"] == "note"
    assert (tmp_path / COLLABORATION_FILENAME).exists()
    assert list_collaboration_events(tmp_path) == [event]
    manifest = BuildManifest.load(tmp_path)
    assert manifest.extra["collaboration"]["event_count"] == 1
    assert manifest.extra["collaboration"]["latest_action"] == "note"


def test_collaboration_events_are_append_only(tmp_path):
    append_collaboration_event(tmp_path, actor="a", action="comment", body="first")
    append_collaboration_event(tmp_path, actor="b", action="approval", body="ship it")

    events = list_collaboration_events(tmp_path)

    assert [event["action"] for event in events] == ["comment", "approval"]
