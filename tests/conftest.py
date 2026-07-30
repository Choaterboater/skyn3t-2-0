"""Test-session defaults.

Pin the LLM backend to the deterministic offline ``stub`` so the suite never
shells out to a locally-installed CLI (claude/kimi/copilot) or the network —
tests stay fast, hermetic, and reproducible regardless of what's on the host.
"""

import os
import socket
from functools import cache, lru_cache

import pytest

os.environ["SKYN3T_LLM_BACKEND"] = "stub"

try:  # settings may have been imported+cached already
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
except Exception:  # pragma: no cover
    pass


_LOOPBACK_BIND_PATTERNS = (
    'HTTPServer(("127.0.0.1"',
    'ThreadingHTTPServer(("127.0.0.1"',
    'socket.bind(("127.0.0.1"',
    's.bind(("127.0.0.1"',
    "MockLLMServer()",
    "AppRunner()",
)


@lru_cache(maxsize=1)
def _loopback_bind_available() -> bool:
    try:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return True
        finally:
            s.close()
    except OSError:
        return False


@cache
def _test_file_needs_loopback(path: str) -> bool:
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return False
    return any(pattern in text for pattern in _LOOPBACK_BIND_PATTERNS)


@lru_cache(maxsize=1)
def _docker_available() -> bool:
    """Reuse the product's own probe so the skip cannot drift from the need."""
    try:
        from skyn3t.config.settings import Settings
        from skyn3t.security.sandbox import SandboxRunner

        return bool(SandboxRunner(Settings()).docker_available())
    except Exception:  # noqa: BLE001 - absent/unimportable docker == unavailable
        return False


