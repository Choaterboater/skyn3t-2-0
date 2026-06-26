"""Bounded fix loop: a failing objective proof is repaired, not just reported."""

from __future__ import annotations

import json as _json
import shutil

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.planner import Planner
from skyn3t.studio.proof_run import proof_run
from skyn3t.studio.runner import StudioRunner


def _runner():
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def test_fill_missing_synthesizes_real_entrypoint(tmp_path):
    # A package with real code but NO runnable root — the exact failure mode the
    # old pipeline shipped (taskcli-v4). The proof must reject it, and the fix
    # loop must WIRE a real entrypoint to the delivered code, not stub-fill.
    runner = _runner()
    plan = Planner(Settings()).plan("a python cli task tool", "mdi")
    proj = tmp_path / "proj"
    pkg = proj / "mdi"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def run():\n    print('idea')\n    return 0\n")
    (proj / "README.md").write_text("# mdi\n")

    before = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    assert before.passed is False  # no runnable entrypoint -> not proven

    filled = runner._fill_missing(str(proj), plan, "a python cli task tool", list(before.missing))
    assert filled >= 1
    # A real wired entrypoint was synthesized (not an empty stub).
    main_py = (proj / "main.py").read_text()
    assert "from mdi.core import run" in main_py

    after = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    assert after.passed is True


def test_deterministic_repairs_declares_deps_peers_and_stubs(tmp_path):
    """The deterministic repairs run each fix-loop iteration: declare an
    imported-but-undeclared npm dep, add the next.config optimizeCss peer
    (critters), and stub a missing @/ alias import — all idempotent."""
    runner = _runner()
    plan = Planner(Settings()).plan("a nextjs marketing site", "site")
    plan.stack = "nextjs"
    proj = tmp_path / "p"
    (proj / "app").mkdir(parents=True)
    (proj / "components").mkdir()
    (proj / "jsconfig.json").write_text(
        _json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}}}), encoding="utf-8")
    (proj / "next.config.js").write_text("module.exports={experimental:{optimizeCss:true}}", encoding="utf-8")
    (proj / "package.json").write_text(_json.dumps({"dependencies": {"next": "14.2.3"}}), encoding="utf-8")
    (proj / "app" / "page.jsx").write_text(
        "import { z } from 'zod'\nimport Hero from '@/components/Hero'\n"
        "export default function P(){return <Hero/>}\n", encoding="utf-8")

    changes = runner._deterministic_repairs(str(proj), plan)
    pkg = _json.loads((proj / "package.json").read_text())
    assert "zod" in pkg["dependencies"]                  # undeclared import declared
    assert "critters" in pkg.get("devDependencies", {})  # next.config peer added
    assert (proj / "components" / "Hero.jsx").exists()   # missing alias import stubbed
    assert "zod" in changes["npm_deps_added"]
    assert changes["next_config_peers"] == ["critters"]

    # idempotent: a second pass changes nothing
    again = runner._deterministic_repairs(str(proj), plan)
    assert again["npm_deps_added"] == [] and again["next_config_peers"] == [] \
        and again["imports_scaffolded"] == []


def test_stub_for_known_files():
    runner = _runner()
    plan = Planner(Settings()).plan("x", "x")
    assert "[project]" in runner._stub_for("pyproject.toml", plan, "x")
    assert runner._stub_for("src/__init__.py", plan, "x") == ""
    assert "def test" in runner._stub_for("tests/test_basic.py", plan, "x")
    assert runner._stub_for("weird.xyz", plan, "x") is None


async def test_fix_loop_stops_when_proof_passes(tmp_path):
    # A loop with already-passing proof returns immediately (no infinite loop).
    runner = _runner()
    plan = Planner(Settings()).plan("a python tool", "t")
    proj = tmp_path / "p"
    proj.mkdir()
    for rel in plan.checklist:
        f = proj / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n" if rel.endswith(".py") else "# ok\n")
    (proj / "main.py").write_text("print('hi')\n")

    class _M:
        build_id = "b"
        slug = "t"
        brief = "a python tool"
        files = []
        extra = {}

    proof = proof_run(str(proj), checklist=plan.checklist, stack=plan.stack)
    out = await runner._fix_loop(_M(), plan, str(proj), proof, "cid", {"max_fix_attempts": 2})
    assert out.passed is True


