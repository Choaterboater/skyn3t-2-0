# tests/test_cli_liveness.py
"""The `skyn3t studio liveness` command is registered and its helper is wired to
the (already-tested) liveness loop."""
from __future__ import annotations

import inspect


def test_liveness_command_registered():
    from skyn3t.cli.main import studio_app

    names = {c.name or (c.callback and c.callback.__name__) for c in studio_app.registered_commands}
    assert "liveness" in names


def test_run_liveness_cli_is_async():
    from skyn3t.cli import main as cli_main

    assert hasattr(cli_main, "_run_liveness_cli")
    assert inspect.iscoroutinefunction(cli_main._run_liveness_cli)
