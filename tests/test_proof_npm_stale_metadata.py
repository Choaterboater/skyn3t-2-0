"""Bounded npm metadata refresh through the existing proof command context."""

from types import SimpleNamespace

import pytest

from skyn3t.studio import proof_run


def _prepare(monkeypatch, tmp_path, results, *, container=False, manager="npm", existing=False):
    (tmp_path / "package.json").write_text("{}")
    calls = []
    marked = []
    discarded = []
    monkeypatch.setattr(proof_run, "_node_package_manager", lambda *_: (manager, manager))
    monkeypatch.setattr(proof_run, "_node_install_current", lambda *_, **__: False)
    monkeypatch.setattr(proof_run, "discard_foreign_node_modules", lambda *_: "")
    monkeypatch.setattr(proof_run, "_mark_node_install_current", lambda *a, **k: marked.append((a, k)))
    monkeypatch.setattr(proof_run, "_discard_node_modules", lambda p: discarded.append(p))

    def run(ctx, command, **kwargs):
        calls.append((ctx, list(command), kwargs))
        return results.pop(0)

    monkeypatch.setattr(proof_run, "_run_proof_command", run)
    ctx = proof_run._ProofCommandContext(runner=SimpleNamespace(docker_available=container), stack="node", docker_available=container, existing_project=existing)
    outcome = proof_run._prepare_node_dependencies(tmp_path, timeout=37, cmd_ctx=ctx)
    return outcome, calls, marked, discarded


@pytest.mark.parametrize("container", [False, True])
def test_stale_registry_metadata_refresh_preserves_execution_policy(monkeypatch, tmp_path, container):
    outcome, calls, marked, discarded = _prepare(
        monkeypatch, tmp_path,
        [proof_run._ProofCommandResult(1, "", "npm error code ETARGET"),
         proof_run._ProofCommandResult(0, "installed", "")], container=container,
    )
    assert outcome[:2] == (True, True)
    assert len(calls) == 2
    first, retry = calls
    assert first[0] is retry[0]
    assert first[2] == retry[2]
    assert retry[2]["network"] is True
    assert retry[2]["timeout"] == 37
    assert retry[2]["cwd"] == tmp_path
    first_cache = first[1][first[1].index("--cache") + 1]
    retry_cache = retry[1][retry[1].index("--cache") + 1]
    assert retry_cache.startswith(first_cache.rstrip("/\\") + "/.skyn3t-metadata-refresh-")
    assert retry_cache != first_cache
    assert "--prefer-online" in retry[1]
    assert "--prefer-offline" not in retry[1]
    assert "--ignore-scripts" in retry[1]
    assert retry[2]["env"]["npm_config_prefer_offline"] == "true"
    if container:
        assert retry_cache.startswith(proof_run._CONTAINER_NPM_CACHE + "/")
        assert retry[2]["env"]["HOME"] == proof_run._CONTAINER_HOME
    assert len(marked) == 1
    assert not discarded


@pytest.mark.parametrize(
    ("result", "expected_calls"),
    [
        (proof_run._ProofCommandResult(1, "npm error EACCES", ""), 1),
        (proof_run._ProofCommandResult(1, "npm error ERESOLVE", ""), 1),
        (proof_run._ProofCommandResult(1, "npm error ETARGET", "", timed_out=True), 1),
        (proof_run._ProofCommandResult(1, "npm error ETARGET", ""), 2),
    ],
)
def test_dependency_failure_stays_failed_and_never_loops(monkeypatch, tmp_path, result, expected_calls):
    outcome, calls, marked, discarded = _prepare(
        monkeypatch, tmp_path, [result, proof_run._ProofCommandResult(1, "genuine ETARGET", "")],
    )
    assert outcome[:2] == (True, False)
    assert len(calls) == expected_calls
    assert not marked
    assert discarded == [tmp_path]
    if expected_calls == 2:
        assert "genuine ETARGET" in outcome[2]


def test_refresh_timeout_is_reported(monkeypatch, tmp_path):
    outcome, calls, marked, discarded = _prepare(
        monkeypatch, tmp_path,
        [proof_run._ProofCommandResult(1, "ETARGET", ""),
         proof_run._ProofCommandResult(1, "", "", timed_out=True)],
    )
    assert outcome == (True, False, "npm install timed out after 37s")
    assert len(calls) == 2
    assert not marked
    assert discarded == [tmp_path]


@pytest.mark.parametrize("manager", ["pnpm", "yarn"])
def test_non_npm_manager_does_not_receive_npm_retry(monkeypatch, tmp_path, manager):
    outcome, calls, marked, discarded = _prepare(
        monkeypatch, tmp_path,
        [proof_run._ProofCommandResult(1, "ETARGET", "")], manager=manager, existing=True,
    )
    assert outcome[:2] == (True, False)
    assert len(calls) == 1
    assert "--prefer-online" not in calls[0][1]
    assert not marked
    assert discarded == [tmp_path]


def test_existing_npm_refresh_retains_frozen_install_flags(monkeypatch, tmp_path):
    outcome, calls, marked, _ = _prepare(
        monkeypatch, tmp_path,
        [proof_run._ProofCommandResult(1, "ETARGET", ""),
         proof_run._ProofCommandResult(0, "ok", "")], existing=True,
    )
    assert outcome[:2] == (True, True)
    assert len(calls) == 2
    assert "--package-lock=false" in calls[1][1]
    assert "--ignore-scripts" in calls[1][1]
    assert calls[1][2]["env"]["YARN_ENABLE_SCRIPTS"] == "false"
    assert len(marked) == 1
