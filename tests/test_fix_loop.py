"""Bounded fix loop: a failing objective proof is repaired, not just reported."""

from __future__ import annotations

import json as _json
import shutil

import pytest

from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
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


# ---- game-quality verdict gate (visual + QA hard-gate) ---------------------
def test_game_quality_gate_blocks_only_real_nonskipped_failures():
    from skyn3t.studio.runner import StudioRunner
    g = StudioRunner._game_quality_gate_ok
    # a REAL, non-skipped failure (empty play field / sprites never render) blocks
    assert g(False, False, True) is False
    # a passing check is fine
    assert g(True, False, True) is True
    # a SKIPPED check (no vision model / no Playwright) never false-fails a build
    assert g(False, True, True) is True
    # gating disabled -> advisory (legacy behavior), never blocks
    assert g(False, False, False) is True
    # an unknown/None verdict is treated as non-blocking (do-no-harm)
    assert g(None, False, True) is True


def test_headless_reachable_gate_is_optin_and_safe():
    from skyn3t.studio.runner import StudioRunner
    g = StudioRunner._headless_reachable_ok

    class _Gate:
        def __init__(self, applicable, report):
            self.applicable = applicable
            self.report = report

    both_dead = {"winReachable": False, "loseReachable": False}
    # off by default -> never blocks, even with no reachable ending
    assert g(_Gate(True, both_dead), False) is True
    # opt-in ON: a game you can neither win nor lose is blocked
    assert g(_Gate(True, both_dead), True) is False
    # at least one reachable ending -> ok
    assert g(_Gate(True, {"winReachable": True, "loseReachable": False}), True) is True
    # non-applicable / absent gate never false-fails
    assert g(_Gate(False, both_dead), True) is True
    assert g(None, True) is True


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


# ---- unconditional final consistency pass (real shipped bug) ----------------
# _headless_gate_pass, the game-visual loop, qa_playtest's repair, visual_self_heal,
# and liveness can ALL mutate files after the last proof_run() a build captures —
# none of them re-verify import resolution against what THEY leave behind. Root
# cause of a real shipped bug: a fresh agentic codegen session left `src/main.js`
# importing `./PreloadScene.js`, which was never written — the app couldn't boot,
# but the STORED proof (captured before that particular file state) still said
# passed=True. This pass re-checks the TRUE final file state, unconditionally,
# after every post-proof stage has had its chance to run.

def test_final_consistency_check_forces_no_go_on_a_late_dangling_import(tmp_path):
    runner = _runner()
    plan = Planner(Settings()).plan("a phaser game", "moonshine")
    plan.stack = "phaser"
    proj = tmp_path / "p"
    (proj / "src").mkdir(parents=True)
    # Simulates a post-proof repair stage (e.g. qa_playtest's improver call)
    # leaving behind exactly the real shipped defect: an import to a file that
    # was never created, undetectable by scaffold_missing_imports's stub path
    # only if it too were broken -- here it CAN stub it, but the point is: no
    # earlier proof_run() saw this file state at all.
    (proj / "src" / "main.js").write_text(
        "import { PreloadScene } from './PreloadScene.js';\n"
        "export default PreloadScene;\n", encoding="utf-8")
    manifest = BuildManifest(slug="moonshine", brief="a phaser game")

    verdict = runner._final_consistency_check(str(proj), plan, manifest, "go")

    # The deterministic stub scaffolder should have fixed it (Fix #1/#2) --
    # confirm the pass actually repairs when it CAN, not just detects.
    assert (proj / "src" / "PreloadScene.js").exists()
    assert verdict == "go"
    assert "final_consistency_repairs" in manifest.extra
    assert "src/PreloadScene.js" in manifest.extra["final_consistency_repairs"]["imports_scaffolded"]


