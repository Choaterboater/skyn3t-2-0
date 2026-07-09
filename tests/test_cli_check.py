"""Tests for the deterministic CLI gate (``skyn3t/studio/cli_check.py``).

Fixture CLIs are stdlib argparse programs. Degrade-open paths pin SKIP (no
gaps); real defects pin ISSUES. The live integrations drive the actual
python_cli base scaffold and the §3.6 terminal-copilot variant through the
gate — both must pass.
"""

from __future__ import annotations

import sys

from skyn3t.studio.cli_check import check_cli, parse_subcommands

_GOOD_SUBCOMMAND_CLI = '''
import argparse, sys

def main():
    parser = argparse.ArgumentParser(description="a well-behaved cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("greet")
    sub.add_parser("count")
    args = parser.parse_args()
    print(f"ran {args.command}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''

# The help text ADVERTISES a subcommand ("count") that was never implemented —
# `main.py count --help` fails while the top-level help looks fine.
_BROKEN_SUBCOMMAND_CLI = '''
import sys

if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
    print("usage: main.py {greet,count} ...")
    sys.exit(0)
if sys.argv[1] == "greet":
    print("greet ok")
    sys.exit(0)
print(f"unknown command {sys.argv[1]}", file=sys.stderr)
sys.exit(2)
'''

# Accepts absolutely anything with exit 0 (a subcommand CLI faking argparse).
_ACCEPT_ANYTHING_CLI = '''
import sys
print("usage: main.py {greet,count} ...")
print("I accept anything!")
sys.exit(0)
'''

_HELP_CRASH_CLI = '''
raise RuntimeError("boom on import")
'''

_DEP_MISSING_CLI = 'raise ModuleNotFoundError("No module named \'pandas\'")\n'


def _project(tmp_path, code: str):
    (tmp_path / "main.py").write_text(code, encoding="utf-8")
    return tmp_path


# ---- helpers ----------------------------------------------------------------


def test_parse_subcommands_reads_argparse_help():
    assert parse_subcommands("usage: x [-h] {run,doctor} ...") == ["run", "doctor"]
    assert parse_subcommands("usage: flat [-h] name") == []
    # Deduped + bounded.
    many = "{a,b,c,d,e,f,g,h,a}"
    assert len(parse_subcommands(many)) == 6


# ---- degrade-open skips ------------------------------------------------------


def test_skips_non_cli_stack(tmp_path):
    v = check_cli(_project(tmp_path, _GOOD_SUBCOMMAND_CLI), stack="rag")
    assert v.skipped and v.gaps() == []


def test_skips_missing_main(tmp_path):
    v = check_cli(tmp_path, stack="python_cli")
    assert v.skipped and v.gaps() == []


def test_missing_third_party_dep_soft_skips(tmp_path):
    v = check_cli(_project(tmp_path, _DEP_MISSING_CLI), stack="python_cli")
    assert v.skipped, v.to_dict()
    assert v.gaps() == []


# ---- the contract ------------------------------------------------------------


def test_good_subcommand_cli_passes(tmp_path):
    v = check_cli(_project(tmp_path, _GOOD_SUBCOMMAND_CLI), stack="python_cli")
    assert v.ok, v.to_dict()
    assert v.checked["subcommands"] == ["greet", "count"]
    assert isinstance(v.checked["nonsense_exit"], int)
    assert v.checked["nonsense_exit"] != 0


def test_crash_on_help_is_an_issue(tmp_path):
    v = check_cli(_project(tmp_path, _HELP_CRASH_CLI), stack="python_cli")
    assert not v.skipped
    assert any("--help" in i for i in v.issues), v.issues


def test_broken_advertised_subcommand_is_an_issue(tmp_path):
    v = check_cli(_project(tmp_path, _BROKEN_SUBCOMMAND_CLI), stack="python_cli")
    assert not v.skipped
    assert any("'count'" in i for i in v.issues), v.issues


def test_accepting_nonsense_is_an_issue(tmp_path):
    v = check_cli(_project(tmp_path, _ACCEPT_ANYTHING_CLI), stack="python_cli")
    assert not v.skipped
    assert any("exited 0" in i for i in v.issues), v.issues


def test_flat_cli_skips_the_nonsense_probe(tmp_path):
    flat = '''
import argparse
parser = argparse.ArgumentParser(description="flat greeter")
parser.add_argument("name")
args = parser.parse_args()
print(f"hello {args.name}")
'''
    v = check_cli(_project(tmp_path, flat), stack="python_cli")
    assert v.ok, v.to_dict()
    assert v.checked["nonsense_exit"] == "not_probed_flat_cli"


def test_cli_check_does_not_pass_host_secrets_to_generated_cli(tmp_path, monkeypatch):
    import subprocess

    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-host-secret")

    def fake_run(*args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(args[0], 0, stdout="usage: main.py {run} ...", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    v = check_cli(_project(tmp_path, _GOOD_SUBCOMMAND_CLI), stack="python_cli")

    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not v.skipped


# ---- live integrations: the real scaffolds pass the gate ---------------------


def test_python_cli_base_scaffold_passes(tmp_path):
    from skyn3t.agents._scaffold import scaffold_for

    for rel, contents in scaffold_for("python_cli", "demo", "a demo tool").items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")
    v = check_cli(tmp_path, stack="python_cli", python_exec=sys.executable)
    assert not v.skipped, v.to_dict()
    assert v.ok, v.to_dict()


def test_terminal_copilot_variant_passes(tmp_path):
    from skyn3t.agents._scaffold import scaffold_for

    files = scaffold_for("python_cli", "pilot", "a terminal assistant for my notes")
    assert "cli_core.py" in files  # the variant, not the base
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")
    v = check_cli(tmp_path, stack="python_cli", python_exec=sys.executable)
    assert not v.skipped, v.to_dict()
    assert v.ok, v.to_dict()
    assert set(v.checked["subcommands"]) == {"run", "chat", "doctor"}
