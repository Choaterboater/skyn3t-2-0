"""Build and exercise the terminal agent as an installed, offline wheel."""

import json
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

from skyn3t.agents._scaffold import scaffold_for


def test_terminal_agent_wheel_runs_outside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    files = scaffold_for("python_cli", "wheel-pilot", "a terminal assistant for notes")
    for relative, content in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    wheels = tmp_path / "wheels"
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheels),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    (wheel,) = wheels.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert (
            archive.read("copilot_wheel_pilot/scenarios.json").decode() == files["scenarios.json"]
        )
        assert "copilot_wheel_pilot/main.py" in archive.namelist()
        assert "copilot_wheel_pilot/cli_core.py" in archive.namelist()
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(python),
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # Remove the source entirely: imports and data must come from the wheel.
    source.rename(tmp_path / "unavailable-source")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("Offline notes for the installed CLI.")
    clean_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    cli = scripts / ("wheel-pilot.exe" if os.name == "nt" else "wheel-pilot")
    doctor = subprocess.run(
        [str(cli), "doctor", "--output-format", "json"],
        cwd=outside,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert json.loads(doctor.stdout)["ok"] is True
    run = subprocess.run(
        [str(cli), "run", "summarize my notes", "--format", "json"],
        cwd=outside,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    events = [json.loads(line) for line in run.stdout.splitlines()]
    assert events[0]["type"] == "session_start"
    assert any(event["type"] == "answer" for event in events)
    assert any(event.get("content") == "Offline notes for the installed CLI." for event in events)
    denied = subprocess.run(
        [str(cli), "run", "write the file", "--format", "json"],
        cwd=outside,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert any(json.loads(line)["type"] == "denied" for line in denied.stdout.splitlines())
    assert not (outside / "out.txt").exists()
    allowed = subprocess.run(
        [str(cli), "run", "write the file", "--format", "json", "--mode", "workspace-write"],
        cwd=outside,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert (outside / "out.txt").read_text() == "hello from the copilot"