def test_final_consistency_check_forces_no_go_when_unfixable(tmp_path, monkeypatch):
    # When the deterministic repair genuinely can't resolve it (simulated by
    # neutering scaffold_missing_imports), the pass must still catch the
    # unresolved import and force no_go -- it must not trust a stale "go".
    runner = _runner()
    plan = Planner(Settings()).plan("a phaser game", "moonshine")
    plan.stack = "phaser"
    proj = tmp_path / "p"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.js").write_text(
        "import { PreloadScene } from './PreloadScene.js';\n"
        "export default PreloadScene;\n", encoding="utf-8")
    manifest = BuildManifest(slug="moonshine", brief="a phaser game")

    # _deterministic_repairs now delegates to proof_run.apply_deterministic_repairs,
    # which calls scaffold_missing_imports as a proof_run module global — patch it
    # THERE so the neutering takes effect through the delegation chain.
    import skyn3t.studio.proof_run as proof_mod
    monkeypatch.setattr(proof_mod, "scaffold_missing_imports", lambda *a, **kw: [])

    verdict = runner._final_consistency_check(str(proj), plan, manifest, "go")

    assert not (proj / "src" / "PreloadScene.js").exists()
    assert verdict == "no_go"
    assert "final_consistency_check" in manifest.extra
    assert any("PreloadScene" in u for u in manifest.extra["final_consistency_check"]["unresolved_imports"])


def test_final_consistency_check_never_downgrades_no_go_to_go(tmp_path):
    # The pass must never UPGRADE a verdict -- only confirm "go" or downgrade it.
    runner = _runner()
    plan = Planner(Settings()).plan("a python tool", "t")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "main.py").write_text("print('ok')\n", encoding="utf-8")
    manifest = BuildManifest(slug="t", brief="a python tool")

    verdict = runner._final_consistency_check(str(proj), plan, manifest, "no_go")

    assert verdict == "no_go"


def test_final_consistency_check_is_a_noop_on_a_clean_tree(tmp_path):
    runner = _runner()
    plan = Planner(Settings()).plan("a python tool", "t")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "main.py").write_text("print('ok')\n", encoding="utf-8")
    manifest = BuildManifest(slug="t", brief="a python tool")

    verdict = runner._final_consistency_check(str(proj), plan, manifest, "go")

    assert verdict == "go"
    assert "final_consistency_check" not in manifest.extra


# ---- qa_playtest gate re-verifies after a repair (real bug) -----------------
# A successful code_improve repair dispatched off qa_playtest gaps updated
# manifest.files but NEVER re-ran qa_playtest() — so qa_playtest_ok stayed the
# STALE pre-repair value forever, and a fully successful repair could never
# flip the build's verdict from no_go to go via this path. _run_qa_playtest_gate
# must re-verify ONCE (not loop) after a dispatched repair completes.

class _FakeCodeImprover(BaseAgent):
    """A no-op code_improve agent: the test's fake qa_playtest (not this agent)
    decides whether the "repair" succeeded, so this only needs to satisfy
    _has_capability(...) and return a successful TaskResult."""

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(task_id=task.task_id, success=True, output={})


async def _register_code_improve(runner: StudioRunner) -> None:
    agent = _FakeCodeImprover("improver", "code_improve", "stub", runner.event_bus)
    agent.add_capability(AgentCapability("code_improve"))
    await runner.orchestrator.register(agent)


def _qa_gate_fixture(tmp_path):
    runner = _runner()
    plan = Planner(Settings()).plan("a phaser game", "moonshine")
    plan.stack = "phaser"
    proj = tmp_path / "p"
    proj.mkdir()
    manifest = BuildManifest(slug="moonshine", brief="a phaser game")
    return runner, plan, proj, manifest


