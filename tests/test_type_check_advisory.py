"""A type error must not fail a page that builds.

Measured on a delivered Astro site:

    npx astro build   ->  exit 0, 14 pages built     the app WORKS
    npx astro check   ->  1 error, ts(2352)          a quality complaint

Its package.json had `"build": "astro check && astro build"`, so that one type
error was reported as `<build>` — "the delivery is broken" — and the build
scored no_go 44.

Nothing in SkyN3t asked for that chained check:

  * our own scaffold emits plain `"build": "astro build"` (_scaffold.py:1692)
  * both MoA advisors independently specified `"build": "astro build"`

Codegen overwrote the scaffold and added the gate that then failed it.

The split is deliberately narrow. A genuine compile failure must still block —
turning a broken app into a pass would be far worse than the bug being fixed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skyn3t.studio import proof_run as pr
from skyn3t.studio.proof_run import compile_only_segment

_TS_ERROR = (
    "src/utils/lessons.ts:19:18 - error ts(2352): Conversion of type "
    "'{ [k: string]: never[]; }' to type 'Record<\"grip\", T[]>' may be a mistake.\n"
    "Result (39 files):\n- 1 error\n"
)
_REAL_COMPILE_ERROR = (
    "[vite]: Rollup failed to resolve import \"./missing.js\"\n"
    "error during build:\n"
)


# ---- the script parser ---------------------------------------------------

def test_the_observed_astro_script_splits():
    assert compile_only_segment("astro check && astro build") == "astro build"


@pytest.mark.parametrize("checker", ["vue-tsc", "svelte-check", "tsc --noEmit", "tsc -b"])
def test_other_type_checkers_split_too(checker):
    assert compile_only_segment(f"{checker} && vite build") == "vite build"


def test_the_stock_vite_ts_template_script_splits():
    """The legacy create-vite TS template emits a bare `tsc` first segment."""
    assert compile_only_segment("tsc && vite build") == "vite build"


def test_a_tsc_lookalike_binary_is_not_split():
    """Token boundary: `tsc-watch` is not a type check we recognise."""
    assert compile_only_segment("tsc-watch && vite build") == ""


def test_a_plain_build_is_not_split():
    """The scaffold's own script — nothing to separate."""
    assert compile_only_segment("astro build") == ""


def test_an_unrecognised_first_segment_is_not_split():
    """`npm run clean && vite build` must stay a single hard build."""
    assert compile_only_segment("rimraf dist && vite build") == ""


def test_a_three_part_chain_is_not_split():
    """Ambiguous which part is the compile; refuse rather than guess."""
    assert compile_only_segment("astro check && astro build && node post.js") == ""


def test_empty_and_malformed_scripts_are_safe():
    for s in ("", "   ", "&&", "astro check &&", "&& astro build", None):
        assert compile_only_segment(s) == ""


# ---- the build behaviour -------------------------------------------------

def _project(tmp_path, build_script: str):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo-app", "version": "1.0.0",
                    "scripts": {"build": build_script}}),
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir(exist_ok=True)
    return tmp_path


def _stub_commands(monkeypatch, results):
    """Script _run_proof_command; record the argv it was handed."""
    seen: list[list[str]] = []
    seq = list(results)

    def fake(ctx, command, *, cwd, timeout, env=None, network=False):
        seen.append(list(command))
        code, out = seq.pop(0)
        return pr._ProofCommandResult(code, out, "")

    monkeypatch.setattr(pr, "_run_proof_command", fake)
    monkeypatch.setattr(pr, "_prepare_node_dependencies", lambda *a, **k: (True, True, "install ok"))
    monkeypatch.setattr(pr, "npm_build_current", lambda *a, **k: False)
    monkeypatch.setattr(pr, "mark_npm_build_current", lambda *a, **k: None)
    monkeypatch.setattr(pr.shutil, "which", lambda n: "npm")
    return seen


