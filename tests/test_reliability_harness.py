import sys

from scripts.reliability_harness import skyn3t_command


def test_skyn3t_command_uses_active_interpreter() -> None:
    assert skyn3t_command("doctor") == [
        sys.executable,
        "-m",
        "skyn3t.cli.main",
        "doctor",
    ]