async def test_qa_playtest_gate_reverifies_and_flips_no_go_to_go_on_real_repair(
        tmp_path, monkeypatch):
    from skyn3t.studio.qa_playtest import QaPlaytestVerdict

    runner, plan, proj, manifest = _qa_gate_fixture(tmp_path)
    await _register_code_improve(runner)

    calls = {"n": 0}

    async def fake_qa_playtest(project_dir, *, settings, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return QaPlaytestVerdict(console_errors=["ReferenceError: x is not defined"])
        return QaPlaytestVerdict()  # clean second playthrough -> repair WORKED

    # GOTCHA: qa_playtest is imported LOCALLY inside the gate, so patch the
    # SOURCE module (skyn3t.studio.qa_playtest.qa_playtest), not any
    # runner-module name — there is no module-level name to patch there.
    monkeypatch.setattr("skyn3t.studio.qa_playtest.qa_playtest", fake_qa_playtest)
    monkeypatch.setattr(
        "skyn3t.studio.game_visual_loop.select_game_source_files",
        lambda project_dir: ["src/main.js"],
    )

    ok = await runner._run_qa_playtest_gate(
        object(), manifest, plan, str(proj), "cid", {})

    assert calls["n"] == 2  # re-verified exactly once after the repair
    assert ok is True  # a genuinely successful repair MUST flip the gate
    assert manifest.extra["qa_playtest"]["ok"] is True  # fresh, not the stale failure
    assert manifest.extra["qa_playtest"]["console_errors"] == []
    assert "qa_playtest_gate" not in manifest.extra  # stale failure note dropped


async def test_qa_playtest_gate_stays_no_go_with_fresh_gaps_when_repair_incomplete(
        tmp_path, monkeypatch):
    from skyn3t.studio.qa_playtest import QaPlaytestVerdict

    runner, plan, proj, manifest = _qa_gate_fixture(tmp_path)
    await _register_code_improve(runner)

    calls = {"n": 0}

    async def fake_qa_playtest(project_dir, *, settings, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return QaPlaytestVerdict(console_errors=["ReferenceError: first bug"])
        # Repair didn't fully fix it -- a DIFFERENT error surfaces post-repair.
        return QaPlaytestVerdict(console_errors=["TypeError: second bug"])

    monkeypatch.setattr("skyn3t.studio.qa_playtest.qa_playtest", fake_qa_playtest)
    monkeypatch.setattr(
        "skyn3t.studio.game_visual_loop.select_game_source_files",
        lambda project_dir: ["src/main.js"],
    )

    ok = await runner._run_qa_playtest_gate(
        object(), manifest, plan, str(proj), "cid", {})

    assert calls["n"] == 2
    assert ok is False  # still genuinely failing -> stays no_go
    # The reported gap text must be the POST-repair truth, not stale pre-repair text.
    assert "second bug" in manifest.extra["qa_playtest_gate"]
    assert "first bug" not in manifest.extra["qa_playtest_gate"]
    assert manifest.extra["qa_playtest"]["console_errors"] == ["TypeError: second bug"]


async def test_qa_playtest_gate_no_repair_dispatched_when_clean_on_first_pass(
        tmp_path, monkeypatch):
    """No gaps -> no code_improve task -> qa_playtest called exactly ONCE
    (guards against the fix accidentally re-verifying every clean build too)."""
    from skyn3t.studio.qa_playtest import QaPlaytestVerdict

    runner, plan, proj, manifest = _qa_gate_fixture(tmp_path)
    await _register_code_improve(runner)

    calls = {"n": 0}

    async def fake_qa_playtest(project_dir, *, settings, **kw):
        calls["n"] += 1
        return QaPlaytestVerdict()

    monkeypatch.setattr("skyn3t.studio.qa_playtest.qa_playtest", fake_qa_playtest)

    ok = await runner._run_qa_playtest_gate(
        object(), manifest, plan, str(proj), "cid", {})

    assert calls["n"] == 1
    assert ok is True


async def test_qa_playtest_gate_skips_entirely_for_non_game_stacks(tmp_path, monkeypatch):
    """``gate is None`` selects non-game stacks -- must short-circuit before ever
    importing/calling qa_playtest."""
    runner, plan, proj, manifest = _qa_gate_fixture(tmp_path)

    called = {"n": 0}

    async def fake_qa_playtest(project_dir, *, settings, **kw):
        called["n"] += 1
        raise AssertionError("qa_playtest must not run when gate is None")

    monkeypatch.setattr("skyn3t.studio.qa_playtest.qa_playtest", fake_qa_playtest)

    ok = await runner._run_qa_playtest_gate(
        None, manifest, plan, str(proj), "cid", {})

    assert called["n"] == 0
    assert ok is True
