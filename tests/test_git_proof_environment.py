from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from skyn3t.security.secrets import filter_env


def _git_config(*entries: tuple[str, str]) -> dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def test_filter_env_preserves_complete_git_safety_overrides():
    env = _git_config(
        ("safe.bareRepository", "explicit"),
        ("credential.interactive", "never"),
        ("core.fsmonitor", "false"),
    )

    assert filter_env(env) == env
    assert filter_env(filter_env(env)) == env


def test_filter_env_removes_sensitive_git_entries_and_reindexes():
    env = _git_config(
        ("http.extraHeader", "Authorization: Basic Zml4dHVyZTpub3QtYS1jcmVkZW50aWFs"),
        ("safe.bareRepository", "explicit"),
        ("credential.helper", "fixture-secret-helper"),
        ("core.fsmonitor", "false"),
    )

    assert filter_env(env) == _git_config(
        ("safe.bareRepository", "explicit"),
        ("core.fsmonitor", "false"),
    )


def test_filter_env_does_not_forward_orphaned_git_config_values():
    assert filter_env({
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic fixture",
        "PATH": "/usr/bin",
    }) == {"PATH": "/usr/bin"}


def test_filter_env_blocks_git_entry_as_a_pair():
    env = _git_config(
        ("safe.bareRepository", "explicit"),
        ("core.fsmonitor", "false"),
    )

    assert filter_env(env, extra_block=("GIT_CONFIG_VALUE_1",)) == _git_config(
        ("safe.bareRepository", "explicit"),
    )


@pytest.mark.parametrize(
    "env",
    [
        {"GIT_CONFIG_COUNT": "-1"},
        {"GIT_CONFIG_COUNT": "not-a-count"},
        {"GIT_CONFIG_COUNT": "999999999"},
        {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_VALUE_0": "explicit"},
        {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.bareRepository"},
    ],
)
def test_filter_env_rejects_incomplete_git_configuration(env):
    with pytest.raises(ValueError, match="Git configuration"):
        filter_env(env)


def test_filtered_environment_is_accepted_by_real_git(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        **_git_config(
            ("safe.bareRepository", "explicit"),
            ("credential.interactive", "never"),
            ("core.fsmonitor", "false"),
        ),
    }

    result = subprocess.run(
        [git, "config", "--get", "safe.bareRepository"],
        cwd=tmp_path,
        env=filter_env(env),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "explicit"


@pytest.mark.skipif(
    shutil.which("swift") is None or shutil.which("git") is None,
    reason="the Swift and Git toolchains are required",
)
def test_swift_proof_resolves_local_dependency_with_explicit_bare_policy(tmp_path, monkeypatch):
    from skyn3t.studio.proof_run import _proof_command_context, _run_swift_build

    home = tmp_path / "home"
    home.mkdir()
    for name, value in {
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TRACE": str(tmp_path / "git-trace.log"),
        "GIT_TRACE2_EVENT": str(tmp_path / "git-events.jsonl"),
        **_git_config(
            ("safe.bareRepository", "explicit"),
            ("credential.interactive", "never"),
            ("core.fsmonitor", "false"),
        ),
    }.items():
        monkeypatch.setenv(name, value)
    git = shutil.which("git")
    assert git is not None
    dependency = tmp_path / "fixture-dependency"
    dependency_source = dependency / "Sources" / "FixtureDependency"
    dependency_source.mkdir(parents=True)
    (dependency / "Package.swift").write_text(
        '// swift-tools-version: 5.7\n'
        'import PackageDescription\n'
        'let package = Package(name: "FixtureDependency", products: [\n'
        '    .library(name: "FixtureDependency", targets: ["FixtureDependency"])\n'
        '], targets: [.target(name: "FixtureDependency")])\n',
        encoding="utf-8",
    )
    (dependency_source / "Value.swift").write_text(
        "public func fixtureValue() -> Int { 42 }\n", encoding="utf-8",
    )
    for argv in (
        ["init", "-q"],
        ["add", "Package.swift", "Sources"],
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "Add local fixture"],
        ["tag", "1.0.0"],
    ):
        subprocess.run(
            [git, *argv], cwd=dependency, capture_output=True, text=True,
            timeout=15, check=True,
        )

    project = tmp_path / "app"
    source = project / "Sources" / "FixtureApp"
    source.mkdir(parents=True)
    (project / "Package.swift").write_text(
        '// swift-tools-version: 5.7\n'
        'import PackageDescription\n'
        'let package = Package(name: "FixtureApp", dependencies: [\n'
        f'    .package(url: {json.dumps(dependency.as_uri())}, from: "1.0.0")\n'
        '], targets: [.executableTarget(name: "FixtureApp", dependencies: [\n'
        '    .product(name: "FixtureDependency", package: "fixture-dependency")\n'
        '])])\n',
        encoding="utf-8",
    )
    (source / "main.swift").write_text(
        "import FixtureDependency\nprint(fixtureValue())\n", encoding="utf-8",
    )
    context = _proof_command_context("subprocess", "swift")
    try:
        for _ in range(2):
            ran, passed, summary = _run_swift_build(project, 120, context)
            assert ran, summary
            assert passed, summary
    finally:
        context.swift_resources.close()
    result = subprocess.run(
        [git, "config", "--get", "safe.bareRepository"],
        cwd=project, env=filter_env(dict(os.environ)),
        capture_output=True, text=True, timeout=10, check=True,
    )
    assert result.stdout.strip() == "explicit"