def test_a_type_only_failure_reports_the_app_as_built(tmp_path, monkeypatch):
    root = _project(tmp_path, "astro check && astro build")
    seen = _stub_commands(monkeypatch, [(1, _TS_ERROR), (0, "8 pages built")])
    findings: dict[str, str] = {}

    ran, ok, _summary = pr._run_node_build(root, "astro", 300, None, findings=findings)

    assert (ran, ok) == (True, True), "the app compiles; it must not be a build failure"
    assert "ts(2352)" in findings["type_check"], "the type error must still be reported"
    assert len(seen) == 2
    assert seen[1] == ["npm", "exec", "--", "astro", "build"], seen[1]


def test_a_real_compile_failure_still_fails(tmp_path, monkeypatch):
    """The retry must never rescue an app that genuinely cannot build."""
    root = _project(tmp_path, "astro check && astro build")
    seen = _stub_commands(monkeypatch, [(1, _TS_ERROR), (1, _REAL_COMPILE_ERROR)])
    findings: dict[str, str] = {}

    ran, ok, summary = pr._run_node_build(root, "astro", 300, None, findings=findings)

    assert (ran, ok) == (True, False)
    assert "type_check" not in findings
    assert "Rollup failed to resolve" in summary
    assert len(seen) == 2


def test_a_non_type_failure_is_not_retried(tmp_path, monkeypatch):
    """Only type diagnostics justify a second build."""
    root = _project(tmp_path, "astro check && astro build")
    seen = _stub_commands(monkeypatch, [(1, _REAL_COMPILE_ERROR)])

    ran, ok, _ = pr._run_node_build(root, "astro", 300, None, findings={})

    assert (ran, ok) == (True, False)
    assert len(seen) == 1, "no retry for a failure that is not a type error"


def test_a_plain_build_failure_is_not_retried(tmp_path, monkeypatch):
    """Nothing to split, so a failure is simply a failure."""
    root = _project(tmp_path, "astro build")
    seen = _stub_commands(monkeypatch, [(1, _TS_ERROR)])

    ran, ok, _ = pr._run_node_build(root, "astro", 300, None, findings={})

    assert (ran, ok) == (True, False)
    assert len(seen) == 1


def _project_with_scripts(tmp_path, scripts):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo-app", "version": "1.0.0", "scripts": scripts}),
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir(exist_ok=True)
    return tmp_path


def test_a_standalone_typecheck_type_failure_is_advisory_after_a_passing_build(
    tmp_path, monkeypatch
):
    """A separately declared `typecheck` script failing on TYPE diagnostics is
    the same quality finding as a chained `<check> && <build>` script."""
    root = _project_with_scripts(
        tmp_path, {"build": "vite build", "typecheck": "tsc --noEmit"}
    )
    seen = _stub_commands(
        monkeypatch,
        [
            (0, "built in 1.2s"),
            (1, "src/App.tsx(4,7): error TS2322: Type 'string' is not assignable."),
        ],
    )
    findings: dict[str, str] = {}

    ran, ok, summary = pr._run_node_build(root, "react", 300, None, findings=findings)

    assert (ran, ok) == (True, True), "the app builds; the type error must not fail proof"
    assert "TS2322" in findings["type_check"]
    assert "advisory" in summary
    assert seen == [["npm", "run", "build"], ["npm", "run", "typecheck"]]


def test_a_standalone_check_crash_still_fails_the_build(tmp_path, monkeypatch):
    """No type diagnostics means a checker crash/misconfig — still a hard fail."""
    root = _project_with_scripts(
        tmp_path, {"build": "astro build", "check": "astro check"}
    )
    seen = _stub_commands(
        monkeypatch,
        [
            (0, "14 pages built"),
            (1, "Cannot find module 'typescript'"),
        ],
    )
    findings: dict[str, str] = {}

    ran, ok, summary = pr._run_node_build(root, "astro", 300, None, findings=findings)

    assert (ran, ok) == (True, False)
    assert "type_check" not in findings
    assert "Cannot find module" in summary
    assert len(seen) == 2


def test_omitting_findings_preserves_the_old_signature(tmp_path, monkeypatch):
    """~12 existing call sites pass three positional args and unpack a triple."""
    root = _project(tmp_path, "astro check && astro build")
    _stub_commands(monkeypatch, [(1, _TS_ERROR), (0, "built")])

    result = pr._run_node_build(root, "astro", 300, None)

    assert isinstance(result, tuple) and len(result) == 3
    assert result[:2] == (True, True)
