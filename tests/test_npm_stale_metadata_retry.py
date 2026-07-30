"""A stale npm metadata cache failed three proof runs on a working app.

Measured on a delivered Astro site whose package.json declares
``tailwindcss: ^4.1.11``::

    npm error code ETARGET
    npm error notarget No matching version found for tailwindcss@4.3.3.

4.3.3 appears nowhere in the delivered tree — only inside the manifest's own
record of this error. npm resolved the range from stale cached registry
metadata under ``--prefer-offline`` (skyn3t/npm_utils.py sets it) and then
could not fetch the version it had chosen.

Timing confirmed it: the first install that refreshed that cache took 39s, and
every run afterwards took 7s and succeeded. The app was fine throughout; three
fix-loop attempts and the final verdict were all decided by cache state.

An environmental failure like this should self-heal, not sink a build, so a
stale-metadata failure retries once with the offline preference dropped.
"""

from __future__ import annotations

from skyn3t.studio import app_runner

_ETARGET = (
    "npm error code ETARGET\n"
    "npm error notarget No matching version found for tailwindcss@4.3.3.\n"
    "npm error notarget In most cases you or one of your dependencies are requesting\n"
)
_OFFLINE_CMD = ["npm", "install", "--no-audit", "--prefer-offline", "--ignore-scripts"]


def _stub_runs(monkeypatch, results):
    """Feed _npm_run_once a scripted sequence; record the commands it saw."""
    seen: list[list[str]] = []
    seq = list(results)

    def fake(cmd, cwd, timeout):
        seen.append(list(cmd))
        return seq.pop(0)

    monkeypatch.setattr(app_runner, "_npm_run_once", fake)
    return seen


def test_a_stale_metadata_failure_retries_without_prefer_offline(monkeypatch):
    seen = _stub_runs(monkeypatch, [
        (1, _ETARGET, ""),
        (0, "added 376 packages in 39s\n", ""),
    ])

    ok, detail = app_runner._default_npm_run(_OFFLINE_CMD, "/app")

    assert ok is True
    assert detail.get("stale_metadata_retry") is True
    assert len(seen) == 2
    assert "--prefer-offline" in seen[0]
    assert "--prefer-offline" not in seen[1], "the retry must force a metadata refresh"
    # Every other argument is preserved.
    assert "--ignore-scripts" in seen[1] and "--no-audit" in seen[1]


def test_a_genuine_failure_is_not_masked(monkeypatch):
    """The retry must not turn a real dependency error into a pass."""
    seen = _stub_runs(monkeypatch, [
        (1, _ETARGET, ""),
        (1, "npm error 404 Not Found - GET https://registry.npmjs.org/nope\n", ""),
    ])

    ok, detail = app_runner._default_npm_run(_OFFLINE_CMD, "/app")

    assert ok is False
    assert "npm exited 1" in detail["error"]
    assert "404" in detail["log_tail"], "the RETRY's error is what the fix loop needs"
    assert len(seen) == 2


def test_an_unrelated_failure_does_not_retry(monkeypatch):
    """Only stale-metadata markers justify a second network install."""
    seen = _stub_runs(monkeypatch, [
        (1, "npm error EACCES permission denied\n", ""),
    ])

    ok, detail = app_runner._default_npm_run(_OFFLINE_CMD, "/app")

    assert ok is False
    assert len(seen) == 1, "an unrelated failure must not be retried"
    assert "EACCES" in detail["log_tail"]


def test_success_never_retries(monkeypatch):
    seen = _stub_runs(monkeypatch, [(0, "added 376 packages\n", "")])

    ok, detail = app_runner._default_npm_run(_OFFLINE_CMD, "/app")

    assert ok is True
    assert len(seen) == 1
    assert "stale_metadata_retry" not in detail


def test_no_retry_when_prefer_offline_was_not_used(monkeypatch):
    """Without the offline preference a refresh already happened."""
    seen = _stub_runs(monkeypatch, [(1, _ETARGET, "")])

    ok, _ = app_runner._default_npm_run(["npm", "install"], "/app")

    assert ok is False
    assert len(seen) == 1


def test_a_timeout_is_reported_and_not_retried(monkeypatch):
    seen = _stub_runs(monkeypatch, [(None, "", "npm install timed out after 300s")])

    ok, detail = app_runner._default_npm_run(_OFFLINE_CMD, "/app")

    assert ok is False
    assert "timed out" in detail["error"]
    assert len(seen) == 1
