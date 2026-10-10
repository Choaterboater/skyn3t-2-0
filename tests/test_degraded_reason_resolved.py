"""code_degraded must reflect the DELIVERED tree, not a codegen-time snapshot.

bellwork-v2 (2026-10-09): the stall guard cut Opus off mid-slice, so codegen
recorded "missing 1 owned file(s): app/globals.css". The fix-loop then wrote
app/globals.css, `next build` passed, the app rendered — and the build was still
no_go because the degraded flag was never re-checked against the disk."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skyn3t.agents._scaffold import scaffold_for
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.planner import BuildPlan
from skyn3t.studio.proof_run import scaffold_missing_imports
from skyn3t.studio.runner import (
    StudioRunner,
    degraded_baseline_sha256,
    degraded_reason_resolved,
)

_REASON = ("frontend: agentic slice 'frontend' incomplete after 1 retry: provider did "
           "not confirm completion: agentic CLI ended without a successful result; "
           "missing 1 owned file(s): app/globals.css")


def test_resolved_when_every_missing_file_now_exists(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "globals.css").write_text(":root{--bg:#fff}\n")
    assert degraded_reason_resolved(_REASON, tmp_path) is True


def test_unresolved_while_file_still_missing_or_empty(tmp_path):
    assert degraded_reason_resolved(_REASON, tmp_path) is False
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "globals.css").write_text("  \n")
    assert degraded_reason_resolved(_REASON, tmp_path) is False


def test_multi_slice_all_must_resolve(tmp_path):
    reason = (_REASON + "; backend: completion slice 'backend' missed 2 owned file(s): "
              "lib/a.js, lib/b.js")
    (tmp_path / "app").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "app" / "globals.css").write_text("a{}\n")
    (tmp_path / "lib" / "a.js").write_text("export const a=1\n")
    assert degraded_reason_resolved(reason, tmp_path) is False
    (tmp_path / "lib" / "b.js").write_text("export const b=1\n")
    assert degraded_reason_resolved(reason, tmp_path) is True


def test_truncated_list_cannot_be_verified(tmp_path):
    names = [f"f{i}.js" for i in range(12)]
    for n in names:
        (tmp_path / n).write_text("x=1\n")
    reason = "slice 's' incomplete: missing 14 owned file(s): " + ", ".join(names)
    assert degraded_reason_resolved(reason, tmp_path) is False


def test_non_file_reasons_never_resolve(tmp_path):
    for reason in ("agentic build under-delivered after 1 retry",
                   "frontend: slice produced no files",
                   "frontend: agentic slice 'frontend' incomplete after 1 retry: "
                   "provider did not confirm completion",
                   "x: missing 0 owned file(s): ; rejected prose source: app/page.jsx",
                   ""):
        assert degraded_reason_resolved(reason, tmp_path) is False, reason


# The monolithic agentic path (the CLI default under best-of-N) and the completion
# path word their reasons differently from slices: "planned" files and an
# "agentic build failed or did not confirm completion" note (code_agent.py). The
# resolver only understood the slice wording, so these never cleared.
_MONOLITHIC = ("agentic build incomplete after 1 retry: agentic build failed or did not "
               "confirm completion: agentic CLI ended without a successful result; "
               "missing 2 planned file(s): src/App.jsx, src/styles.css")


def test_monolithic_reason_resolves_once_planned_files_exist(tmp_path):
    baseline = degraded_baseline_sha256(_MONOLITHIC, tmp_path)
    assert baseline == {"src/App.jsx": None, "src/styles.css": None}
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path, baseline_sha256=baseline) is False
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export default function App(){return null}\n")
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path, baseline_sha256=baseline) is False
    (tmp_path / "src" / "styles.css").write_text("body{margin:0}\n")
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path, baseline_sha256=baseline) is True


def test_monolithic_confirmed_but_missing_planned_resolves(tmp_path):
    reason = "agentic build incomplete after 1 retry: missing 1 planned file(s): app/page.tsx"
    baseline = degraded_baseline_sha256(reason, tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("export default function Page(){return null}\n")
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True


def test_completion_codegen_missed_planned_resolves(tmp_path):
    reason = "completion codegen missed 1 planned file(s): lib/db.js"
    baseline = degraded_baseline_sha256(reason, tmp_path)
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "db.js").write_text("export const db = {}\n")
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True


def test_monolithic_non_file_reasons_still_never_resolve(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("x\n")
    for reason in (
        # unconfirmed completion alone: nothing on disk to verify
        "agentic build incomplete after 1 retry: agentic build failed or did not "
        "confirm completion: provider returned without a successful finish",
        "agentic build incomplete after 1 retry: agentic build failed or did not "
        "confirm completion: timeout; missed required react contract: src/main.jsx",
        "agentic build incomplete after 2 retries: under-delivered 120 code bytes in "
        "1 files (threshold 4000)",
        "agentic build incomplete after 1 retry: missing 14 planned file(s): src/App.jsx",
    ):
        assert degraded_reason_resolved(
            reason, tmp_path, baseline_sha256={"src/App.jsx": None}) is False, reason


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _planned(*paths: str) -> str:
    return (f"agentic build incomplete after 1 retry: missing {len(paths)} planned "
            f"file(s): {', '.join(paths)}")


def _owned(*paths: str) -> str:
    return (f"frontend: agentic slice 'frontend' incomplete after 1 retry: missing "
            f"{len(paths)} owned file(s): {', '.join(paths)}")


def _write(root: Path, rel: str, content: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def test_baseline_names_every_missing_file_clause(tmp_path):
    _write(tmp_path, "src/App.jsx", "export default function App(){return 1}\n")
    _write(tmp_path, "lib/blank.js", "  \n")
    reason = (_planned("src/App.jsx", "src/sim.js") + "; missed required phaser contract: "
              "src/sim.js; " + _owned("lib/blank.js"))
    assert degraded_baseline_sha256(reason, tmp_path) == {
        "src/App.jsx": _sha(tmp_path / "src" / "App.jsx"),
        "src/sim.js": None,
        "lib/blank.js": None,
    }
    assert degraded_baseline_sha256("agentic build under-delivered", tmp_path) == {}


# Monolithic/completion codegen computes the reason BEFORE it back-fills missing
# planned paths with the offline scaffold, so a "missing" scaffold path (main.py,
# src/App.jsx, src/sim.js ...) holds placeholder content the moment codegen ends.
# The runner baselines each named path before the first proof (after codegen's
# back-fill and the pre-proof deterministic repairs): bytes still matching that
# baseline were never repaired.
def test_scaffold_backfill_unchanged_since_baseline_never_resolves(tmp_path):
    scaffold = scaffold_for("react_vite", "app", "an app")
    app = _write(tmp_path, "src/App.jsx", scaffold["src/App.jsx"])
    _write(tmp_path, "src/styles.css", "body{margin:0}\n")
    reason = _planned("src/App.jsx")
    baseline = degraded_baseline_sha256(reason, tmp_path)
    assert baseline == {"src/App.jsx": _sha(app)}
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False
    # The fix-loop's improver rewrote it: different bytes, so it really was repaired.
    app.write_text("export default function App(){return <main>kanban</main>}\n")
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True


def test_scaffold_backfill_reformatted_before_baseline_never_resolves(tmp_path):
    """The pre-proof ruff format rewrites a back-filled scaffold module, so its bytes
    no longer match the scaffold; the baseline is taken AFTER that, so it still
    matches and the untouched back-fill keeps the gate."""
    scaffold = scaffold_for("python_cli", "app", "a tool")
    core = _write(tmp_path, "app/core.py", scaffold["app/core.py"])
    reason = _planned("app/core.py")
    # ruff-format-like whitespace change, made by a deterministic repair pre-proof
    core.write_text(scaffold["app/core.py"].replace("\n", "\n\n", 1) + "\n")
    assert _sha(core) != hashlib.sha256(scaffold["app/core.py"].encode()).hexdigest()
    baseline = degraded_baseline_sha256(reason, tmp_path)
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False


def test_planned_reason_without_baseline_fails_closed(tmp_path):
    _write(tmp_path, "src/App.jsx", "export default function App(){return 1}\n")
    _write(tmp_path, "src/styles.css", "body{margin:0}\n")
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path) is False
    assert degraded_reason_resolved(
        "completion codegen missed 1 planned file(s): src/App.jsx", tmp_path) is False
    # A baseline that never saw a named planned path fails closed for it too.
    assert degraded_reason_resolved(
        _MONOLITHIC, tmp_path, baseline_sha256={"src/App.jsx": None}) is False
    # Slices never back-fill from the scaffold: their reasons need no baseline.
    _write(tmp_path, "app/globals.css", ":root{--bg:#fff}\n")
    assert degraded_reason_resolved(_REASON, tmp_path) is True


def test_owned_path_unchanged_since_a_present_baseline_never_resolves(tmp_path):
    """Slices keep missing paths missing, so an owned baseline is normally None;
    when it does hold bytes, an untouched path was never repaired either."""
    css = _write(tmp_path, "app/globals.css", ":root{--bg:#fff}\n")
    baseline = degraded_baseline_sha256(_REASON, tmp_path)
    assert degraded_reason_resolved(_REASON, tmp_path, baseline_sha256=baseline) is False
    css.write_text(":root{--bg:#000}\n")
    assert degraded_reason_resolved(_REASON, tmp_path, baseline_sha256=baseline) is True


def test_scaffold_stylesheet_and_readme_are_not_exempt(tmp_path):
    """A starter stylesheet or README untouched since the baseline is the
    scaffold, not a delivery; only pure config/manifests are exempt."""
    scaffold = scaffold_for("react_vite", "app", "an app")
    for rel in ("README.md", "src/styles.css", "src/design-tokens.css", "index.html",
                "src/main.jsx"):
        _write(tmp_path, rel, scaffold[rel])
        reason = _planned(rel)
        baseline = degraded_baseline_sha256(reason, tmp_path)
        assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False, rel
        assert degraded_reason_resolved(
            _owned(rel), tmp_path, baseline_sha256=baseline) is False, rel


@pytest.mark.parametrize(("stack", "paths"), [
    ("react_vite", ["vite.config.js", "package.json", ".gitignore"]),
    ("react_native", ["app.json", "tsconfig.json", "babel.config.js"]),
    ("swift", ["Package.swift"]),
    ("tauri", ["src-tauri/Cargo.toml", "src-tauri/tauri.conf.json"]),
    ("python_cli", ["pyproject.toml", "requirements.txt"]),
])
def test_config_and_manifests_unchanged_since_baseline_resolve(tmp_path, stack, paths):
    """A deterministic copy of config/manifests is already a working delivery, so
    once proof passed it must not pin the gate shut (no false no_go)."""
    scaffold = scaffold_for(stack, "app", "an app")
    for rel in paths:
        _write(tmp_path, rel, scaffold[rel] or "# deps\n")
    reason = _planned(*paths)
    baseline = degraded_baseline_sha256(reason, tmp_path)
    assert all(baseline[rel] for rel in paths)
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True
    # ...but never alongside an unrepaired source module.
    source = next(rel for rel in sorted(scaffold) if rel not in paths and rel.endswith(
        (".jsx", ".tsx", ".swift", ".rs", ".py")) and scaffold[rel].strip())
    _write(tmp_path, source, scaffold[source])
    reason = _planned(*paths, source)
    baseline = degraded_baseline_sha256(reason, tmp_path)
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False


def test_real_content_at_a_scaffold_path_resolves(tmp_path):
    scaffold = scaffold_for("react_vite", "app", "an app")
    _write(tmp_path, "src/App.jsx", scaffold["src/App.jsx"])
    reason = _planned("src/App.jsx", "src/Board.jsx")
    baseline = degraded_baseline_sha256(reason, tmp_path)
    _write(tmp_path, "src/App.jsx", "export default function App(){return <main>kanban</main>}\n")
    _write(tmp_path, "src/Board.jsx", "export default function Board(){return <section/>}\n")
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True


_BIG_JS = "\n".join(f"export const value_{i} = {i};" for i in range(1400))


class _ScriptedAgenticLLM:
    backend = "claude_cli"
    supports_agentic = True
    last_model = None
    last_route = None
    routes: list = []

    def __init__(self, files: dict[str, str]):
        self.files = files

    async def agentic_build(self, prompt, workdir, **kwargs):
        for rel, content in self.files.items():
            target = Path(workdir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return {"ok": True, "completed": True, "backend": "claude_cli"}


async def _codegen(tmp_path, stack: str, planned: list[str], llm) -> dict:
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    result = await agent.run(TaskRequest(
        type="codegen",
        payload={
            "brief": "an app", "slug": "app", "stack": stack,
            "plan": {"stack": stack, "files": [{"path": p} for p in planned]},
            "worktree_dir": str(tmp_path),
        },
        capabilities_required=("codegen",),
    ))
    assert result.success
    return result.output


@pytest.mark.parametrize(("stack", "missing", "written"), [
    ("react_vite", ["src/App.jsx", "src/styles.css"], {"src/feature.js": _BIG_JS}),
    ("fastapi", ["main.py", "test_main.py"],
     {"app/core.py": "\n".join(f"VALUE_{i} = {i}" for i in range(1400))}),
])
async def test_monolithic_scaffold_backfill_never_self_resolves(
        tmp_path, stack, missing, written):
    out = await _codegen(tmp_path, stack, [*missing, *written],
                         _ScriptedAgenticLLM(written))
    reason = out["degraded_reason"]
    assert f"missing {len(missing)} planned file(s): {', '.join(missing)}" in reason
    assert "scaffold_filled" not in out
    baseline = degraded_baseline_sha256(reason, tmp_path)
    for rel in missing:
        # Back-filled from the scaffold: present, and baselined with its bytes.
        assert baseline[rel] == _sha(tmp_path / rel)
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False
    # Only after the fix-loop actually rewrites every named file does it clear.
    for rel in missing:
        (tmp_path / rel).write_text(f"# repaired {rel}\nREAL = True\n")
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True


async def test_repair_stub_stylesheet_in_a_missing_planned_path_never_resolves(tmp_path):
    """_repair_entrypoints stubs an imported-but-missing stylesheet after the
    reason is written; that stand-in must not clear the gate either."""
    main_jsx = "import './index.css'\nimport App from './App.jsx'\n" + _BIG_JS
    out = await _codegen(tmp_path, "react_vite", ["src/main.jsx", "src/index.css"],
                         _ScriptedAgenticLLM({"src/main.jsx": main_jsx}))
    reason = out["degraded_reason"]
    assert "missing 1 planned file(s): src/index.css" in reason
    assert "stub stylesheet" in (tmp_path / "src" / "index.css").read_text()
    for baseline in (degraded_baseline_sha256(reason, tmp_path), {"src/index.css": None}):
        assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False


async def test_completion_scaffold_fallback_never_resolves(tmp_path):
    class _CompletionLLM:
        backend = "openrouter"
        supports_agentic = False
        last_model = None
        last_route = None
        routes: list = []

    agent = CodeAgent(event_bus=EventBus(), llm=_CompletionLLM())  # type: ignore[arg-type]
    await agent.start()

    async def fake_generate(rel_path, *args, **kwargs):
        if rel_path == "src/App.jsx":
            raise RuntimeError("provider timed out")
        return f"// generated {rel_path}\n{_BIG_JS}\n"

    agent._generate_file = fake_generate  # type: ignore[method-assign]
    result = await agent.run(TaskRequest(
        type="codegen",
        payload={
            "brief": "an app", "slug": "app", "stack": "react_vite",
            "plan": {"stack": "react_vite",
                     "files": [{"path": "src/App.jsx"}, {"path": "src/feature.js"}]},
            "worktree_dir": str(tmp_path),
        },
        capabilities_required=("codegen",),
    ))
    out = result.output
    assert out["degraded_reason"] == "completion codegen missed 1 planned file(s): src/App.jsx"
    baseline = degraded_baseline_sha256(out["degraded_reason"], tmp_path)
    assert baseline == {"src/App.jsx": _sha(tmp_path / "src" / "App.jsx")}
    assert degraded_reason_resolved(
        out["degraded_reason"], tmp_path, baseline_sha256=baseline) is False


async def test_phaser_contract_gap_is_not_hidden_by_missing_planned(tmp_path):
    main_js = "import Phaser from 'phaser';\nnew Phaser.Game({});\n" + _BIG_JS
    out = await _codegen(
        tmp_path, "phaser", ["src/main.js", "src/sim.js", "src/scenes/Game.js"],
        _ScriptedAgenticLLM({"src/main.js": main_js, "src/scenes/Game.js": _BIG_JS}))
    reason = out["degraded_reason"]
    assert "missing 1 planned file(s): src/sim.js" in reason
    assert "missed required phaser contract" in reason
    baseline = degraded_baseline_sha256(reason, tmp_path)
    (tmp_path / "src" / "sim.js").write_text("export function step(s){return s}\n")
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False


async def test_under_delivery_is_not_hidden_by_missing_planned(tmp_path):
    out = await _codegen(
        tmp_path, "react_vite", ["src/feature.js", "src/extra.js"],
        _ScriptedAgenticLLM({"src/feature.js": "export const a = 1\n"}))
    reason = out["degraded_reason"]
    assert "missing 1 planned file(s): src/extra.js" in reason
    assert "under-delivered" in reason
    baseline = degraded_baseline_sha256(reason, tmp_path)
    (tmp_path / "src" / "extra.js").write_text("export const b = 2\n")
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False


# The RUNNER also writes stand-ins into missing paths: the pre-proof deterministic
# repairs stub missing import targets (an "Auto-scaffolded by SkyN3t" module or
# the stub stylesheet) and the fix-loop's _fill_missing copies its own scaffold
# or a _stub_for placeholder. Each lets the proof pass, none is delivery.
def _runner() -> StudioRunner:
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def test_import_stub_at_a_missing_path_never_resolves(tmp_path):
    _write(tmp_path, "src/App.jsx",
           "import Board from './components/Board';\n"
           "export default function App(){return <Board/>}\n")
    assert scaffold_missing_imports(tmp_path, stack="react_vite") == [
        "src/components/Board.jsx"]
    board = tmp_path / "src" / "components" / "Board.jsx"
    assert "Auto-scaffolded by SkyN3t" in board.read_text()
    planned = _planned("src/components/Board.jsx")
    for reason, baseline in ((planned, degraded_baseline_sha256(planned, tmp_path)),
                             (planned, {"src/components/Board.jsx": None}),
                             (_owned("src/components/Board.jsx"), None)):
        assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False
    baseline = degraded_baseline_sha256(planned, tmp_path)
    board.write_text("export default function Board(){return <section>board</section>}\n")
    assert degraded_reason_resolved(planned, tmp_path, baseline_sha256=baseline) is True


def test_stub_stylesheet_never_resolves(tmp_path):
    _write(tmp_path, "src/main.jsx", "import './index.css'\nimport App from './App.jsx'\n")
    assert "src/index.css" in scaffold_missing_imports(tmp_path, stack="react_vite")
    planned = _planned("src/index.css")
    for reason, baseline in ((planned, {"src/index.css": None}),
                             (_owned("src/index.css"), None)):
        assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False
    baseline = degraded_baseline_sha256(planned, tmp_path)
    (tmp_path / "src" / "index.css").write_text("body{margin:0}\n")
    assert degraded_reason_resolved(planned, tmp_path, baseline_sha256=baseline) is True


def test_smoke_test_stub_never_resolves(tmp_path):
    plan = BuildPlan(slug="tool", brief="a tool", stack="python_cli", stages=[])
    assert _runner()._fill_missing(str(tmp_path), plan, "a tool", ["tests/test_tool.py"]) == 1
    test_file = tmp_path / "tests" / "test_tool.py"
    assert test_file.read_text() == StudioRunner._stub_for("tests/test_tool.py", plan, "a tool")
    reason = "backend: completion slice 'backend' missed 1 owned file(s): tests/test_tool.py"
    assert degraded_reason_resolved(reason, tmp_path) is False
    test_file.write_text("def test_smoke() :\r\n\tassert   True")  # whitespace only differs
    assert degraded_reason_resolved(reason, tmp_path) is False
    test_file.write_text("from tool import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    assert degraded_reason_resolved(reason, tmp_path) is True


class _Proof:
    def __init__(self, passed: bool, missing: list[str]):
        self.passed, self.missing, self.detail = passed, missing, {}

    def error_gaps(self):
        return []

    def to_dict(self):
        return {"passed": self.passed, "missing": self.missing, "detail": self.detail}


async def _fix_once(tmp_path, monkeypatch, plan, reason: str, missing: list[str], *,
                    baseline: dict | None = None, runner: StudioRunner | None = None):
    """One fix-loop round over ``missing`` (the re-proof passes), starting from the
    baseline the runner records before the first proof."""
    manifest = BuildManifest(slug=plan.slug, brief=plan.brief)
    manifest.extra["degraded_baseline_sha256"] = (
        baseline if baseline is not None else degraded_baseline_sha256(reason, tmp_path))
    monkeypatch.setattr("skyn3t.studio.runner.proof_run", lambda *a, **k: _Proof(True, []))
    await (runner or _runner())._fix_loop(
        manifest, plan, str(tmp_path), _Proof(False, missing), "cid",
        {"max_fix_attempts": 1}, degraded_reason=reason)
    return manifest.extra["degraded_baseline_sha256"]


async def test_fix_loop_fill_is_baselined_after_its_round_repairs(tmp_path, monkeypatch):
    """_fill_missing back-fills a missing planned module and the same round's
    deterministic repairs ruff-format it: the baseline is re-taken after both,
    so that back-fill never counts as a repair."""
    rel = "app/engine.py"
    filled = "def step( state ):\n  return state+1\n"
    monkeypatch.setattr(StudioRunner, "_runner_scaffold",
                        staticmethod(lambda plan, brief: {rel: filled}))
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'tool'\nversion = '0.1.0'\n\n[tool.ruff]\n")
    plan = BuildPlan(slug="tool", brief="a tool", stack="python_cli", stages=[])
    reason = _planned(rel)
    baseline = await _fix_once(tmp_path, monkeypatch, plan, reason, [rel])
    engine = tmp_path / rel
    assert engine.read_text() == "def step(state):\n    return state + 1\n"  # formatted
    assert baseline == {rel: _sha(engine)}
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False
    engine.write_text("def step(state):\n    return state + 2\n")  # an improver repair
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True


async def test_fix_loop_reformat_of_an_untouched_backfill_keeps_the_gate(
        tmp_path, monkeypatch):
    """A fix-loop round's deterministic repairs can reformat a codegen back-fill
    nobody repaired (here the project opts into Ruff only after the baseline):
    the baseline follows, so it still counts as unrepaired."""
    rel = "app/engine.py"
    engine = _write(tmp_path, rel, "def step( state ):\n  return state+1\n")
    pyproject = _write(tmp_path, "pyproject.toml", "[project]\nname = 'tool'\nversion = '0.1.0'\n")
    plan = BuildPlan(slug="tool", brief="a tool", stack="python_cli", stages=[])
    reason = _planned(rel)
    baseline = degraded_baseline_sha256(reason, tmp_path)
    pyproject.write_text(pyproject.read_text() + "\n[tool.ruff]\n")
    baseline = await _fix_once(tmp_path, monkeypatch, plan, reason, [], baseline=baseline)
    assert engine.read_text() == "def step(state):\n    return state + 1\n"
    assert baseline == {rel: _sha(engine)}
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False


async def test_fix_loop_improver_repair_after_a_backfill_resolves(tmp_path, monkeypatch):
    """The re-baseline happens before the round's improver runs, so its rewrite of
    the freshly back-filled path counts as the repair."""
    rel = "app/engine.py"

    class _Improver(BaseAgent):
        async def initialize(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def execute(self, task: TaskRequest) -> TaskResult:
            (Path(task.payload["project_dir"]) / rel).write_text(
                "def step(state):\n    return state + 2\n")
            return TaskResult(task_id=task.task_id, success=True,
                              output={"files_improved": 1})

    runner = _runner()
    improver = _Improver("improver", "code_improve", "stub", runner.event_bus)
    improver.add_capability(AgentCapability("code_improve"))
    await runner.orchestrator.register(improver)
    monkeypatch.setattr(StudioRunner, "_runner_scaffold",
                        staticmethod(lambda plan, brief: {rel: "def step(state):\n    return state\n"}))
    plan = BuildPlan(slug="tool", brief="a tool", stack="python_cli", stages=[])
    reason = _planned(rel)
    baseline = await _fix_once(tmp_path, monkeypatch, plan, reason, [rel], runner=runner)
    assert (tmp_path / rel).read_text() == "def step(state):\n    return state + 2\n"
    assert baseline[rel] == hashlib.sha256(b"def step(state):\n    return state\n").hexdigest()
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True


async def test_fix_loop_scaffold_fill_of_planned_source_never_resolves(tmp_path, monkeypatch):
    brief = "a kanban board"
    plan = BuildPlan(slug="board", brief=brief, stack="react", stages=[])
    missing = ["src/App.jsx", "src/main.jsx", "index.html"]
    reason = _planned(*missing)
    baseline = await _fix_once(tmp_path, monkeypatch, plan, reason, missing)
    scaffold = StudioRunner._runner_scaffold(plan, brief)
    for rel in missing:
        assert (tmp_path / rel).read_text() == scaffold[rel]
        assert baseline[rel] == _sha(tmp_path / rel)
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is False
    for rel in missing:
        (tmp_path / rel).write_text(f"// repaired {rel}\nexport const real = true\n")
    assert degraded_reason_resolved(reason, tmp_path, baseline_sha256=baseline) is True


async def test_fix_loop_scaffold_stylesheet_for_owned_path_still_resolves(
        tmp_path, monkeypatch):
    """bellwork: the fix-loop wrote app/globals.css from the scaffold and the app
    built. Owned (slice) paths are never re-baselined: once present they resolve."""
    brief = "a classroom bellwork app"
    plan = BuildPlan(slug="bellwork", brief=brief, stack="nextjs", stages=[])
    baseline = await _fix_once(tmp_path, monkeypatch, plan, _REASON, ["app/globals.css"])
    css = tmp_path / "app" / "globals.css"
    assert css.read_text() == StudioRunner._runner_scaffold(plan, brief)["app/globals.css"]
    assert baseline == {"app/globals.css": None}
    assert degraded_reason_resolved(_REASON, tmp_path, baseline_sha256=baseline) is True
