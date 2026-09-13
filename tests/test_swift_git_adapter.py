from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from skyn3t.security.secrets import filter_env

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX SwiftPM compatibility adapter")


@pytest.fixture
def protected_git(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is not installed")
    env = filter_env({
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "safe.bareRepository",
        "GIT_CONFIG_VALUE_0": "explicit",
        "GIT_CONFIG_KEY_1": "credential.interactive",
        "GIT_CONFIG_VALUE_1": "never",
        "GIT_CONFIG_KEY_2": "core.fsmonitor",
        "GIT_CONFIG_VALUE_2": "false",
    })
    source = tmp_path / "source with spaces.git"
    subprocess.run(
        [git, "init", "--bare", "-q", str(source)],
        env=env, check=True, capture_output=True, timeout=10,
    )
    implicit = subprocess.run(
        [git, "-C", str(source), "rev-parse", "--git-dir"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    if implicit.returncode == 0:
        pytest.skip("Git does not enforce explicit bare-repository protection")
    assert "cannot use bare repository" in implicit.stderr
    return git, env, source


def _run(session, *arguments):
    return subprocess.run(
        ["git", *map(str, arguments)],
        env=filter_env(session.env),
        capture_output=True, text=True, timeout=15, check=False,
    )


def _clone_cache(session, source, name="dependency"):
    cache = session.scratch_dir / "repositories" / name
    result = _run(session, "clone", "--mirror", source, cache, "--progress")
    assert result.returncode == 0, result.stderr
    return cache


def test_adapter_authorizes_only_its_successfully_cloned_mirrors(protected_git):
    from skyn3t.studio.swift_git import swift_git_session

    _, env, source = protected_git
    with swift_git_session(env) as session:
        cache = _clone_cache(session, source)
        for _ in range(2):
            result = _run(session, "-C", cache, "rev-parse", "--is-bare-repository")
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "true"

        denied = _run(session, "-C", source, "rev-parse", "--git-dir")
        assert denied.returncode != 0
        assert "cannot use bare repository" in denied.stderr
        assert _run(session, "config", "--get", "safe.bareRepository").stdout.strip() == "explicit"
        assert _run(session, "config", "--get", "credential.interactive").stdout.strip() == "never"
        assert _run(session, "config", "--get", "core.fsmonitor").stdout.strip() == "false"

        private_root = session.scratch_dir.parent
    assert not private_root.exists()


def test_adapter_does_not_trust_a_planted_repository_by_its_directory_name(protected_git):
    from skyn3t.studio.swift_git import swift_git_session

    git, env, _ = protected_git
    with swift_git_session(env) as session:
        planted = session.scratch_dir / "repositories" / "planted"
        subprocess.run(
            [git, "init", "--bare", "-q", str(planted)],
            env=env, check=True, capture_output=True, timeout=10,
        )

        result = _run(session, "-C", planted, "rev-parse", "--git-dir")

        assert result.returncode != 0
        assert "cannot use bare repository" in result.stderr


def test_adapter_rejects_repeated_location_options(protected_git):
    from skyn3t.studio.swift_git import swift_git_session

    _, env, source = protected_git
    with swift_git_session(env) as session:
        cache = _clone_cache(session, source)

        result = _run(session, "-C", cache, "-C", source, "rev-parse", "--git-dir")

        assert result.returncode != 0
        assert "cannot use bare repository" in result.stderr


def test_adapter_rejects_symlink_escape_after_authorization(protected_git):
    from skyn3t.studio.swift_git import swift_git_session

    _, env, source = protected_git
    with swift_git_session(env) as session:
        cache = _clone_cache(session, source)
        cache.rename(cache.with_name("retired"))
        cache.symlink_to(source, target_is_directory=True)

        result = _run(session, "-C", cache, "rev-parse", "--git-dir")

        assert result.returncode != 0
        assert "cannot use bare repository" in result.stderr


def test_adapter_rejects_replacement_of_an_authorized_directory(protected_git):
    from skyn3t.studio.swift_git import swift_git_session

    git, env, source = protected_git
    with swift_git_session(env) as session:
        cache = _clone_cache(session, source)
        cache.rename(cache.with_name("retired"))
        subprocess.run(
            [git, "init", "--bare", "-q", str(cache)],
            env=env, check=True, capture_output=True, timeout=10,
        )

        result = _run(session, "-C", cache, "rev-parse", "--git-dir")

        assert result.returncode != 0
        assert "cannot use bare repository" in result.stderr


def test_adapter_preserves_already_explicit_git_invocations(protected_git):
    from skyn3t.studio.swift_git import swift_git_session

    _, env, source = protected_git
    with swift_git_session(env) as session:
        result = _run(session, "--git-dir", source, "rev-parse", "--is-bare-repository")

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "true"