@lru_cache(maxsize=1)
def _posix_modes_available() -> bool:
    """True when chmod can express distinct permission bits.

    Windows collapses 0o755 and 0o644 to 0o666 and ignores directory chmod
    entirely, so tests asserting exact POSIX modes cannot pass there. Probed
    rather than keyed off sys.platform so the reason stays factual.
    """
    import os as _os
    import stat as _stat
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as directory:
            probe = _os.path.join(directory, "probe")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("x")
            _os.chmod(probe, 0o755)
            executable = _stat.S_IMODE(_os.stat(probe).st_mode)
            _os.chmod(probe, 0o644)
            plain = _stat.S_IMODE(_os.stat(probe).st_mode)
            return executable != plain
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip on MISSING CAPABILITY, never on a known-bad list.

    A capability skip states why it did not run and what would make it run. A
    known-failures allowlist states neither and rots — which is exactly how a
    real product bug (proof-artifact reuse being broken on Windows) sat
    undetected inside a "these always fail here" pile.
    """
    loopback_ok = _loopback_bind_available()
    capabilities = (
        ("requires_loopback", loopback_ok,
         "loopback socket bind is not available in this environment"),
        ("requires_docker", _docker_available(),
         "Docker daemon is not available in this environment"),
        ("requires_posix_modes", _posix_modes_available(),
         "filesystem cannot represent POSIX permission bits (Windows)"),
    )
    for item in items:
        for marker, available, reason in capabilities:
            if not available and item.get_closest_marker(marker):
                item.add_marker(pytest.mark.skip(reason=reason))
        if not loopback_ok:
            path = str(getattr(item, "path", "") or "")
            if _test_file_needs_loopback(path):
                item.add_marker(
                    pytest.mark.skip(reason=capabilities[0][2])
                )


@pytest.fixture(autouse=True)
def _no_cli_vision(request, monkeypatch):
    """Neuter the CLI vision fallback for the whole suite.

    ``visual_check._make_cli_vision_fn`` spawns a REAL ``claude -p`` whenever the
    CLI is on PATH and no OpenRouter key is set — so any test path reaching an
    actual screenshot+judge (qa_playtest / game_visual / liveness) silently burns
    CLI quota on a dev machine with claude installed. Tests that exercise the CLI
    path itself opt out with ``@pytest.mark.real_cli_vision`` — those stub
    ``shutil.which``/``subprocess`` themselves, so nothing real is ever spawned.
    """
    if request.node.get_closest_marker("real_cli_vision"):
        yield
        return
    from skyn3t.studio import visual_check

    monkeypatch.setattr(visual_check, "_make_cli_vision_fn", lambda settings: None)
    yield


@pytest.fixture(autouse=True)
def _no_real_llm_cli(monkeypatch):
    """Keep ``llm_backend=auto`` offline even when a developer has CLIs installed.

    Tests that cover a CLI backend replace these caches and mock subprocess
    creation themselves. This fence prevents unrelated tests from consuming a
    signed-in Codex/Claude/Copilot account merely because the binary is on PATH.
    """
    from skyn3t.adapters.llm import KNOWN_CLI_PROVIDERS, LLMClient

    monkeypatch.setattr(
        LLMClient,
        "_cli_cache",
        {provider: False for provider in KNOWN_CLI_PROVIDERS},
    )
    monkeypatch.setattr(LLMClient, "_cli_cache_checked_at", {})
    monkeypatch.setattr(LLMClient, "_cli_version_cache", {})
    yield


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Point every test's ``data_dir`` at a unique temp dir.

    Subsystems persist to ``data_dir`` (cortex proposals.jsonl, model_tournament,
    tuning/prompt overrides, the SQLite db, snapshots). Without isolation those
    files leak ACROSS tests (shared dedup/tournament state) and INTO the real
    ``./data`` — a test once wrote fake cortex proposals there that the live
    server would then load. Tests that pass an explicit ``Settings(data_dir=...)``
    still win (init args outrank env), so this only redirects the default.
    """
    from skyn3t.config import settings as settings_mod

    # Runtime backend-selection tests intentionally mutate os.environ. Re-pin
    # the offline backend for every test so that mutation cannot leak into the
    # next test and make unrelated API tests depend on an installed CLI.
    monkeypatch.setenv("SKYN3T_LLM_BACKEND", "stub")
    # Runtime settings endpoints intentionally update os.environ so future
    # builds see the new policy immediately. Re-pin mutable lab switches per
    # test; otherwise a settings-route test can leak autonomy/research/merge
    # consent into every later test in the same pytest process.
    monkeypatch.setenv("SKYN3T_LAB_AUTONOMY", "false")
    monkeypatch.setenv("SKYN3T_GITHUB_SIMILARITY_RESEARCH", "false")
    monkeypatch.setenv("SKYN3T_CORTEX_CANDIDATES_ENABLED", "true")
    monkeypatch.setenv("SKYN3T_CORTEX_CANDIDATE_AUTO_MERGE", "false")
    # External Docker/Playwright/Maestro proof has focused seam tests. Keep the
    # rest of the suite hermetic even though production builds require it.
    monkeypatch.setenv("SKYN3T_PROOF_LADDER_REQUIRED", "false")
    # Gate posture defaults to "lab" for real builds (findings are recorded, not
    # blocking). The suite asserts on 2.0's blocking behaviour in ~51 files, so
    # pin "release" here and let the focused posture tests opt into lab via an
    # explicit Settings(build_posture="lab") kwarg (init args outrank env).
    monkeypatch.setenv("SKYN3T_BUILD_POSTURE", "release")
    # The MoA council ships ON. It is inert on the stub backend, so most of the
    # suite would be unaffected — but ~18 files construct a NON-stub backend,
    # and there an advisor slot would spawn a real signed-in CLI mid-test. Pin
    # the master switch off and let the focused council tests opt in with an
    # explicit Settings(moa_enabled=True, moa_advisors=...) kwarg.
    monkeypatch.setenv("SKYN3T_MOA_ENABLED", "false")
    monkeypatch.setenv("SKYN3T_MOA_ADVISORS", "")
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    # Don't read the developer's real repo .env during tests. Settings hard-codes
    # env_file=REPO_ROOT/.env, so a locally-configured secret (replicate/github
    # token, …) otherwise bleeds in and breaks tests that assert on the DEFAULT
    # ("not configured"). Tests get config from explicit kwargs + os.environ only.
    monkeypatch.setitem(settings_mod.Settings.model_config, "env_file", None)
    # LLM keys included: a shell-exported SKYN3T_OPENROUTER_API_KEY would flip
    # every test's LLMClient from "stub" to a REAL paid backend (the vision-CLI
    # cousin of this leak is fenced by _no_cli_vision above). Tests that need a
    # key set it explicitly via monkeypatch/kwargs.
    for _var in ("SKYN3T_REPLICATE_API_TOKEN", "SKYN3T_REPLICATE_MODEL",
                 "SKYN3T_GITHUB_TOKEN",
                 "SKYN3T_OPENROUTER_API_KEY", "SKYN3T_ANTHROPIC_API_KEY",
                 "SKYN3T_OPENAI_API_KEY", "SKYN3T_KIMI_API_KEY"):
        monkeypatch.delenv(_var, raising=False)
    settings_mod.get_settings.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()