async def test_fix_loop_converges_past_two_attempts(tmp_path, monkeypatch):
    # The convergence loop must keep retrying past the old 2-cap until the proof
    # passes — a multi-error cascade needs >2 passes (each proof reveals the next).
    runner = _runner()
    plan = Planner(Settings()).plan("a python tool", "t")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "main.py").write_text("print('hi')\n")

    class _P:
        def __init__(self, passed):
            self.passed, self.missing, self.detail = passed, [], {}

        def error_gaps(self):
            return []

        def to_dict(self):
            return {"passed": self.passed}

    seq = [_P(False), _P(False), _P(False), _P(True)]  # green on the 4th re-proof
    calls = {"n": 0}

    def fake_proof(*a, **k):
        i = calls["n"]
        calls["n"] += 1
        return seq[min(i, len(seq) - 1)]

    monkeypatch.setattr("skyn3t.studio.runner.proof_run", fake_proof)

    class _M:
        build_id, slug, brief, files, extra = "b", "t", "a python tool", [], {}

    out = await runner._fix_loop(_M(), plan, str(proj), _P(False), "cid", {})
    assert out.passed is True
    assert calls["n"] >= 3  # iterated past the old 2-attempt cap


# ---- substance gate + best-of-N richness ranking -------------------------
def test_largest_source_bytes(tmp_path):
    runner = _runner()
    proj = tmp_path / "p"
    (proj / "src").mkdir(parents=True)
    (proj / "main.py").write_text("x" * 50)            # thin source
    (proj / "README.md").write_text("y" * 5000)         # not source (excluded)
    (proj / "test_x.py").write_text("z" * 9000)         # test (excluded)
    assert runner._largest_source_bytes(str(proj)) == 50
    # __init__.py counts (can hold the real implementation); bytes SUM
    (proj / "src" / "__init__.py").write_text("z" * 2000)
    assert runner._largest_source_bytes(str(proj)) == 2050
    assert runner._substance_floor == 1500


def test_best_of_n_prefers_richer_over_more_files():
    from skyn3t.studio.best_of_n import Candidate, select

    class _Proof:
        passed = True
        score = 90.0
        files_substantive = 3

    rich = Candidate(index=0, worktree=None, proof=_Proof(), files_written=2, source_bytes=28000)
    thin = Candidate(index=1, worktree=None, proof=_Proof(), files_written=8, source_bytes=559)
    res = select([thin, rich])
    assert res.winner is rich  # substance beats raw file count


# ---- proof gating: runnable entrypoint + the generated suite -------------
def test_proof_rejects_package_with_no_entrypoint(tmp_path):
    # Real package code but no runnable root: NOT proven (the taskcli failure).
    proj = tmp_path / "p"
    pkg = proj / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def run():\n    return 0\n")
    res = proof_run(str(proj), stack="python")
    assert res.passed is False
    assert "<entrypoint>" in res.missing


