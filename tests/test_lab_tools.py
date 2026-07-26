from __future__ import annotations

from types import SimpleNamespace

from skyn3t.studio.lab_tools import inspect_lab_toolchain


def _completed(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_lab_toolchain_requires_docker_and_playwright_for_web():
    paths = {
        "docker": "/usr/local/bin/docker",
        "playwright": "/usr/local/bin/playwright",
        "maestro": None,
    }
    calls: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        calls.append(tuple(argv))
        if argv[:2] == ["/usr/local/bin/docker", "info"]:
            return _completed(stdout="29.4.0")
        return _completed(stdout="Version 1.61.0")

    report = inspect_lab_toolchain(
        stack="react",
        which=lambda name: paths.get(name),
        run=run,
    )

    assert report.ready is True
    assert report.checks["docker"].ready is True
    assert report.checks["playwright"].required is True
    assert report.checks["maestro"].required is False
    assert calls[0][:2] == ("/usr/local/bin/docker", "info")


def test_mobile_lab_toolchain_reports_missing_maestro_as_blocking():
    report = inspect_lab_toolchain(
        stack="react_native",
        which=lambda name: f"/bin/{name}" if name in {"docker", "playwright"} else None,
        run=lambda *_args, **_kwargs: _completed(stdout="ok"),
    )

    assert report.ready is False
    assert report.missing_required == ["maestro"]


def test_mobile_lab_toolchain_requires_maestro_without_docker():
    calls: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        calls.append(tuple(argv))
        return _completed(stdout="1.40.0")

    report = inspect_lab_toolchain(
        stack="react_native",
        which=lambda name: "/opt/bin/maestro" if name == "maestro" else None,
        run=run,
    )

    assert report.ready is True
    assert report.missing_required == []
    assert report.checks["docker"].required is False
    assert report.checks["maestro"].required is True
    assert calls == [("/opt/bin/maestro", "--version")]


def test_docker_cli_without_daemon_is_not_ready():
    report = inspect_lab_toolchain(
        stack="static",
        which=lambda name: f"/bin/{name}",
        run=lambda argv, **_kwargs: (
            _completed(code=1, stderr="daemon unavailable")
            if argv[:2] == ["/bin/docker", "info"]
            else _completed(stdout="ok")
        ),
    )

    assert report.checks["docker"].installed is True
    assert report.checks["docker"].ready is False
    assert "daemon unavailable" in report.checks["docker"].detail


def test_non_mobile_build_does_not_boot_maestro_just_to_report_optional_install():
    calls: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        calls.append(tuple(argv))
        return _completed(stdout="ready")

    report = inspect_lab_toolchain(
        stack="python",
        which=lambda name: f"/bin/{name}",
        run=run,
    )

    assert report.checks["maestro"].ready is True
    assert "probe skipped" in report.checks["maestro"].detail
    assert report.checks["docker"].required is False
    assert not any(call[0].endswith("docker") for call in calls)
    assert not any(call[0].endswith("maestro") for call in calls)