def _proj_with_test(tmp_path, body: str):
    proj = tmp_path / "p"
    tests = proj / "tests"
    tests.mkdir(parents=True)
    (proj / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (tests / "test_add.py").write_text(body)
    return proj


def test_proof_runs_generated_tests_pass(tmp_path):
    proj = _proj_with_test(
        tmp_path, "from main import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    res = proof_run(str(proj), stack="python", run_tests=True)
    assert res.passed is True
    assert res.detail.get("tests") == "passed"


def test_proof_runs_generated_tests_fail(tmp_path):
    proj = _proj_with_test(
        tmp_path, "from main import add\n\n\ndef test_add():\n    assert add(1, 2) == 99\n"
    )
    res = proof_run(str(proj), stack="python", run_tests=True)
    assert res.passed is False
    assert res.detail.get("tests") == "failed"
    assert "<tests>" in res.missing


def test_proof_soft_skips_when_no_tests(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "main.py").write_text("def add(a, b):\n    return a + b\n")
    res = proof_run(str(proj), stack="python", run_tests=True)
    assert res.passed is True  # no tests is a soft skip, never a hard fail
    assert res.detail.get("tests") == "skipped"


def test_proof_does_not_run_tests_by_default(tmp_path):
    # run_tests defaults OFF: a failing test in the tree must NOT fail a proof
    # unless test execution is explicitly enabled (offline/CI kill-switch).
    proj = tmp_path / "p"
    tests = proj / "tests"
    tests.mkdir(parents=True)
    (proj / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (tests / "test_x.py").write_text("def test_x():\n    assert False\n")
    res = proof_run(str(proj), stack="python")  # no run_tests
    assert res.passed is True
    assert "tests" not in res.detail


def _node_proj(tmp_path, build_script: str | None):
    proj = tmp_path / "p"
    (proj / "src").mkdir(parents=True)
    pkg = {"name": "t", "version": "1.0.0", "private": True}
    if build_script is not None:
        pkg["scripts"] = {"build": build_script}
    (proj / "package.json").write_text(_json.dumps(pkg, indent=2) + "\n")
    (proj / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script></body></html>\n'
    )
    (proj / "src" / "main.jsx").write_text("console.log('hi')\n")
    return proj


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_proof_node_build_pass(tmp_path):
    proj = _node_proj(tmp_path, 'node -e "process.exit(0)"')
    res = proof_run(str(proj), stack="react", run_build=True, build_timeout=120)
    assert res.passed is True
    assert res.detail.get("build") == "passed"


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_proof_node_build_fail(tmp_path):
    proj = _node_proj(tmp_path, 'node -e "process.exit(1)"')
    res = proof_run(str(proj), stack="react", run_build=True, build_timeout=120)
    assert res.passed is False
    assert res.detail.get("build") == "failed"
    assert "<build>" in res.missing


def test_proof_node_build_soft_skip_no_script(tmp_path):
    proj = _node_proj(tmp_path, None)  # no build script
    res = proof_run(str(proj), stack="react", run_build=True)
    assert res.passed is True  # no build script -> soft skip, never a hard fail
    assert res.detail.get("build") == "skipped"


def test_proof_does_not_run_build_by_default(tmp_path):
    # run_build defaults OFF at the call site (heavyweight/network); only the
    # runner turns it on from settings.
    proj = _node_proj(tmp_path, 'node -e "process.exit(1)"')
    res = proof_run(str(proj), stack="react")  # no run_build
    assert res.passed is True
    assert "build" not in res.detail


# ---- fix-loop can target + repair package.json (findings #3, #11) -----------
def test_targets_package_json_on_npm_error(tmp_path):
    """A build failure carrying an npm error must route the fix-loop to
    package.json (its regex previously omitted .json, so deps were never
    repaired and the loop churned on App.jsx)."""
    from skyn3t.agents.code_improver import CodeImproverAgent
    agent = CodeImproverAgent(event_bus=EventBus())
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    gaps = ["BUILD FAILED: npm error code ETARGET No matching version found for "
            "@react-three/fiber@8.15.21"]
    assert "package.json" in agent._targets_from_gaps(gaps, tmp_path)


def test_deterministic_sanitizes_package_json(tmp_path):
    """Offline deterministic fix repairs malformed dep keys: trim a fixable
    leading space, drop unfixable names, keep valid ones."""
    from skyn3t.agents.code_improver import CodeImproverAgent
    agent = CodeImproverAgent(event_bus=EventBus())
    bad = _json.dumps({"name": "x", "dependencies": {
        " slick-carousel": "^1.8.1",   # leading space -> trim to valid
        "react": "^18.2.0",            # valid -> keep
        "has space": "1.0.0",          # internal space -> drop
        "": "1.0.0",                   # empty -> drop
    }})
    fixed = agent._deterministic_fix("package.json", bad, "nextjs")
    deps = _json.loads(fixed)["dependencies"]
    assert "slick-carousel" in deps and " slick-carousel" not in deps
    assert deps["react"] == "^18.2.0"
    assert "has space" not in deps and "" not in deps


# ---- score clamp on no_go (finding #17) ------------------------------------
def test_score_clamped_to_verdict():
    from skyn3t.studio.runner import StudioRunner
    # a failed delivery can never read like a success
    assert StudioRunner._clamp_score_to_verdict(100.0, "no_go") <= 49.0
    assert StudioRunner._clamp_score_to_verdict(92.5, "no_go") <= 49.0
    # a passing build keeps its score
    assert StudioRunner._clamp_score_to_verdict(87.0, "go") == 87.0
    # already-low no_go is untouched
    assert StudioRunner._clamp_score_to_verdict(12.0, "no_go") == 12.0


# ---- runtime liveness gate (findings #2, #19) ------------------------------
def test_liveness_gate_dead_root_fails_ui_stack():
    from skyn3t.studio.runner import StudioRunner
    g = StudioRunner._liveness_gate
    # UI app whose '/' is dead -> no_go, even with broad gate off
    v, why = g("go", "nextjs", 1, ["/"], False)
    assert v == "no_go" and why
    # a non-root dead route (e.g. 405 on /api) does NOT fail when broad gate off
    v, why = g("go", "nextjs", 1, ["/api/x"], False)
    assert v == "go" and why is None
    # API-only stack with dead '/' is NOT gated (its '/' may legitimately 404)
    v, why = g("go", "fastapi", 1, ["/"], False)
    assert v == "go" and why is None
    # broad opt-in gate fails on any dead route
    v, why = g("go", "nextjs", 2, ["/about", "/contact"], True)
    assert v == "no_go" and why


# ---- verdict consumes verifier verdicts (findings #4, #21) ------------------
def test_verifiers_gate_consumes_real_failures():
    from skyn3t.studio.runner import StudioRunner
    g = StudioRunner._verifiers_gate
    # real build failed -> block
    ok, why = g({"verify_build": {"verdict": "fail", "ran_real_build": True, "details": "ETARGET"}})
    assert ok is False and why
    # reward-hacking suspected -> block
    ok, why = g({"verify_build": {"verdict": "fail", "ran_real_build": False,
                                  "reward_hacking": {"suspicious": True, "flags": ["empty project"]}}})
    assert ok is False and why
    # offline/dry fail with no real build + not gamed -> does NOT block
    ok, why = g({"verify_build": {"verdict": "fail", "ran_real_build": False, "mode": "dry",
                                  "reward_hacking": {"suspicious": False}}})
    assert ok is True and why is None
    # a passing verifier -> not blocked
    ok, why = g({"verify_build": {"verdict": "pass", "ran_real_build": True}})
    assert ok is True
    # real python boot failure -> block; structural web boot fail -> not
    assert g({"verify_boot": {"verdict": "fail", "mode": "import"}})[0] is False
    assert g({"verify_boot": {"verdict": "fail", "mode": "web"}})[0] is True
    # nothing present -> not blocked
    assert g({})[0] is True


def test_verifiers_gate_proof_build_overrides_stale_verify_build():
    # verify_build runs as a STAGE on the pre-repair worktree; once the
    # authoritative post-repair proof_run build passes, a stale verify_build
    # failure must not veto the verdict — but reward-hacking still blocks.
    from skyn3t.studio.runner import StudioRunner
    g = StudioRunner._verifiers_gate
    vb_fail = {"verify_build": {"verdict": "fail", "ran_real_build": True, "details": "Module not found"}}
    assert g(vb_fail, proof_build_passed=True)[0] is True   # stale -> overridden
    assert g(vb_fail)[0] is False                            # no passing proof -> still blocks
    rh = {"verify_build": {"verdict": "fail", "ran_real_build": False,
                           "reward_hacking": {"suspicious": True, "flags": ["empty"]}}}
    assert g(rh, proof_build_passed=True)[0] is False        # reward-hacking still blocks


# ---- LLM-backed config detection bridge (findings #29/#35) ------------------
def test_config_llm_fn_none_on_stub_backend():
    """With the stub backend (tests/offline) the config llm_fn is None, so
    detection cleanly uses the keyword heuristic — no behavior change."""
    runner = _runner()  # Settings(llm_backend="stub")
    assert runner._config_llm_fn() is None


def test_config_llm_fn_bridges_async_when_real(monkeypatch):
    """When a real (non-stub) LLM is present, the sync bridge returns its text."""
    runner = _runner()

    class _Res:
        text = '{"keys": [{"name": "ACME_API_KEY", "scope": "client"}], "apis": ["Acme"]}'

    class _LLM:
        backend = "openrouter"
        async def complete(self, prompt, **kw):
            return _Res()

    monkeypatch.setattr(runner, "_intent_llm", lambda: _LLM())
    fn = runner._config_llm_fn()
    assert fn is not None
    assert "ACME_API_KEY" in fn("detect config for: an Acme-powered app")


# ---- fix-loop targets package.json on build module-not-found (HVAC regress) -
def test_targets_package_json_on_module_not_found(tmp_path):
    """A `next build` 'Module not found' is a dependency problem fixable in
    package.json — the fix-loop must route there, not rewrite source."""
    from skyn3t.agents.code_improver import CodeImproverAgent
    agent = CodeImproverAgent(event_bus=EventBus())
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    gaps = ["BUILD FAILED: Module not found: Can't resolve '@hookform/resolvers/yup' "
            "in ./components/ContactForm.jsx"]
    assert "package.json" in agent._targets_from_gaps(gaps, tmp_path)


# ---- fix-loop targets the module on a "not exported" error (HVAC layer 3) ---
def test_targets_module_on_not_exported(tmp_path):
    """A `next build` 'X is not exported from <module>' must route the fix-loop
    to that module (so the repair adds the real export), not an entrypoint."""
    from skyn3t.agents.code_improver import CodeImproverAgent
    agent = CodeImproverAgent(event_bus=EventBus())
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "constants.js").write_text("export const x = 1\n", encoding="utf-8")
    gaps = ["Attempted import error: 'services' is not exported from '../lib/constants'",
            "Attempted import error: 'companyInfo' is not exported from '@/lib/constants'"]
    targets = agent._targets_from_gaps(gaps, tmp_path)
    assert "lib/constants.js" in targets
